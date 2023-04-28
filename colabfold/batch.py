from __future__ import annotations

import os
os.environ["TF_FORCE_UNIFIED_MEMORY"] = "1"
os.environ["XLA_PYTHON_CLIENT_MEM_FRACTION"] = "2.0"

import json
import logging
import math
import random
import sys
import time
import zipfile
import shutil
import pickle

from argparse import ArgumentParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, TYPE_CHECKING
from io import StringIO

import importlib_metadata
import numpy as np
import pandas
import scipy.special
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
import json
import seaborn as sns

try:
    import alphafold
except ModuleNotFoundError:
    raise RuntimeError(
        "\n\nalphafold is not installed. Please run `pip install colabfold[alphafold]`\n"
    )

from alphafold.common import protein, residue_constants

# delay imports of tensorflow, jax and numpy
# loading these for type checking only can take around 10 seconds just to show a CLI usage message
if TYPE_CHECKING:
    import haiku
    from alphafold.model import model
    from numpy import ndarray

from alphafold.common.protein import Protein
from alphafold.data import (
    feature_processing,
    msa_pairing,
    pipeline,
    pipeline_multimer,
    templates,
)
from alphafold.data.tools import hhsearch
from colabfold.citations import write_bibtex
from colabfold.download import default_data_dir, download_alphafold_params
from colabfold.utils import (
    ACCEPT_DEFAULT_TERMS,
    DEFAULT_API_SERVER,
    NO_GPU_FOUND,
    CIF_REVISION_DATE,
    get_commit,
    safe_filename,
    setup_logging,
    CFMMCIFIO,
)
from Bio.PDB import MMCIFParser, PDBParser, MMCIF2Dict

logger = logging.getLogger(__name__)
import jax
import jax.numpy as jnp
logging.getLogger('jax._src.lib.xla_bridge').addFilter(lambda _: False)

def patch_openmm():
    from simtk.openmm import app
    from simtk.unit import nanometers, sqrt

    # applied https://raw.githubusercontent.com/deepmind/alphafold/main/docker/openmm.patch
    # to OpenMM 7.5.1 (see PR https://github.com/openmm/openmm/pull/3203)
    # patch is licensed under CC-0
    # OpenMM is licensed under MIT and LGPL
    # fmt: off
    def createDisulfideBonds(self, positions):
        def isCyx(res):
            names = [atom.name for atom in res._atoms]
            return 'SG' in names and 'HG' not in names
        # This function is used to prevent multiple di-sulfide bonds from being
        # assigned to a given atom.
        def isDisulfideBonded(atom):
            for b in self._bonds:
                if (atom in b and b[0].name == 'SG' and
                    b[1].name == 'SG'):
                    return True

            return False

        cyx = [res for res in self.residues() if res.name == 'CYS' and isCyx(res)]
        atomNames = [[atom.name for atom in res._atoms] for res in cyx]
        for i in range(len(cyx)):
            sg1 = cyx[i]._atoms[atomNames[i].index('SG')]
            pos1 = positions[sg1.index]
            candidate_distance, candidate_atom = 0.3*nanometers, None
            for j in range(i):
                sg2 = cyx[j]._atoms[atomNames[j].index('SG')]
                pos2 = positions[sg2.index]
                delta = [x-y for (x,y) in zip(pos1, pos2)]
                distance = sqrt(delta[0]*delta[0] + delta[1]*delta[1] + delta[2]*delta[2])
                if distance < candidate_distance and not isDisulfideBonded(sg2):
                    candidate_distance = distance
                    candidate_atom = sg2
            # Assign bond to closest pair.
            if candidate_atom:
                self.addBond(sg1, candidate_atom)
    # fmt: on
    app.Topology.createDisulfideBonds = createDisulfideBonds


def mk_mock_template(
    query_sequence: Union[List[str], str], num_temp: int = 1
) -> Dict[str, Any]:
    ln = (
        len(query_sequence)
        if isinstance(query_sequence, str)
        else sum(len(s) for s in query_sequence)
    )
    output_templates_sequence = "A" * ln
    output_confidence_scores = np.full(ln, 1.0)

    templates_all_atom_positions = np.zeros(
        (ln, templates.residue_constants.atom_type_num, 3)
    )
    templates_all_atom_masks = np.zeros((ln, templates.residue_constants.atom_type_num))
    templates_aatype = templates.residue_constants.sequence_to_onehot(
        output_templates_sequence, templates.residue_constants.HHBLITS_AA_TO_ID
    )
    template_features = {
        "template_all_atom_positions": np.tile(
            templates_all_atom_positions[None], [num_temp, 1, 1, 1]
        ),
        "template_all_atom_masks": np.tile(
            templates_all_atom_masks[None], [num_temp, 1, 1]
        ),
        "template_sequence": [f"none".encode()] * num_temp,
        "template_aatype": np.tile(np.array(templates_aatype)[None], [num_temp, 1, 1]),
        "template_confidence_scores": np.tile(
            output_confidence_scores[None], [num_temp, 1]
        ),
        "template_domain_names": [f"none".encode()] * num_temp,
        "template_release_date": [f"none".encode()] * num_temp,
        "template_sum_probs": np.zeros([num_temp], dtype=np.float32),
    }
    return template_features


def mk_template(
    a3m_lines: str, template_path: str, query_sequence: str
) -> Dict[str, Any]:
    template_featurizer = templates.HhsearchHitFeaturizer(
        mmcif_dir=template_path,
        max_template_date="2100-01-01",
        max_hits=20,
        kalign_binary_path="kalign",
        release_dates_path=None,
        obsolete_pdbs_path=None,
    )

    hhsearch_pdb70_runner = hhsearch.HHSearch(
        binary_path="hhsearch", databases=[f"{template_path}/pdb70"]
    )

    hhsearch_result = hhsearch_pdb70_runner.query(a3m_lines)
    hhsearch_hits = pipeline.parsers.parse_hhr(hhsearch_result)
    templates_result = template_featurizer.get_templates(
        query_sequence=query_sequence, hits=hhsearch_hits
    )
    return dict(templates_result.features)


def validate_and_fix_mmcif(cif_file: Path):
    """validate presence of _entity_poly_seq in cif file and add revision_date if missing"""
    # check that required poly_seq and revision_date fields are present
    cif_dict = MMCIF2Dict.MMCIF2Dict(cif_file)
    required = [
        "_chem_comp.id",
        "_chem_comp.type",
        "_struct_asym.id",
        "_struct_asym.entity_id",
        "_entity_poly_seq.mon_id",
    ]
    for r in required:
        if r not in cif_dict:
            raise ValueError(f"mmCIF file {cif_file} is missing required field {r}.")
    if "_pdbx_audit_revision_history.revision_date" not in cif_dict:
        logger.info(
            f"Adding missing field revision_date to {cif_file}. Backing up original file to {cif_file}.bak."
        )
        shutil.copy2(cif_file, str(cif_file) + ".bak")
        with open(cif_file, "a") as f:
            f.write(CIF_REVISION_DATE)

def convert_pdb_to_mmcif(pdb_file: Path):
    """convert existing pdb files into mmcif with the required poly_seq and revision_date"""
    i = pdb_file.stem
    cif_file = pdb_file.parent.joinpath(f"{i}.cif")
    if cif_file.is_file():
        return
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure(i, pdb_file)
    cif_io = CFMMCIFIO()
    cif_io.set_structure(structure)
    cif_io.save(str(cif_file))

def mk_hhsearch_db(template_dir: str):
    template_path = Path(template_dir)

    cif_files = template_path.glob("*.cif")
    for cif_file in cif_files:
        validate_and_fix_mmcif(cif_file)

    pdb_files = template_path.glob("*.pdb")
    for pdb_file in pdb_files:
        convert_pdb_to_mmcif(pdb_file)

    pdb70_db_files = template_path.glob("pdb70*")
    for f in pdb70_db_files:
        os.remove(f)

    with open(template_path.joinpath("pdb70_a3m.ffdata"), "w") as a3m, open(
        template_path.joinpath("pdb70_cs219.ffindex"), "w"
    ) as cs219_index, open(
        template_path.joinpath("pdb70_a3m.ffindex"), "w"
    ) as a3m_index, open(
        template_path.joinpath("pdb70_cs219.ffdata"), "w"
    ) as cs219:
        n = 1000000
        index_offset = 0
        cif_files = template_path.glob("*.cif")
        for cif_file in cif_files:
            with open(cif_file) as f:
                cif_string = f.read()
            cif_fh = StringIO(cif_string)
            parser = MMCIFParser(QUIET=True)
            structure = parser.get_structure("none", cif_fh)
            models = list(structure.get_models())
            if len(models) != 1:
                raise ValueError(
                    f"Only single model PDBs are supported. Found {len(models)} models."
                )
            model = models[0]
            for chain in model:
                amino_acid_res = []
                for res in chain:
                    if res.id[2] != " ":
                        raise ValueError(
                            f"PDB contains an insertion code at chain {chain.id} and residue "
                            f"index {res.id[1]}. These are not supported."
                        )
                    amino_acid_res.append(
                        residue_constants.restype_3to1.get(res.resname, "X")
                    )

                protein_str = "".join(amino_acid_res)
                a3m_str = f">{cif_file.stem}_{chain.id}\n{protein_str}\n\0"
                a3m_str_len = len(a3m_str)
                a3m_index.write(f"{n}\t{index_offset}\t{a3m_str_len}\n")
                cs219_index.write(f"{n}\t{index_offset}\t{len(protein_str)}\n")
                index_offset += a3m_str_len
                a3m.write(a3m_str)
                cs219.write("\n\0")
                n += 1


def pad_input(
    input_features: model.features.FeatureDict,
    model_runner: model.RunModel,
    model_name: str,
    pad_len: int,
    use_templates: bool,
) -> model.features.FeatureDict:
    from colabfold.alphafold.msa import make_fixed_size

    model_config = model_runner.config
    eval_cfg = model_config.data.eval
    crop_feats = {k: [None] + v for k, v in dict(eval_cfg.feat).items()}

    max_msa_clusters = eval_cfg.max_msa_clusters
    max_extra_msa = model_config.data.common.max_extra_msa
    # templates models
    if (model_name == "model_1" or model_name == "model_2") and use_templates:
        pad_msa_clusters = max_msa_clusters - eval_cfg.max_templates
    else:
        pad_msa_clusters = max_msa_clusters

    max_msa_clusters = pad_msa_clusters

    # let's try pad (num_res + X)
    input_fix = make_fixed_size(
        input_features,
        crop_feats,
        msa_cluster_size=max_msa_clusters,  # true_msa (4, 512, 68)
        extra_msa_size=max_extra_msa,  # extra_msa (4, 5120, 68)
        num_res=pad_len,  # aatype (4, 68)
        num_templates=4,
    )  # template_mask (4, 4) second value
    return input_fix

def _jnp_to_np(output: Dict[str, Any]) -> Dict[str, Any]:
  """Recursively changes jax arrays to numpy arrays."""
  for k, v in output.items():
    if isinstance(v, dict):
      output[k] = _jnp_to_np(v)
    elif isinstance(v, jnp.ndarray):
      output[k] = np.array(v)
  return output

def class_to_np(c):
  class dict2obj():
    def __init__(self, d):
      for k,v in _jnp_to_np(d).items(): setattr(self, k, v)
  return dict2obj(c.__dict__)

def relax_me(pdb_filename=None, pdb_lines=None, pdb_obj=None, use_gpu=False):
    if "relax" not in dir():
        patch_openmm()
        from alphafold.common import residue_constants
        from alphafold.relax import relax

    if pdb_obj is None:        
        if pdb_lines is None:
            pdb_lines = Path(pdb_filename).read_text()
        pdb_obj = protein.from_pdb_string(pdb_lines)
    
    amber_relaxer = relax.AmberRelaxation(
        max_iterations=0,
        tolerance=2.39,
        stiffness=10.0,
        exclude_residues=[],
        max_outer_iterations=3,
        use_gpu=use_gpu)
    
    relaxed_pdb_lines, _, _ = amber_relaxer.process(prot=pdb_obj)
    return relaxed_pdb_lines

class file_manager:
    def __init__(self, prefix: str, result_dir: Path):
        self.prefix = prefix
        self.result_dir = result_dir
        self.tag = None
        self.files = {}
    
    def get(self, x: str, ext:str) -> Path:
        if self.tag not in self.files:
            self.files[self.tag] = []
        file = self.result_dir.joinpath(f"{self.prefix}_{x}_{self.tag}.{ext}")
        self.files[self.tag].append([x,ext,file])
        return file

    def set_tag(self, tag):
        self.tag = tag

def predict_structure(
    prefix: str,
    result_dir: Path,
    feature_dict: Dict[str, Any],
    is_complex: bool,
    use_templates: bool,
    sequences_lengths: List[int],
    pad_len: int,
    model_type: str,
    model_runner_and_params: List[Tuple[str, model.RunModel, haiku.Params]],
    num_relax: int = 0,
    rank_by: str = "auto",
    random_seed: int = 0,
    num_seeds: int = 1,
    stop_at_score: float = 100,
    prediction_callback: Callable[[Any, Any, Any, Any, Any], Any] = None,
    use_gpu_relax: bool = False,
    save_all: bool = False,
    save_single_representations: bool = False,
    save_pair_representations: bool = False,
    save_recycles: bool = False,
):
    """Predicts structure using AlphaFold for the given sequence."""

    mean_scores = []
    conf = []
    unrelaxed_pdb_lines = []
    prediction_times = []
    seq_len = sum(sequences_lengths)
    model_names = []
    files = file_manager(prefix, result_dir)
    for (model_name, model_runner, params) in model_runner_and_params:
        # swap params to avoid recompiling
        model_runner.params = params
        
        # iterate through random seeds
        for seed in range(random_seed, random_seed+num_seeds):
            tag = f"{model_type}_{model_name}_seed_{seed:03d}"
            model_names.append(tag)
            files.set_tag(tag)

            ########################
            # process inputs
            ########################
            processed_feature_dict = model_runner.process_features(feature_dict, random_seed=seed)            
            # pad inputs
            if "multimer" in model_type:
                # TODO: add multimer padding
                input_features = processed_feature_dict
                input_features["asym_id"] = input_features["asym_id"] - input_features["asym_id"][...,0]
            else:
                # TODO: move asym_id processing to "process_features"
                r = processed_feature_dict["aatype"].shape[0]
                processed_feature_dict["asym_id"] = np.tile(feature_dict["asym_id"],r).reshape(r,-1)
                input_features = pad_input(
                    processed_feature_dict,
                    model_runner,
                    model_name,
                    pad_len,
                    use_templates)

            ########################
            # predict
            ########################
            start = time.time()

            # monitor intermediate results
            def callback(prediction_result, recycles):
                print_line = ""
                for x,y in [["mean_plddt","pLDDT"],["ptm","pTM"],["iptm","ipTM"],["diff","tol"]]:
                  if x in prediction_result:
                    print_line += f" {y}={prediction_result[x]:.3g}"
                logger.info(f"{tag} recycle={recycles}{print_line}")

                if save_recycles:
                    result = _jnp_to_np(prediction_result)
                    final_atom_mask = result["structure_module"]["final_atom_mask"]
                    b_factors = result["plddt"][:, None] * final_atom_mask
                    unrelaxed_protein = protein.from_prediction(features=input_features,
                        result=result, b_factors=b_factors,
                        remove_leading_feature_dimension=("ptm" in model_type))
                    
                    unrelaxed_pdb_lines = protein.to_pdb(class_to_np(unrelaxed_protein))
                    files.get("unrelaxed",f"r{recycles}.pdb").write_text(unrelaxed_pdb_lines)
                
                    if save_all:
                        with files.get("all",f"r{recycles}.pickle").open("wb") as handle:
                            pickle.dump(result, handle)

            prediction_result, recycles = \
            model_runner.predict(input_features, random_seed=seed, callback=callback)
            prediction_result = _jnp_to_np(prediction_result)
            prediction_times.append(time.time() - start)

            ########################
            # parse results
            ########################
            # summary metrics
            mean_scores.append(prediction_result["ranking_confidence"])         
            print_line = ""
            conf.append({})
            for x,y in [["mean_plddt","pLDDT"],["ptm","pTM"],["iptm","ipTM"]]:
              if x in prediction_result:
                print_line += f" {y}={prediction_result[x]:.3g}"
                conf[-1][x] = float(prediction_result[x])
            conf[-1]["print_line"] = print_line
            logger.info(f"{tag} took {prediction_times[-1]:.1f}s ({recycles} recycles)")

            # create protein object
            final_atom_mask = prediction_result["structure_module"]["final_atom_mask"]
            b_factors = prediction_result["plddt"][:, None] * final_atom_mask
            unrelaxed_protein = protein.from_prediction(
                features=input_features,
                result=prediction_result,
                b_factors=b_factors,
                remove_leading_feature_dimension=("ptm" in model_type))
            unrelaxed_protein = class_to_np(unrelaxed_protein)

            # callback for visualization
            if prediction_callback is not None:
                prediction_callback(unrelaxed_protein, sequences_lengths,
                                    prediction_result, input_features, (tag, False))

            #########################
            # save results
            #########################      

            # save pdb
            protein_lines = protein.to_pdb(unrelaxed_protein)
            files.get("unrelaxed","pdb").write_text(protein_lines)
            unrelaxed_pdb_lines.append(protein_lines)
            
            distmat_dir=f"{files.result_dir}/{files.prefix}_distmat"
            os.makedirs(f'{distmat_dir}',exist_ok=True)

            bin_num=64
            probs=scipy.special.softmax(np.asarray( 
                prediction_result["distogram"]['logits'])[:seq_len,:seq_len,:],axis=-1)
            bin_edges=np.linspace(
                    2.3125,  21.6875,bin_num - 1)
            step=bin_edges[1]-bin_edges[0]
            bin_centers=bin_edges-step/2.0
            bin_centers=np.concatenate([bin_centers, [bin_centers[-1] + step]], axis=0)
            mean=np.sum(probs * bin_centers, axis=-1)
            sq_centers=np.square(bin_centers)
            #std=np.sqrt(np.sum(probs * sq_centers, axis=-1)-mean*mean)
            #mean=mean[...,None]
            std=np.sqrt(np.sum(np.square(mean-bin_centers)*probs,axis=-1))
            
            prob_7=np.log10(np.sum(probs[...,:16], axis=-1))
            prob_7=pd.DataFrame(prob_7).rename(columns={i:i+1 for i in range(seq_len)})
            prob_7.index=[i+1 for i in range(seq_len)]
            ax=sns.heatmap(prob_7,cmap='YlGnBu')
            plt.xlabel('residue i') 
            plt.ylabel('residue j') 
            cbar = ax.collections[0].colorbar
            cbar.set_label(r'log$_{10}$p(distance < 7Å)')
            plt.savefig(f'{distmat_dir}/{tag}_7A_prob_log.png',dpi=800)
            plt.close()

            prob_14=np.log10(np.sum(probs[...,:32], axis=-1))
            prob_14=pd.DataFrame(prob_14).rename(columns={i:i+1 for i in range(seq_len)})
            prob_14.index=[i+1 for i in range(seq_len)]
            ax=sns.heatmap(prob_14,cmap='YlGnBu')
            plt.xlabel('residue i') 
            plt.ylabel('residue j') 
            cbar = ax.collections[0].colorbar
            cbar.set_label(r'log$_{10}$p(distance < 12Å)')
            plt.savefig(f'{distmat_dir}/{tag}_12A_prob_log.png',dpi=800)
            plt.close()

            prob_14=np.sum(probs[...,:32], axis=-1)
            prob_14=pd.DataFrame(prob_14).rename(columns={i:i+1 for i in range(seq_len)})
            prob_14.index=[i+1 for i in range(seq_len)]
            prob_14.to_csv(f'{distmat_dir}/{tag}_12A_prob.csv')
            ax=sns.heatmap(prob_14,cmap='YlGnBu')
            plt.xlabel('residue i') 
            plt.ylabel('residue j') 
            cbar = ax.collections[0].colorbar
            cbar.set_label(r'p(distance < 12Å)')
            plt.savefig(f'{distmat_dir}/{tag}_12A_prob.png',dpi=800)
            plt.close()

            mean=pd.DataFrame(mean).rename(columns={i:i+1 for i in range(seq_len)})
            mean.index=[i+1 for i in range(seq_len)]
            std=pd.DataFrame(std).rename(columns={i:i+1 for i in range(seq_len)})
            std.index=[i+1 for i in range(seq_len)]
            mean.to_csv(f'{distmat_dir}/{tag}_mean.csv')
            std.to_csv(f'{distmat_dir}/{tag}_std.csv')
            np.save(f'{distmat_dir}/{tag}_prob_distributions.npy',probs)

            ax=sns.heatmap(mean,cmap='YlGnBu')
            plt.xlabel('residue i') 
            plt.ylabel('residue j') 
            cbar = ax.collections[0].colorbar
            cbar.set_label(r'distance (Å)')
            plt.savefig(f'{distmat_dir}/{tag}_distmat.png',dpi=800)
            plt.close()

            # save raw outputs
            if save_all:
                os.makedirs(f'{distmat_dir}/{tag}_distribution',exist_ok=True)
                for i in range(seq_len):
                    for j in range(i+1,seq_len):
                        prob=pd.DataFrame(probs[i][j]).rename(columns={0:'probability'})
                        prob.index=[f'dist<{bin_edges[0]}']+[f'{bin_edges[i]}<dist<{bin_edges[i+1]}' for i in range(0,bin_num-2)]+[f'dist>{bin_edges[-1]}']
                        prob.to_csv(f'{distmat_dir}/{tag}_distribution/{i+1}_{j+1}.csv')
                with files.get("all","pickle").open("wb") as handle:
                    pickle.dump(prediction_result, handle)
            if save_single_representations:
                np.save(files.get("single_repr","npy"),
                    prediction_result["prev"]["prev_msa_first_row"])
            if save_pair_representations:
                np.save(files.get("pair_repr","npy"),
                    prediction_result["prev"]["prev_pair"])

            # write an easy-to-use format (pAE and pLDDT)
            with files.get("scores","json").open("w") as handle:
                pae = prediction_result["predicted_aligned_error"][:seq_len,:seq_len]
                plddt = prediction_result["plddt"][:seq_len]            
                scores = {
                    "max_pae": pae.max().astype(float).item(),
                    "pae":   np.around(pae.astype(float), 2).tolist(),
                    "plddt": np.around(plddt.astype(float), 2).tolist(),
                }
                for k in ["ptm","iptm"]:
                  if k in conf[-1]: scores[k] = np.around(conf[-1][k], 2).item()
                json.dump(scores, handle)

            # early stop criteria fulfilled
            if mean_scores[-1] > stop_at_score: break

        # early stop criteria fulfilled
        if mean_scores[-1] > stop_at_score: break

    ###################################################
    # rerank models based on predicted confidence
    ###################################################
    
    rank, metric = [],[]
    result_files = []
    logger.info(f"reranking models by '{rank_by}' metric")
    model_rank = np.array(mean_scores).argsort()[::-1]
    for n, key in enumerate(model_rank):
        metric.append(conf[key])
        tag = model_names[key]
        files.set_tag(tag)
        # save relaxed pdb
        if n < num_relax:
            start = time.time()
            pdb_lines = relax_me(pdb_lines=unrelaxed_pdb_lines[key], use_gpu=use_gpu_relax)
            files.get("relaxed","pdb").write_text(pdb_lines)            
            logger.info(f"Relaxation took {(time.time() - start):.1f}s")

        # rename files to include rank
        new_tag = f"rank_{(n+1):03d}_{tag}"
        rank.append(new_tag)
        logger.info(f"{new_tag}{metric[-1]['print_line']}")
        for x, ext, file in files.files[tag]:
            new_file = result_dir.joinpath(f"{prefix}_{x}_{new_tag}.{ext}")
            file.rename(new_file)
            result_files.append(new_file)
        
    return {"rank":rank,
            "metric":metric,
            "result_files":result_files}

def parse_fasta(fasta_string: str) -> Tuple[List[str], List[str]]:
    """Parses FASTA string and returns list of strings with amino-acid sequences.

    Arguments:
      fasta_string: The string contents of a FASTA file.

    Returns:
      A tuple of two lists:
      * A list of sequences.
      * A list of sequence descriptions taken from the comment lines. In the
        same order as the sequences.
    """
    sequences = []
    descriptions = []
    index = -1
    for line in fasta_string.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        if line.startswith(">"):
            index += 1
            descriptions.append(line[1:])  # Remove the '>' at the beginning.
            sequences.append("")
            continue
        elif not line:
            continue  # Skip blank lines.
        sequences[index] += line

    return sequences, descriptions

def get_queries(
    input_path: Union[str, Path], sort_queries_by: str = "length"
) -> Tuple[List[Tuple[str, str, Optional[List[str]]]], bool]:
    """Reads a directory of fasta files, a single fasta file or a csv file and returns a tuple
    of job name, sequence and the optional a3m lines"""

    input_path = Path(input_path)
    if not input_path.exists():
        raise OSError(f"{input_path} could not be found")

    if input_path.is_file():
        if input_path.suffix == ".csv" or input_path.suffix == ".tsv":
            sep = "\t" if input_path.suffix == ".tsv" else ","
            df = pandas.read_csv(input_path, sep=sep)
            assert "id" in df.columns and "sequence" in df.columns
            queries = [
                (seq_id, sequence.upper().split(":"), None)
                for seq_id, sequence in df[["id", "sequence"]].itertuples(index=False)
            ]
            for i in range(len(queries)):
                if len(queries[i][1]) == 1:
                    queries[i] = (queries[i][0], queries[i][1][0], None)
        elif input_path.suffix == ".a3m":
            (seqs, header) = parse_fasta(input_path.read_text())
            if len(seqs) == 0:
                raise ValueError(f"{input_path} is empty")
            query_sequence = seqs[0]
            # Use a list so we can easily extend this to multiple msas later
            a3m_lines = [input_path.read_text()]
            queries = [(input_path.stem, query_sequence, a3m_lines)]
        elif input_path.suffix in [".fasta", ".faa", ".fa"]:
            (sequences, headers) = parse_fasta(input_path.read_text())
            queries = []
            for sequence, header in zip(sequences, headers):
                sequence = sequence.upper()
                if sequence.count(":") == 0:
                    # Single sequence
                    queries.append((header, sequence, None))
                else:
                    # Complex mode
                    queries.append((header, sequence.upper().split(":"), None))
        else:
            raise ValueError(f"Unknown file format {input_path.suffix}")
    else:
        assert input_path.is_dir(), "Expected either an input file or a input directory"
        queries = []
        for file in sorted(input_path.iterdir()):
            if not file.is_file():
                continue
            if file.suffix.lower() not in [".a3m", ".fasta", ".faa"]:
                logger.warning(f"non-fasta/a3m file in input directory: {file}")
                continue
            (seqs, header) = parse_fasta(file.read_text())
            if len(seqs) == 0:
                logger.error(f"{file} is empty")
                continue
            query_sequence = seqs[0]
            if len(seqs) > 1 and file.suffix in [".fasta", ".faa", ".fa"]:
                logger.warning(
                    f"More than one sequence in {file}, ignoring all but the first sequence"
                )

            if file.suffix.lower() == ".a3m":
                a3m_lines = [file.read_text()]
                queries.append((file.stem, query_sequence.upper(), a3m_lines))
            else:
                if query_sequence.count(":") == 0:
                    # Single sequence
                    queries.append((file.stem, query_sequence, None))
                else:
                    # Complex mode
                    queries.append((file.stem, query_sequence.upper().split(":"), None))

    # sort by seq. len
    if sort_queries_by == "length":
        queries.sort(key=lambda t: len(t[1]))
    elif sort_queries_by == "random":
        random.shuffle(queries)
    is_complex = False
    for job_number, (raw_jobname, query_sequence, a3m_lines) in enumerate(queries):
        if isinstance(query_sequence, list):
            is_complex = True
            break
        if a3m_lines is not None and a3m_lines[0].startswith("#"):
            a3m_line = a3m_lines[0].splitlines()[0]
            tab_sep_entries = a3m_line[1:].split("\t")
            if len(tab_sep_entries) == 2:
                query_seq_len = tab_sep_entries[0].split(",")
                query_seq_len = list(map(int, query_seq_len))
                query_seqs_cardinality = tab_sep_entries[1].split(",")
                query_seqs_cardinality = list(map(int, query_seqs_cardinality))
                is_single_protein = (
                    True
                    if len(query_seq_len) == 1 and query_seqs_cardinality[0] == 1
                    else False
                )
                if not is_single_protein:
                    is_complex = True
                    break
    return queries, is_complex

def pair_sequences(
    a3m_lines: List[str], query_sequences: List[str], query_cardinality: List[int]
) -> str:
    a3m_line_paired = [""] * len(a3m_lines[0].splitlines())
    for n, seq in enumerate(query_sequences):
        lines = a3m_lines[n].splitlines()
        for i, line in enumerate(lines):
            if line.startswith(">"):
                if n != 0:
                    line = line.replace(">", "\t", 1)
                a3m_line_paired[i] = a3m_line_paired[i] + line
            else:
                a3m_line_paired[i] = a3m_line_paired[i] + line * query_cardinality[n]
    return "\n".join(a3m_line_paired)

def pad_sequences(
    a3m_lines: List[str], query_sequences: List[str], query_cardinality: List[int]
) -> str:
    _blank_seq = [
        ("-" * len(seq))
        for n, seq in enumerate(query_sequences)
        for _ in range(query_cardinality[n])
    ]
    a3m_lines_combined = []
    pos = 0
    for n, seq in enumerate(query_sequences):
        for j in range(0, query_cardinality[n]):
            lines = a3m_lines[n].split("\n")
            for a3m_line in lines:
                if len(a3m_line) == 0:
                    continue
                if a3m_line.startswith(">"):
                    a3m_lines_combined.append(a3m_line)
                else:
                    a3m_lines_combined.append(
                        "".join(_blank_seq[:pos] + [a3m_line] + _blank_seq[pos + 1 :])
                    )
            pos += 1
    return "\n".join(a3m_lines_combined)


def get_msa_and_templates(
    jobname: str,
    query_sequences: Union[str, List[str]],
    result_dir: Path,
    msa_mode: str,
    use_templates: bool,
    custom_template_path: str,
    pair_mode: str,
    host_url: str = DEFAULT_API_SERVER,
) -> Tuple[
    Optional[List[str]], Optional[List[str]], List[str], List[int], List[Dict[str, Any]]
]:
    from colabfold.colabfold import run_mmseqs2

    use_env = msa_mode == "mmseqs2_uniref_env"
    if isinstance(query_sequences, str): query_sequences = [query_sequences]

    # remove duplicates before searching
    query_seqs_unique = []
    for x in query_sequences:
        if x not in query_seqs_unique:
            query_seqs_unique.append(x)

    # determine how many times is each sequence is used
    query_seqs_cardinality = [0] * len(query_seqs_unique)
    for seq in query_sequences:
        seq_idx = query_seqs_unique.index(seq)
        query_seqs_cardinality[seq_idx] += 1

    # get template features
    template_features = []
    if use_templates:
        a3m_lines_mmseqs2, template_paths = run_mmseqs2(
            query_seqs_unique,
            str(result_dir.joinpath(jobname)),
            use_env,
            use_templates=True,
            host_url=host_url,
        )
        if custom_template_path is not None:
            template_paths = {}
            for index in range(0, len(query_seqs_unique)):
                template_paths[index] = custom_template_path
        if template_paths is None:
            logger.info("No template detected")
            for index in range(0, len(query_seqs_unique)):
                template_feature = mk_mock_template(query_seqs_unique[index])
                template_features.append(template_feature)
        else:
            for index in range(0, len(query_seqs_unique)):
                if template_paths[index] is not None:
                    template_feature = mk_template(
                        a3m_lines_mmseqs2[index],
                        template_paths[index],
                        query_seqs_unique[index],
                    )
                    if len(template_feature["template_domain_names"]) == 0:
                        template_feature = mk_mock_template(query_seqs_unique[index])
                        logger.info(f"Sequence {index} found no templates")
                    else:
                        logger.info(
                            f"Sequence {index} found templates: {template_feature['template_domain_names'].astype(str).tolist()}"
                        )
                else:
                    template_feature = mk_mock_template(query_seqs_unique[index])
                    logger.info(f"Sequence {index} found no templates")

                template_features.append(template_feature)
    else:
        for index in range(0, len(query_seqs_unique)):
            template_feature = mk_mock_template(query_seqs_unique[index])
            template_features.append(template_feature)

    if len(query_sequences) == 1:
        pair_mode = "none"

    if pair_mode == "none" or pair_mode == "unpaired" or pair_mode == "unpaired_paired":
        if msa_mode == "single_sequence":
            a3m_lines = []
            num = 101
            for i, seq in enumerate(query_seqs_unique):
                a3m_lines.append(f">{num + i}\n{seq}")
        else:
            # find normal a3ms
            a3m_lines = run_mmseqs2(
                query_seqs_unique,
                str(result_dir.joinpath(jobname)),
                use_env,
                use_pairing=False,
                host_url=host_url,
            )
    else:
        a3m_lines = None

    if msa_mode != "single_sequence" and (
        pair_mode == "paired" or pair_mode == "unpaired_paired"
    ):
        # find paired a3m if not a homooligomers
        if len(query_seqs_unique) > 1:
            paired_a3m_lines = run_mmseqs2(
                query_seqs_unique,
                str(result_dir.joinpath(jobname)),
                use_env,
                use_pairing=True,
                host_url=host_url,
            )
        else:
            # homooligomers
            num = 101
            paired_a3m_lines = []
            for i in range(0, query_seqs_cardinality[0]):
                paired_a3m_lines.append(f">{num+i}\n{query_seqs_unique[0]}\n")
    else:
        paired_a3m_lines = None

    return (
        a3m_lines,
        paired_a3m_lines,
        query_seqs_unique,
        query_seqs_cardinality,
        template_features,
    )

def build_monomer_feature(
    sequence: str, unpaired_msa: str, template_features: Dict[str, Any]
):
    msa = pipeline.parsers.parse_a3m(unpaired_msa)
    # gather features
    return {
        **pipeline.make_sequence_features(
            sequence=sequence, description="none", num_res=len(sequence)
        ),
        **pipeline.make_msa_features([msa]),
        **template_features,
    }

def build_multimer_feature(paired_msa: str) -> Dict[str, ndarray]:
    parsed_paired_msa = pipeline.parsers.parse_a3m(paired_msa)
    return {
        f"{k}_all_seq": v
        for k, v in pipeline.make_msa_features([parsed_paired_msa]).items()
    }

def process_multimer_features(
    features_for_chain: Dict[str, Dict[str, ndarray]]
) -> Dict[str, ndarray]:
    all_chain_features = {}
    for chain_id, chain_features in features_for_chain.items():
        all_chain_features[chain_id] = pipeline_multimer.convert_monomer_features(
            chain_features, chain_id
        )

    all_chain_features = pipeline_multimer.add_assembly_features(all_chain_features)
    # np_example = feature_processing.pair_and_merge(
    #    all_chain_features=all_chain_features, is_prokaryote=is_prokaryote)
    feature_processing.process_unmerged_features(all_chain_features)
    np_chains_list = list(all_chain_features.values())
    # noinspection PyProtectedMember
    pair_msa_sequences = not feature_processing._is_homomer_or_monomer(np_chains_list)
    chains = list(np_chains_list)
    chain_keys = chains[0].keys()
    updated_chains = []
    for chain_num, chain in enumerate(chains):
        new_chain = {k: v for k, v in chain.items() if "_all_seq" not in k}
        for feature_name in chain_keys:
            if feature_name.endswith("_all_seq"):
                feats_padded = msa_pairing.pad_features(
                    chain[feature_name], feature_name
                )
                new_chain[feature_name] = feats_padded
        new_chain["num_alignments_all_seq"] = np.asarray(
            len(np_chains_list[chain_num]["msa_all_seq"])
        )
        updated_chains.append(new_chain)
    np_chains_list = updated_chains
    np_chains_list = feature_processing.crop_chains(
        np_chains_list,
        msa_crop_size=feature_processing.MSA_CROP_SIZE,
        pair_msa_sequences=pair_msa_sequences,
        max_templates=feature_processing.MAX_TEMPLATES,
    )
    # merge_chain_features crashes if there are additional features only present in one chain
    # remove all features that are not present in all chains
    common_features = set([*np_chains_list[0]]).intersection(*np_chains_list)
    np_chains_list = [
        {key: value for (key, value) in chain.items() if key in common_features}
        for chain in np_chains_list
    ]
    np_example = feature_processing.msa_pairing.merge_chain_features(
        np_chains_list=np_chains_list,
        pair_msa_sequences=pair_msa_sequences,
        max_templates=feature_processing.MAX_TEMPLATES,
    )
    np_example = feature_processing.process_final(np_example)

    # Pad MSA to avoid zero-sized extra_msa.
    np_example = pipeline_multimer.pad_msa(np_example, min_num_seq=512)
    return np_example


def pair_msa(
    query_seqs_unique: List[str],
    query_seqs_cardinality: List[int],
    paired_msa: Optional[List[str]],
    unpaired_msa: Optional[List[str]],
) -> str:
    if paired_msa is None and unpaired_msa is not None:
        a3m_lines = pad_sequences(
            unpaired_msa, query_seqs_unique, query_seqs_cardinality
        )
    elif paired_msa is not None and unpaired_msa is not None:
        a3m_lines = (
            pair_sequences(paired_msa, query_seqs_unique, query_seqs_cardinality)
            + "\n"
            + pad_sequences(unpaired_msa, query_seqs_unique, query_seqs_cardinality)
        )
    elif paired_msa is not None and unpaired_msa is None:
        a3m_lines = pair_sequences(
            paired_msa, query_seqs_unique, query_seqs_cardinality
        )
    else:
        raise ValueError(f"Invalid pairing")
    return a3m_lines


def generate_input_feature(
    query_seqs_unique: List[str],
    query_seqs_cardinality: List[int],
    unpaired_msa: List[str],
    paired_msa: List[str],
    template_features: List[Dict[str, Any]],
    is_complex: bool,
    model_type: str,
) -> Tuple[Dict[str, Any], Dict[str, str]]:

    input_feature = {}
    domain_names = {}
    if is_complex and "ptm" in model_type:

        full_sequence = ""
        Ls = []
        for sequence_index, sequence in enumerate(query_seqs_unique):
            for cardinality in range(0, query_seqs_cardinality[sequence_index]):
                full_sequence += sequence
                Ls.append(len(sequence))

        # bugfix
        a3m_lines = f">0\n{full_sequence}\n"
        a3m_lines += pair_msa(query_seqs_unique, query_seqs_cardinality, paired_msa, unpaired_msa)        

        input_feature = build_monomer_feature(full_sequence, a3m_lines, mk_mock_template(full_sequence))
        input_feature["residue_index"] = np.concatenate([np.arange(L) for L in Ls])
        input_feature["asym_id"] = np.concatenate([np.full(L,n) for n,L in enumerate(Ls)])
        if any(
            [
                template != b"none"
                for i in template_features
                for template in i["template_domain_names"]
            ]
        ):
            logger.warning(
                "alphafold2_ptm complex does not consider templates. Chose multimer model-type for template support."
            )

    else:
        features_for_chain = {}
        chain_cnt = 0
        # for each unique sequence
        for sequence_index, sequence in enumerate(query_seqs_unique):
            
            # get unpaired msa
            if unpaired_msa is None:
                input_msa = f">{101 + sequence_index}\n{sequence}"
            else:
                input_msa = unpaired_msa[sequence_index]

            feature_dict = build_monomer_feature(
                sequence, input_msa, template_features[sequence_index])

            if "multimer" in model_type:
                # get paired msa
                if paired_msa is None:
                    input_msa = f">{101 + sequence_index}\n{sequence}"
                else:
                    input_msa = paired_msa[sequence_index]
                feature_dict.update(build_multimer_feature(input_msa))

            # for each copy
            for cardinality in range(0, query_seqs_cardinality[sequence_index]):
                features_for_chain[protein.PDB_CHAIN_IDS[chain_cnt]] = feature_dict
                chain_cnt += 1

        if "ptm" in model_type:
            input_feature = features_for_chain[protein.PDB_CHAIN_IDS[0]]
            input_feature["asym_id"] = np.zeros(input_feature["aatype"].shape[0],dtype=int)
            domain_names = {
                protein.PDB_CHAIN_IDS[0]: [
                    name.decode("UTF-8")
                    for name in input_feature["template_domain_names"]
                    if name != b"none"
                ]
            }
        else:
            # combine features across all chains
            input_feature = process_multimer_features(features_for_chain)
            domain_names = {
                chain: [
                    name.decode("UTF-8")
                    for name in feature["template_domain_names"]
                    if name != b"none"
                ]
                for (chain, feature) in features_for_chain.items()
            }
    return (input_feature, domain_names)

def unserialize_msa(
    a3m_lines: List[str], query_sequence: Union[List[str], str]
) -> Tuple[
    Optional[List[str]],
    Optional[List[str]],
    List[str],
    List[int],
    List[Dict[str, Any]],
]:
    a3m_lines = a3m_lines[0].replace("\x00", "").splitlines()
    if not a3m_lines[0].startswith("#") or len(a3m_lines[0][1:].split("\t")) != 2:
        assert isinstance(query_sequence, str)
        return (
            ["\n".join(a3m_lines)],
            None,
            [query_sequence],
            [1],
            [mk_mock_template(query_sequence)],
        )

    if len(a3m_lines) < 3:
        raise ValueError(f"Unknown file format a3m")
    tab_sep_entries = a3m_lines[0][1:].split("\t")
    query_seq_len = tab_sep_entries[0].split(",")
    query_seq_len = list(map(int, query_seq_len))
    query_seqs_cardinality = tab_sep_entries[1].split(",")
    query_seqs_cardinality = list(map(int, query_seqs_cardinality))
    is_homooligomer = (
        True if len(query_seq_len) == 1 and query_seqs_cardinality[0] > 1 else False
    )
    is_single_protein = (
        True if len(query_seq_len) == 1 and query_seqs_cardinality[0] == 1 else False
    )
    query_seqs_unique = []
    prev_query_start = 0
    # we store the a3m with cardinality of 1
    for n, query_len in enumerate(query_seq_len):
        query_seqs_unique.append(
            a3m_lines[2][prev_query_start : prev_query_start + query_len]
        )
        prev_query_start += query_len
    paired_msa = [""] * len(query_seq_len)
    unpaired_msa = [""] * len(query_seq_len)
    already_in = dict()
    for i in range(1, len(a3m_lines), 2):
        header = a3m_lines[i]
        seq = a3m_lines[i + 1]
        if (header, seq) in already_in:
            continue
        already_in[(header, seq)] = 1
        has_amino_acid = [False] * len(query_seq_len)
        seqs_line = []
        prev_pos = 0
        for n, query_len in enumerate(query_seq_len):
            paired_seq = ""
            curr_seq_len = 0
            for pos in range(prev_pos, len(seq)):
                if curr_seq_len == query_len:
                    prev_pos = pos
                    break
                paired_seq += seq[pos]
                if seq[pos].islower():
                    continue
                if seq[pos] != "-":
                    has_amino_acid[n] = True
                curr_seq_len += 1
            seqs_line.append(paired_seq)

        # is sequence is paired add them to output
        if (
            not is_single_protein
            and not is_homooligomer
            and sum(has_amino_acid) == len(query_seq_len)
        ):
            header_no_faster = header.replace(">", "")
            header_no_faster_split = header_no_faster.split("\t")
            for j in range(0, len(seqs_line)):
                paired_msa[j] += ">" + header_no_faster_split[j] + "\n"
                paired_msa[j] += seqs_line[j] + "\n"
        else:
            for j, seq in enumerate(seqs_line):
                if has_amino_acid[j]:
                    unpaired_msa[j] += header + "\n"
                    unpaired_msa[j] += seq + "\n"
    if is_homooligomer:
        # homooligomers
        num = 101
        paired_msa = [""] * query_seqs_cardinality[0]
        for i in range(0, query_seqs_cardinality[0]):
            paired_msa[i] = ">" + str(num + i) + "\n" + query_seqs_unique[0] + "\n"
    if is_single_protein:
        paired_msa = None
    template_features = []
    for query_seq in query_seqs_unique:
        template_feature = mk_mock_template(query_seq)
        template_features.append(template_feature)

    return (
        unpaired_msa,
        paired_msa,
        query_seqs_unique,
        query_seqs_cardinality,
        template_features,
    )

def msa_to_str(
    unpaired_msa: List[str],
    paired_msa: List[str],
    query_seqs_unique: List[str],
    query_seqs_cardinality: List[int],
) -> str:
    msa = "#" + ",".join(map(str, map(len, query_seqs_unique))) + "\t"
    msa += ",".join(map(str, query_seqs_cardinality)) + "\n"
    # build msa with cardinality of 1, it makes it easier to parse and manipulate
    query_seqs_cardinality = [1 for _ in query_seqs_cardinality]
    msa += pair_msa(query_seqs_unique, query_seqs_cardinality, paired_msa, unpaired_msa)
    return msa

def run(
    queries: List[Tuple[str, Union[str, List[str]], Optional[List[str]]]],
    result_dir: Union[str, Path],
    num_models: int,
    is_complex: bool,
    num_recycles: Optional[int] = None,
    recycle_early_stop_tolerance: Optional[float] = None,
    model_order: List[int] = [1,2,3,4,5],
    num_ensemble: int = 1,
    model_type: str = "auto",
    msa_mode: str = "mmseqs2_uniref_env",
    use_templates: bool = False,
    custom_template_path: str = None,
    num_relax: int = 0,
    keep_existing_results: bool = True,
    rank_by: str = "auto",
    pair_mode: str = "unpaired_paired",
    data_dir: Union[str, Path] = default_data_dir,
    host_url: str = DEFAULT_API_SERVER,
    random_seed: int = 0,
    num_seeds: int = 1,
    recompile_padding: Union[int, float] = 10,
    zip_results: bool = False,
    prediction_callback: Callable[[Any, Any, Any, Any, Any], Any] = None,
    save_single_representations: bool = False,
    save_pair_representations: bool = False,
    save_all: bool = False,
    save_recycles: bool = False,
    use_dropout: bool = False,
    use_gpu_relax: bool = False,
    stop_at_score: float = 100,
    dpi: int = 200,
    max_seq: Optional[int] = None,
    max_extra_seq: Optional[int] = None,
    use_cluster_profile: bool = True,
    feature_dict_callback: Callable[[Any], Any] = None,
    **kwargs
):
    # check what device is available
    try:
        # check if TPU is available
        import jax.tools.colab_tpu
        jax.tools.colab_tpu.setup_tpu()
        logger.info('Running on TPU')
        DEVICE = "tpu"
        use_gpu_relax = False
    except:
        if jax.local_devices()[0].platform == 'cpu':
            logger.info("WARNING: no GPU detected, will be using CPU")
            DEVICE = "cpu"
            use_gpu_relax = False
        else:
            import tensorflow as tf
            logger.info('Running on GPU')
            DEVICE = "gpu"
            # disable GPU on tensorflow
            tf.config.set_visible_devices([], 'GPU')

    from alphafold.notebooks.notebook_utils import get_pae_json
    from colabfold.alphafold.models import load_models_and_params
    from colabfold.colabfold import plot_paes, plot_plddts
    from colabfold.plot import plot_msa_v2

    data_dir = Path(data_dir)
    result_dir = Path(result_dir)
    result_dir.mkdir(exist_ok=True)
    model_type = set_model_type(is_complex, model_type)

    # determine model extension
    if   model_type == "alphafold2_multimer_v1": model_suffix = "_multimer"
    elif model_type == "alphafold2_multimer_v2": model_suffix = "_multimer_v2"
    elif model_type == "alphafold2_multimer_v3": model_suffix = "_multimer_v3"
    elif model_type == "alphafold2_ptm":         model_suffix = "_ptm"
    else: raise ValueError(f"Unknown model_type {model_type}")

    # backward-compatibility with old options
    old_names = {"MMseqs2 (UniRef+Environmental)":"mmseqs2_uniref_env",
                 "MMseqs2 (UniRef only)":"mmseqs2_uniref",
                 "unpaired+paired":"unpaired_paired"}
    msa_mode   = old_names.get(msa_mode,msa_mode)
    pair_mode  = old_names.get(pair_mode,pair_mode)
    feature_dict_callback = kwargs.pop("input_features_callback", feature_dict_callback)
    use_dropout           = kwargs.pop("training", use_dropout)
    use_fuse              = kwargs.pop("use_fuse", True)
    use_bfloat16          = kwargs.pop("use_bfloat16", True)
    max_msa               = kwargs.pop("max_msa",None)
    if max_msa is not None:
        max_seq, max_extra_seq = [int(x) for x in max_msa.split(":")]

    if kwargs.pop("use_amber", False) and num_relax == 0: 
        num_relax = num_models * num_seeds

    if len(kwargs) > 0:
        print(f"WARNING: the following options are not being used: {kwargs}")

    # decide how to rank outputs
    if rank_by == "auto":
      rank_by = "multimer" if is_complex else "plddt"

    # Record the parameters of this run
    config = {
        "num_queries": len(queries),
        "use_templates": use_templates,
        "num_relax": num_relax,
        "msa_mode": msa_mode,
        "model_type": model_type,
        "num_models": num_models,
        "num_recycles": num_recycles,
        "recycle_early_stop_tolerance": recycle_early_stop_tolerance,
        "num_ensemble": num_ensemble,
        "model_order": model_order,
        "keep_existing_results": keep_existing_results,
        "rank_by": rank_by,
        "max_seq": max_seq,
        "max_extra_seq": max_extra_seq,
        "pair_mode": pair_mode,
        "host_url": host_url,
        "stop_at_score": stop_at_score,
        "random_seed": random_seed,
        "num_seeds": num_seeds,
        "recompile_padding": recompile_padding,
        "commit": get_commit(),
        "use_dropout": use_dropout,
        "use_cluster_profile": use_cluster_profile,
        "use_fuse": use_fuse,
        "use_bfloat16":use_bfloat16,
        "version": importlib_metadata.version("colabfold"),
    }
    config_out_file = result_dir.joinpath("config.json")
    config_out_file.write_text(json.dumps(config, indent=4))
    use_env = "env" in msa_mode
    use_msa = "mmseqs2" in msa_mode
    use_amber = num_relax > 0

    bibtex_file = write_bibtex(
        model_type, use_msa, use_env, use_templates, use_amber, result_dir
    )

    if custom_template_path is not None:
        mk_hhsearch_db(custom_template_path)

    # get max length (for padding purposes)
    max_len = 0
    for _, query_sequence, _ in queries:
        L = len("".join(query_sequence))
        if L > max_len: max_len = L

    pad_len = 0
    ranks, metrics = [],[]
    first_job = True
    for job_number, (raw_jobname, query_sequence, a3m_lines) in enumerate(queries):
        jobname = safe_filename(raw_jobname)
        
        #######################################
        # check if job has already finished
        #######################################
        # In the colab version and with --zip we know we're done when a zip file has been written
        result_zip = result_dir.joinpath(jobname).with_suffix(".result.zip")
        if keep_existing_results and result_zip.is_file():
            logger.info(f"Skipping {jobname} (result.zip)")
            continue
        # In the local version we use a marker file
        is_done_marker = result_dir.joinpath(jobname + ".done.txt")
        if keep_existing_results and is_done_marker.is_file():
            logger.info(f"Skipping {jobname} (already done)")
            continue

        total_len = len("".join(query_sequence))
        logger.info(f"Query {job_number + 1}/{len(queries)}: {jobname} (length {total_len})")

        ###########################################
        # generate MSA (a3m_lines) and templates
        ###########################################
        try:
            if use_templates or a3m_lines is None:
                (unpaired_msa, paired_msa, query_seqs_unique, query_seqs_cardinality, template_features) \
                = get_msa_and_templates(jobname, query_sequence, result_dir, msa_mode, use_templates, 
                    custom_template_path, pair_mode, host_url)
            if a3m_lines is not None:
                (unpaired_msa, paired_msa, query_seqs_unique, query_seqs_cardinality, template_features_) \
                = unserialize_msa(a3m_lines, query_sequence)
                if not use_templates: template_features = template_features_

            # save a3m
            msa = msa_to_str(unpaired_msa, paired_msa, query_seqs_unique, query_seqs_cardinality)
            result_dir.joinpath(f"{jobname}.a3m").write_text(msa)
                
        except Exception as e:
            logger.exception(f"Could not get MSA/templates for {jobname}: {e}")
            continue
        
        #######################
        # generate features
        #######################
        try:
            (feature_dict, domain_names) \
            = generate_input_feature(query_seqs_unique, query_seqs_cardinality, unpaired_msa, paired_msa,
                                     template_features, is_complex, model_type)
            
            # to allow display of MSA info during colab/chimera run (thanks tomgoddard)
            if feature_dict_callback is not None:
                feature_dict_callback(feature_dict)
        
        except Exception as e:
            logger.exception(f"Could not generate input features {jobname}: {e}")
            continue
        
        ######################
        # predict structures
        ######################
        try:
            # get list of lengths
            query_sequence_len_array = sum([[len(x)] * y 
                for x,y in zip(query_seqs_unique,query_seqs_cardinality)],[])
            
            # decide how much to pad (to avoid recompiling)
            if total_len > pad_len:
                if isinstance(recompile_padding, float):
                    pad_len = math.ceil(total_len * recompile_padding)
                else:
                    pad_len = total_len + recompile_padding
                pad_len = min(pad_len, max_len)
                logger.info(f"Padding length to {pad_len}")
                            
            # prep model and params
            if first_job:
                # if one job input adjust max settings
                if len(queries) == 1 or msa_mode == "single_sequence":
                    # get number of sequences
                    if msa_mode == "single_sequence":
                        num_seqs = 1
                        if "ptm" in model_type and is_complex:
                            num_seqs += len(query_sequence_len_array)
                    else:                    
                        if "msa_mask" in feature_dict:
                            num_seqs = int(sum(feature_dict["msa_mask"].max(-1) == 1))
                        else:
                            num_seqs = int(len(feature_dict["msa"]))

                    # get max settings
                    # 512 5120  = alphafold (models 1,3,4)
                    # 512 1024  = alphafold (models 2,5)
                    # 508 2048  = alphafold-multimer (v3, models 1,2,3)
                    # 508 1152  = alphafold-multimer (v3, models 4,5)
                    # 252 1152  = alphafold-multimer (v1, v2)
                    set_if = lambda x,y: y if x is None else x
                    if model_type in ["alphafold2_multimer_v1","alphafold2_multimer_v2"]:
                        (max_seq, max_extra_seq) = (set_if(max_seq,252), set_if(max_extra_seq,1152))
                    elif model_type == "alphafold2_multimer_v3":
                        (max_seq, max_extra_seq) = (set_if(max_seq,508), set_if(max_extra_seq,2048))
                    else:
                        (max_seq, max_extra_seq) = (set_if(max_seq,512), set_if(max_extra_seq,5120))
                        if use_templates: num_seqs = num_seqs + 4
                    
                    # adjust max settings
                    max_seq = min(num_seqs, max_seq)
                    max_extra_seq = max(min(num_seqs - max_seq, max_extra_seq), 1)
                    logger.info(f"Setting max_seq={max_seq}, max_extra_seq={max_extra_seq}")

                model_runner_and_params = load_models_and_params(
                    num_models=num_models,
                    use_templates=use_templates,
                    num_recycles=num_recycles,
                    num_ensemble=num_ensemble,
                    model_order=model_order,
                    model_suffix=model_suffix,
                    data_dir=data_dir,
                    stop_at_score=stop_at_score,
                    rank_by=rank_by,
                    use_dropout=use_dropout,
                    max_seq=max_seq,
                    max_extra_seq=max_extra_seq,
                    use_cluster_profile=use_cluster_profile,
                    recycle_early_stop_tolerance=recycle_early_stop_tolerance,
                    use_fuse=use_fuse,
                    use_bfloat16=use_bfloat16,
                )
                first_job = False

            results = predict_structure(
                prefix=jobname,
                result_dir=result_dir,
                feature_dict=feature_dict,
                is_complex=is_complex,
                use_templates=use_templates,
                sequences_lengths=query_sequence_len_array,
                pad_len=pad_len,
                model_type=model_type,
                model_runner_and_params=model_runner_and_params,
                num_relax=num_relax,
                rank_by=rank_by,
                stop_at_score=stop_at_score,
                prediction_callback=prediction_callback,
                use_gpu_relax=use_gpu_relax,
                random_seed=random_seed,
                num_seeds=num_seeds,
                save_all=save_all,
                save_single_representations=save_single_representations,
                save_pair_representations=save_pair_representations,
                save_recycles=save_recycles,
            )
            result_files = results["result_files"]
            ranks.append(results["rank"])
            metrics.append(results["metric"])

        except RuntimeError as e:
            # This normally happens on OOM. TODO: Filter for the specific OOM error message
            logger.error(f"Could not predict {jobname}. Not Enough GPU memory? {e}")
            continue

        ###############
        # save plots
        ###############

        # make msa plot
        msa_plot = plot_msa_v2(feature_dict, dpi=dpi)
        coverage_png = result_dir.joinpath(f"{jobname}_coverage.png")
        msa_plot.savefig(str(coverage_png), bbox_inches='tight')
        msa_plot.close()
        result_files.append(coverage_png)

        # load the scores
        scores = []
        for r in results["rank"][:5]:
            scores_file = result_dir.joinpath(f"{jobname}_scores_{r}.json")
            with scores_file.open("r") as handle:
                scores.append(json.load(handle))
        
        # write alphafold-db format (pAE)
        af_pae_file = result_dir.joinpath(f"{jobname}_predicted_aligned_error_v1.json")
        af_pae_file.write_text(json.dumps({
            "predicted_aligned_error":scores[0]["pae"],
            "max_predicted_aligned_error":scores[0]["max_pae"]}))
        result_files.append(af_pae_file)
        
        # make pAE plots
        paes_plot = plot_paes([np.asarray(x["pae"]) for x in scores],
            Ls=query_sequence_len_array, dpi=dpi)
        pae_png = result_dir.joinpath(f"{jobname}_pae.png")
        paes_plot.savefig(str(pae_png), bbox_inches='tight')
        paes_plot.close()
        result_files.append(pae_png)

        # make pLDDT plot
        plddt_plot = plot_plddts([np.asarray(x["plddt"]) for x in scores],
            Ls=query_sequence_len_array, dpi=dpi)
        plddt_png = result_dir.joinpath(f"{jobname}_plddt.png")
        plddt_plot.savefig(str(plddt_png), bbox_inches='tight')
        plddt_plot.close()
        result_files.append(plddt_png)

        if use_templates:
            templates_file = result_dir.joinpath(f"{jobname}_template_domain_names.json")
            templates_file.write_text(json.dumps(domain_names))
            result_files.append(templates_file)

        result_files.append(result_dir.joinpath(jobname + ".a3m"))
        result_files += [bibtex_file, config_out_file]

        if zip_results:
            with zipfile.ZipFile(result_zip, "w") as result_zip:
                for file in result_files:
                    result_zip.write(file, arcname=file.name)
            
            # Delete only after the zip was successful, and also not the bibtex and config because we need those again
            for file in result_files[:-2]:
                file.unlink()
        else:
            is_done_marker.touch()

    logger.info("Done")
    return {"rank":ranks,"metric":metrics}

def set_model_type(is_complex: bool, model_type: str) -> str:
    # backward-compatibility with old options
    old_names = {"AlphaFold2-multimer-v1":"alphafold2_multimer_v1",
                 "AlphaFold2-multimer-v2":"alphafold2_multimer_v2",
                 "AlphaFold2-multimer-v3":"alphafold2_multimer_v3",
                 "AlphaFold2-ptm":"alphafold2_ptm"}
    model_type = old_names.get(model_type, model_type)
    if model_type == "auto" and is_complex:
        model_type = "alphafold2_multimer_v3"
    elif model_type == "auto" and not is_complex:
        model_type = "alphafold2_ptm"
    return model_type

def main():
    parser = ArgumentParser()
    parser.add_argument(
        "input",
        default="input",
        help="Can be one of the following: "
        "Directory with fasta/a3m files, a csv/tsv file, a fasta file or an a3m file",
    )
    parser.add_argument("results", help="Directory to write the results to")

    # Main performance parameter
    parser.add_argument(
        "--stop-at-score",
        help="Compute models until plddt (single chain) or ptmscore (complex) > threshold is reached. "
        "This can make colabfold much faster by only running the first model for easy queries.",
        type=float,
        default=100,
    )

    parser.add_argument(
        "--num-recycle",
        help="Number of prediction recycles."
        "Increasing recycles can improve the quality but slows down the prediction.",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--recycle-early-stop-tolerance",
        help="Specify convergence criteria."
        "Run until the distance between recycles is within specified value.",
        type=float,
        default=None,
    )

    parser.add_argument(
        "--num-ensemble",
        help="Number of ensembles."
        "The trunk of the network is run multiple times with different random choices for the MSA cluster centers.",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--num-seeds",
        help="Number of seeds to try. Will iterate from range(random_seed, random_seed+num_seeds)."
        ".",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--random-seed",
        help="Changing the seed for the random number generator can result in different structure predictions.",
        type=int,
        default=0,
    )

    parser.add_argument("--num-models", type=int, default=5, choices=[1, 2, 3, 4, 5])
    parser.add_argument(
        "--recompile-padding",
        type=int,
        default=10,
        help="Whenever the input length changes, the model needs to be recompiled."
        "We pad sequences by specified length, so we can e.g. compute sequence from length 100 to 110 without recompiling."
        "The prediction will become marginally slower for the longer input, "
        "but overall performance increases due to not recompiling. "
        "Set to 0 to disable.",
    )
    parser.add_argument("--model-order", default="1,2,3,4,5", type=str)
    parser.add_argument("--host-url", default=DEFAULT_API_SERVER)
    parser.add_argument("--data")
    parser.add_argument(
        "--msa-mode",
        default="mmseqs2_uniref_env",
        choices=[
            "mmseqs2_uniref_env",
            "mmseqs2_uniref",
            "single_sequence",
        ],
        help="Using an a3m file as input overwrites this option",
    )
    parser.add_argument(
        "--model-type",
        help="predict strucutre/complex using the following model."
        'Auto will pick "alphafold2_ptm" for structure predictions and "alphafold2_multimer_v3" for complexes.',
        type=str,
        default="auto",
        choices=[
            "auto",
            "alphafold2_ptm",
            "alphafold2_multimer_v1",
            "alphafold2_multimer_v2",
            "alphafold2_multimer_v3",
        ],
    )
    parser.add_argument(
        "--amber",
        default=False,
        action="store_true",
        help="Use amber for structure refinement."
        "To control number of top ranked structures are relaxed set --num-relax.",
    )
    parser.add_argument(
        "--num-relax",
        help="specify how many of the top ranked structures to relax using amber.",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--templates", default=False, action="store_true", help="Use templates from pdb"
    )
    parser.add_argument(
        "--custom-template-path",
        type=str,
        default=None,
        help="Directory with pdb files to be used as input",
    )
    parser.add_argument(
        "--rank",
        help="rank models by auto, plddt or ptmscore",
        type=str,
        default="auto",
        choices=["auto", "plddt", "ptm", "iptm", "multimer"],
    )
    parser.add_argument(
        "--pair-mode",
        help="rank models by auto, unpaired, paired, unpaired_paired",
        type=str,
        default="unpaired_paired",
        choices=["unpaired", "paired", "unpaired_paired"],
    )
    parser.add_argument(
        "--sort-queries-by",
        help="sort queries by: none, length, random",
        type=str,
        default="length",
        choices=["none", "length", "random"],
    )
    parser.add_argument(
        "--save-single-representations",
        default=False,
        action="store_true",
        help="saves the single representation embeddings of all models",
    )
    parser.add_argument(
        "--save-pair-representations",
        default=False,
        action="store_true",
        help="saves the pair representation embeddings of all models",
    )
    parser.add_argument(
        "--use-dropout",
        default=False,
        action="store_true",
        help="activate dropouts during inference to sample from uncertainity of the models",
    )
    parser.add_argument(
        "--max-seq",
        help="number of sequence clusters to use",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--max-extra-seq",
        help="number of extra sequences to use",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--max-msa",
        help="defines: `max-seq:max-extra-seq` number of sequences to use",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--disable-cluster-profile",
        default=False,
        action="store_true",
        help="EXPERIMENTAL: for multimer models, disable cluster profiles",
    )
    parser.add_argument(
        "--zip",
        default=False,
        action="store_true",
        help="zip all results into one <jobname>.result.zip and delete the original files",
    )
    parser.add_argument(
        "--use-gpu-relax",
        default=False,
        action="store_true",
        help="run amber on GPU instead of CPU",
    )
    parser.add_argument(
        "--save-all",
        default=False,
        action="store_true",
        help="save ALL raw outputs from model to a pickle file",
    )
    parser.add_argument(
        "--save-recycles",
        default=False,
        action="store_true",
        help="save all intermediate predictions at each recycle",
    )
    parser.add_argument(
        "--overwrite-existing-results", default=False, action="store_true"
    )

    args = parser.parse_args()

    setup_logging(Path(args.results).joinpath("log.txt"))

    version = importlib_metadata.version("colabfold")
    commit = get_commit()
    if commit:
        version += f" ({commit})"

    logger.info(f"Running colabfold {version}")

    data_dir = Path(args.data or default_data_dir)

    queries, is_complex = get_queries(args.input, args.sort_queries_by)
    model_type = set_model_type(is_complex, args.model_type)
        
    download_alphafold_params(model_type, data_dir)
    uses_api = any((query[2] is None for query in queries))
    if uses_api and args.host_url == DEFAULT_API_SERVER:
        print(ACCEPT_DEFAULT_TERMS, file=sys.stderr)

    model_order = [int(i) for i in args.model_order.split(",")]

    assert args.recompile_padding >= 0, "Can't apply negative padding"

    # backward compatibility
    if args.amber and args.num_relax == 0:
        args.num_relax = args.num_models * args.num_seeds

    run(
        queries=queries,
        result_dir=args.results,
        use_templates=args.templates,
        custom_template_path=args.custom_template_path,
        num_relax=args.num_relax,
        msa_mode=args.msa_mode,
        model_type=model_type,
        num_models=args.num_models,
        num_recycles=args.num_recycle,
        recycle_early_stop_tolerance=args.recycle_early_stop_tolerance,
        num_ensemble=args.num_ensemble,
        model_order=model_order,
        is_complex=is_complex,
        keep_existing_results=not args.overwrite_existing_results,
        rank_by=args.rank,
        pair_mode=args.pair_mode,
        data_dir=data_dir,
        host_url=args.host_url,
        random_seed=args.random_seed,
        num_seeds=args.num_seeds,
        stop_at_score=args.stop_at_score,
        recompile_padding=args.recompile_padding,
        zip_results=args.zip,
        save_single_representations=args.save_single_representations,
        save_pair_representations=args.save_pair_representations,
        use_dropout=args.use_dropout,
        max_seq=args.max_seq,
        max_extra_seq=args.max_extra_seq,
        max_msa=args.max_msa,
        use_cluster_profile=not args.disable_cluster_profile,
        use_gpu_relax = args.use_gpu_relax,
        save_all=args.save_all,
        save_recycles=args.save_recycles,
    )

if __name__ == "__main__":
    main()
