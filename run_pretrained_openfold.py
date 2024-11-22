# Copyright 2021 AlQuraishi Laboratory
# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import json
import logging
import math
import os
import pickle
import random
import time
from functools import partial
import traceback
import warnings

import numpy as np
import pandas as pd

logging.basicConfig()
logger = logging.getLogger(__file__)
logger.setLevel(level=logging.INFO)

# unable warning info
warnings.filterwarnings("ignore")
logging.getLogger("root").setLevel(logging.ERROR)

import torch

torch_versions = torch.__version__.split(".")
torch_major_version = int(torch_versions[0])
torch_minor_version = int(torch_versions[1])
if torch_major_version > 1 or (torch_major_version == 1 and torch_minor_version >= 12):
    # Gives a large speedup on Ampere-class GPUs
    torch.set_float32_matmul_precision("high")

torch.set_grad_enabled(False)

from openfold.config import model_config
from openfold.data import data_pipeline, feature_pipeline, templates
from openfold.data.tools import hhsearch, hmmsearch
from openfold.np import protein
from openfold.utils.script_utils import (
    load_models_from_command_line,
    parse_fasta,
    prep_output,
    relax_protein,
    run_model,
)
from openfold.utils.tensor_utils import tensor_tree_map
from openfold.utils.trace_utils import pad_feature_dict_seq, trace_model_
from scripts.precompute_embeddings import EmbeddingGenerator
from scripts.utils import add_data_args

TRACING_INTERVAL = 50


def get_msa_stats(all_msa, seq_length_1, seq_length_2, num_recycle, if_homo, prefix=""):
    res = pd.DataFrame(index=range(1, num_recycle + 1))
    res.index.name = "recycle_num"
    for i in range(num_recycle):
        if prefix == "total_":
            batch_msa = all_msa
        else:
            batch_msa = all_msa[:, :, i]
        mask = (batch_msa != 0).any(dim=1)
        batch_msa = batch_msa[mask, :]
        msa_1 = batch_msa[:, :seq_length_1]
        if not if_homo:
            msa_2 = batch_msa[:, seq_length_1:]
            assert msa_2.shape[1] == seq_length_2
        else:
            msa_2 = msa_1
            assert batch_msa.shape[1] == seq_length_1
        have_msa_1 = ~(msa_1 == 21).all(dim=1)
        have_msa_2 = ~(msa_2 == 21).all(dim=1)
        pair_msa = have_msa_1 & have_msa_2
        unpair_msa_1 = have_msa_1 ^ pair_msa
        unpair_msa_2 = have_msa_2 ^ pair_msa
        res.loc[i + 1, [prefix + j for j in ["paired", "unpaired_chain_1", "unpaired_chain_2"]]] = [
            pair_msa.sum().item(),
            unpair_msa_1.sum().item(),
            unpair_msa_2.sum().item(),
        ]
    return res.astype(int)


def precompute_alignments(tags, seqs, alignment_dir, args):
    for tag, seq in zip(tags, seqs):
        tmp_fasta_path = os.path.join(args.output_dir, f"tmp_{os.getpid()}.fasta")
        with open(tmp_fasta_path, "w") as fp:
            fp.write(f">{tag}\n{seq}")

        local_alignment_dir = os.path.join(alignment_dir, tag)

        if args.use_precomputed_alignments is None:
            logger.info(f"Generating alignments for {tag}...")

            os.makedirs(local_alignment_dir, exist_ok=True)

            if "multimer" in args.config_preset:
                template_searcher = hmmsearch.Hmmsearch(
                    binary_path=args.hmmsearch_binary_path,
                    hmmbuild_binary_path=args.hmmbuild_binary_path,
                    database_path=args.pdb_seqres_database_path,
                )
            else:
                template_searcher = hhsearch.HHSearch(
                    binary_path=args.hhsearch_binary_path,
                    databases=[args.pdb70_database_path],
                )

            # In seqemb mode, use AlignmentRunner only to generate templates
            if args.use_single_seq_mode:
                alignment_runner = data_pipeline.AlignmentRunner(
                    jackhmmer_binary_path=args.jackhmmer_binary_path,
                    uniref90_database_path=args.uniref90_database_path,
                    template_searcher=template_searcher,
                    no_cpus=args.cpus,
                )
                embedding_generator = EmbeddingGenerator()
                embedding_generator.run(tmp_fasta_path, alignment_dir)
            else:
                alignment_runner = data_pipeline.AlignmentRunner(
                    jackhmmer_binary_path=args.jackhmmer_binary_path,
                    hhblits_binary_path=args.hhblits_binary_path,
                    uniref90_database_path=args.uniref90_database_path,
                    mgnify_database_path=args.mgnify_database_path,
                    bfd_database_path=args.bfd_database_path,
                    uniref30_database_path=args.uniref30_database_path,
                    uniclust30_database_path=args.uniclust30_database_path,
                    uniprot_database_path=args.uniprot_database_path,
                    template_searcher=template_searcher,
                    use_small_bfd=args.bfd_database_path is None,
                    no_cpus=args.cpus,
                )

            alignment_runner.run(tmp_fasta_path, local_alignment_dir)
        else:
            logger.debug(f"Using precomputed alignments for {tag} at {alignment_dir}...")

        # Remove temporary FASTA file
        os.remove(tmp_fasta_path)


def round_up_seqlen(seqlen):
    return int(math.ceil(seqlen / TRACING_INTERVAL)) * TRACING_INTERVAL


def generate_feature_dict(tags, seqs, alignment_dir, data_processor, args, template_save_dir):
    tmp_fasta_path = os.path.join(args.output_dir, f"tmp_{os.getpid()}.fasta")

    if "multimer" in args.config_preset:
        with open(tmp_fasta_path, "w") as fp:
            fp.write("\n".join([f">{tag}\n{seq}" for tag, seq in zip(tags, seqs)]))
        feature_dict = data_processor.process_fasta(
            fasta_path=tmp_fasta_path, alignment_dir=alignment_dir, template_info_save_dir=template_save_dir
        )
    elif len(seqs) == 1:
        tag = tags[0]
        seq = seqs[0]
        with open(tmp_fasta_path, "w") as fp:
            fp.write(f">{tag}\n{seq}")

        local_alignment_dir = os.path.join(alignment_dir, tag)
        feature_dict = data_processor.process_fasta(
            fasta_path=tmp_fasta_path,
            alignment_dir=local_alignment_dir,
            seqemb_mode=args.use_single_seq_mode,
        )
    else:
        with open(tmp_fasta_path, "w") as fp:
            fp.write("\n".join([f">{tag}\n{seq}" for tag, seq in zip(tags, seqs)]))
        feature_dict = data_processor.process_multiseq_fasta(
            fasta_path=tmp_fasta_path,
            super_alignment_dir=alignment_dir,
        )

    # Remove temporary FASTA file
    os.remove(tmp_fasta_path)

    return feature_dict


def list_files_with_extensions(dir, extensions):
    return [f for f in os.listdir(dir) if f.endswith(extensions)]


def main(args):
    # Create the output directory
    os.makedirs(args.output_dir, exist_ok=True)

    if args.config_preset.startswith("seq"):
        args.use_single_seq_mode = True

    config = model_config(
        args.config_preset,
        long_sequence_inference=args.long_sequence_inference,
        use_deepspeed_evoformer_attention=args.use_deepspeed_evoformer_attention,
    )

    if args.experiment_config_json:
        with open(args.experiment_config_json, "r") as f:
            custom_config_dict = json.load(f)
        config.update_from_flattened_dict(custom_config_dict)

    if args.experiment_config_json:
        with open(args.experiment_config_json, "r") as f:
            custom_config_dict = json.load(f)
        config.update_from_flattened_dict(custom_config_dict)

    if args.trace_model:
        if not config.data.predict.fixed_size:
            raise ValueError("Tracing requires that fixed_size mode be enabled in the config")

    is_multimer = "multimer" in args.config_preset

    if is_multimer:
        template_featurizer = templates.HmmsearchHitFeaturizer(
            mmcif_dir=args.template_mmcif_dir,
            max_template_date=args.max_template_date,
            max_hits=config.data.predict.max_templates,
            kalign_binary_path=args.kalign_binary_path,
            release_dates_path=args.release_dates_path,
            obsolete_pdbs_path=args.obsolete_pdbs_path,
        )
    else:
        template_featurizer = templates.HhsearchHitFeaturizer(
            mmcif_dir=args.template_mmcif_dir,
            max_template_date=args.max_template_date,
            max_hits=config.data.predict.max_templates,
            kalign_binary_path=args.kalign_binary_path,
            release_dates_path=args.release_dates_path,
            obsolete_pdbs_path=args.obsolete_pdbs_path,
        )

    data_processor = data_pipeline.DataPipeline(
        template_featurizer=template_featurizer, enable_template=args.enable_template
    )

    if is_multimer:
        data_processor = data_pipeline.DataPipelineMultimer(
            monomer_data_pipeline=data_processor,
        )

    output_dir_base = args.output_dir
    random_seed = args.data_random_seed
    if random_seed is None:
        random_seed = random.randrange(2**32)

    np.random.seed(random_seed)
    torch.manual_seed(random_seed + 1)

    feature_processor = feature_pipeline.FeaturePipeline(config.data)
    if not os.path.exists(output_dir_base):
        os.makedirs(output_dir_base)
    if args.use_precomputed_alignments is None:
        alignment_dir = os.path.join(output_dir_base, "alignments")
    else:
        alignment_dir = args.use_precomputed_alignments

    tag_list = []
    seq_list = []
    file_list = []
    for fasta_file in list_files_with_extensions(args.fasta_dir, (".fasta", ".fa")):
        # Gather input sequences
        fasta_path = os.path.join(args.fasta_dir, fasta_file)
        with open(fasta_path, "r") as fp:
            data = fp.read()

        tags, seqs = parse_fasta(data)

        if not is_multimer and len(tags) != 1:
            print(
                f"{fasta_path} contains more than one sequence but " f"multimer mode is not enabled. Skipping..."
            )
            continue

        # assert len(tags) == len(set(tags)), "All FASTA tags must be unique"
        tag = "-".join(tags)

        tag_list.append((tag, tags))
        seq_list.append(seqs)
        file_list.append(fasta_file.split(".")[0])

    seq_sort_fn = lambda target: sum([len(s) for s in target[1]])
    sorted_targets = sorted(zip(tag_list, seq_list, file_list), key=seq_sort_fn)
    feature_dicts = {}
    model_generator = load_models_from_command_line(
        config, args.model_device, args.openfold_checkpoint_path, args.jax_param_path, args.output_dir
    )

    for model, output_directory in model_generator:
        cur_tracing_interval = 0
        for (tag, tags), seqs, file_name in sorted_targets:
            try:
                # output_name = f'{tag}_{args.config_preset}'
                # if args.output_postfix is not None:
                #     output_name = f'{output_name}_{args.output_postfix}'
                output_name = file_name
                final_output_dir = os.path.join(output_directory, args.config_preset, output_name)
                os.makedirs(final_output_dir, exist_ok=True)

                logger.info(f"Inference for {file_name} start.")
                # Does nothing if the alignments have already been computed
                precompute_alignments(tags, seqs, alignment_dir, args)

                feature_dict = feature_dicts.get(tag, None)
                if feature_dict is None:
                    feature_dict = generate_feature_dict(
                        tags, seqs, alignment_dir, data_processor, args, final_output_dir
                    )

                    if args.trace_model:
                        n = feature_dict["aatype"].shape[-2]
                        rounded_seqlen = round_up_seqlen(n)
                        feature_dict = pad_feature_dict_seq(
                            feature_dict,
                            rounded_seqlen,
                        )

                    feature_dicts[tag] = feature_dict

                processed_feature_dict = feature_processor.process_features(
                    feature_dict, mode="predict", is_multimer=is_multimer
                )

                processed_feature_dict = {
                    k: torch.as_tensor(v, device=args.model_device) for k, v in processed_feature_dict.items()
                }

                # print(seqs)
                # save msa and extra msa info
                total_msas = torch.tensor(feature_dict["msa"])
                ## [508, cancat_seq_length, recycle_num]
                msas = processed_feature_dict["true_msa"]
                ## [2048, cancat_seq_length, recycle_num]
                extra_msas = processed_feature_dict["extra_msa"]
                num_recycle = msas.shape[-1]

                seq_length_1, seq_length_2 = map(len, seqs)
                # if_homo = seqs[0] == seqs[1]
                if_homo = False

                get_msa_stats_ = partial(
                    get_msa_stats, seq_length_1=seq_length_1, seq_length_2=seq_length_2, if_homo=if_homo, num_recycle=num_recycle
                )

                msa_stats = pd.concat(
                    [
                        get_msa_stats_(all_msa=total_msas, prefix="total_"),
                        get_msa_stats_(all_msa=msas, prefix=""),
                        get_msa_stats_(all_msa=extra_msas, prefix="extra_"),
                    ],
                    axis=1,
                )

                # with open(os.path.join(
                #         final_output_dir, 'feature.pkl'
                #     ), "wb") as f:
                #     pickle.dump(feature_dict, f)

                # with open(os.path.join(
                #         final_output_dir, 'processed_feature.pkl'
                #     ), "wb") as f:
                #     pickle.dump(processed_feature_dict, f)

                if args.trace_model:
                    if rounded_seqlen > cur_tracing_interval:
                        logger.info(f"Tracing model at {rounded_seqlen} residues...")
                        t = time.perf_counter()
                        trace_model_(model, processed_feature_dict)
                        tracing_time = time.perf_counter() - t
                        logger.info(f"Tracing time: {tracing_time}")
                        cur_tracing_interval = rounded_seqlen

                out = run_model(model, processed_feature_dict, tag, args.output_dir)

                # Toss out the recycling dimensions --- we don't need them anymore
                processed_feature_dict = tensor_tree_map(lambda x: np.array(x[..., -1].cpu()), processed_feature_dict)
                out = tensor_tree_map(lambda x: np.array(x.cpu()), out)

                unrelaxed_protein = prep_output(
                    out,
                    processed_feature_dict,
                    feature_dict,
                    feature_processor,
                    args.config_preset,
                    args.multimer_ri_gap,
                    args.subtract_plddt,
                )

                unrelaxed_file_suffix = "unrelaxed.pdb"
                if args.cif_output:
                    unrelaxed_file_suffix = "unrelaxed.cif"
                unrelaxed_output_path = os.path.join(final_output_dir, unrelaxed_file_suffix)

                with open(unrelaxed_output_path, "w") as fp:
                    if args.cif_output:
                        fp.write(protein.to_modelcif(unrelaxed_protein))
                    else:
                        fp.write(protein.to_pdb(unrelaxed_protein))

                if not args.skip_relaxation:
                    # Relax the prediction.
                    logger.info(f"Running relaxation for {file_name}...")
                    relax_protein(
                        config,
                        args.model_device,
                        unrelaxed_protein,
                        output_directory,
                        output_name,
                        args.config_preset,
                        args.cif_output,
                    )

                if args.save_outputs:
                    output_metric_dir = os.path.join(final_output_dir, "analysis")
                    os.makedirs(output_metric_dir, exist_ok=True)
                    output_tmp_dir = os.path.join(final_output_dir, "tmp")
                    os.makedirs(output_tmp_dir, exist_ok=True)

                    # save metrics
                    output_metrics = {
                        k: out[k]
                        for k in ["plddt", "ptm_score", "iptm_score", "predicted_aligned_error", "num_recycles", "tm_logits"]
                    }
                    output_metrics["num_alignments"] = feature_dict["num_alignments"]
                    output_metrics["num_templates"] = feature_dict["num_templates"]
                    with open(os.path.join(output_metric_dir, "metric_dict.pkl"), "wb") as fp:
                        pickle.dump(output_metrics, fp, protocol=pickle.HIGHEST_PROTOCOL)

                    # save msa stats
                    msa_stats.iloc[: out["num_recycles"].item()].to_csv(
                        os.path.join(output_metric_dir, "msa_stats.csv")
                    )

                    # save raw msa info
                    raw_msa = {
                        "total_msa": total_msas,
                        "msa_recycle": msas[:, :, : out["num_recycles"].item()].cpu(),
                        "extra_msa_recycle": extra_msas[:, :, : out["num_recycles"].item()].cpu(),
                    }
                    with open(os.path.join(output_tmp_dir, "raw_msa.pkl"), "wb") as fp:
                        pickle.dump(raw_msa, fp)

                    logger.info(f"Output written to {final_output_dir}...")
                    logger.info(f"Inference for {file_name} finish.\n")
            except Exception as e:
                logger.error(f"Error happens for {file_name}")
                traceback.print_exc()
                print()
                continue

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "fasta_dir", type=str, help="Path to directory containing FASTA files, one sequence per file"
    )
    parser.add_argument(
        "template_mmcif_dir",
        type=str,
    )
    parser.add_argument(
        "--use_precomputed_alignments",
        type=str,
        default=None,
        help="""Path to alignment directory. If provided, alignment computation 
                is skipped and database path arguments are ignored.""",
    )
    parser.add_argument(
        "--use_single_seq_mode",
        action="store_true",
        default=False,
        help="""Use single sequence embeddings instead of MSAs.""",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=os.getcwd(),
        help="""Name of the directory in which to output the prediction""",
    )
    parser.add_argument(
        "--model_device",
        type=str,
        default="cpu",
        help="""Name of the device on which to run the model. Any valid torch
             device name is accepted (e.g. "cpu", "cuda:0")""",
    )
    parser.add_argument(
        "--config_preset",
        type=str,
        default="model_1",
        help="""Name of a model config preset defined in openfold/config.py""",
    )
    parser.add_argument(
        "--jax_param_path",
        type=str,
        default=None,
        help="""Path to JAX model parameters. If None, and openfold_checkpoint_path
             is also None, parameters are selected automatically according to 
             the model name from openfold/resources/params""",
    )
    parser.add_argument(
        "--openfold_checkpoint_path",
        type=str,
        default=None,
        help="""Path to OpenFold checkpoint. Can be either a DeepSpeed 
             checkpoint directory or a .pt file""",
    )
    parser.add_argument(
        "--save_outputs",
        action="store_true",
        default=False,
        help="Whether to save all model outputs, including embeddings, etc.",
    )
    parser.add_argument("--cpus", type=int, default=4, help="""Number of CPUs with which to run alignment tools""")
    parser.add_argument("--preset", type=str, default="full_dbs", choices=("reduced_dbs", "full_dbs"))
    parser.add_argument(
        "--output_postfix", type=str, default=None, help="""Postfix for output prediction filenames"""
    )
    parser.add_argument("--data_random_seed", type=int, default=None)
    parser.add_argument(
        "--skip_relaxation",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--multimer_ri_gap",
        type=int,
        default=200,
        help="""Residue index offset between multiple sequences, if provided""",
    )
    parser.add_argument(
        "--trace_model",
        action="store_true",
        default=False,
        help="""Whether to convert parts of each model to TorchScript.
                Significantly improves runtime at the cost of lengthy
                'compilation.' Useful for large batch jobs.""",
    )
    parser.add_argument(
        "--subtract_plddt",
        action="store_true",
        default=False,
        help=""""Whether to output (100 - pLDDT) in the B-factor column instead
                 of the pLDDT itself""",
    )
    parser.add_argument(
        "--long_sequence_inference",
        action="store_true",
        default=False,
        help="""enable options to reduce memory usage at the cost of speed, helps longer sequences fit into GPU memory, see the README for details""",
    )
    parser.add_argument(
        "--cif_output",
        action="store_true",
        default=False,
        help="Output predicted models in ModelCIF format instead of PDB format (default)",
    )
    parser.add_argument(
        "--experiment_config_json",
        default="",
        help="Path to a json file with custom config values to overwrite config setting",
    )
    parser.add_argument(
        "--use_deepspeed_evoformer_attention",
        action="store_true",
        default=False,
        help="Whether to use the DeepSpeed evoformer attention layer. Must have deepspeed installed in the environment.",
    )
    parser.add_argument(
        "--enable_template",
        action="store_true",
        default=False,
    )
    add_data_args(parser)
    args = parser.parse_args()

    if args.jax_param_path is None and args.openfold_checkpoint_path is None:
        args.jax_param_path = os.path.join(
            "openfold", "resources", "params", "params_" + args.config_preset + ".npz"
        )

    if args.model_device == "cpu" and torch.cuda.is_available():
        logging.warning(
            """The model is being run on CPU. Consider specifying 
            --model_device for better performance"""
        )

    main(args)
