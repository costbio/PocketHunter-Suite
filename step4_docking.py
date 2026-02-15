import pandas as pd
from prody import *
import os
import tqdm
import glob
import subprocess
import seaborn as sns
import scipy.cluster.hierarchy as sch
import numpy as np
from openbabel import pybel
import re
import logging
from config import Config

logger = logging.getLogger(__name__)

# CONVERTING

def pdb_to_pdbqt(pdb_path, pdbqt_path, pH=7.4):
    """
    Convert a PDB file to a PDBQT file needed by docking programs of the AutoDock family.

    Parameters
    ----------
    pdb_path: str or pathlib.Path
        Path to input PDB file.
    pdbqt_path: str or pathlib.path
        Path to output PDBQT file.
    pH: float
        Protonation at given pH.
    """
    molecule = list(pybel.readfile("pdb", str(pdb_path)))[0]
    # add hydrogens at given pH
    molecule.OBMol.CorrectForPH(pH)
    molecule.addh()
    # add partial charges to each atom
    for atom in molecule.atoms:
        atom.OBAtom.GetPartialCharge()
    molecule.write("pdbqt", str(pdbqt_path), overwrite=True)
    return pdbqt_path


def calc_box(pdb_path, pocket_res_list):
    syst = parsePDB(pdb_path)
    pocket_res_list = pocket_res_list.split()
    pocket_res_list = [el for el in pocket_res_list if len(el) >= 2]
    chains = np.unique([el[0] for el in pocket_res_list])
    selection_string = ""
    for chain in chains:
        resnums = [el[2:] for el in pocket_res_list if el.startswith(chain)]
        resnums_string = ' '.join(resnums)
        selection_string = selection_string+f"chain {chain} and resnum {resnums_string} or "

    selection_string = selection_string.rstrip(" or ")
    syst_box = syst.select(selection_string)
    box_coords = syst_box.getCoords()
    box_center = calcCenter(box_coords)
    box_min = np.min(box_coords,axis=0)
    box_max = np.max(box_coords,axis=0)
    return box_center, box_min, box_max


def run_smina(
    ligand_path, protein_path, out_path, pocket_center, pocket_size, smina_exe, num_poses=10, exhaustiveness=8, log_dir=None
):
    """
    Perform docking with Smina.

    Parameters
    ----------
    ligand_path: str or pathlib.Path
        Path to ligand PDBQT file that should be docked.
    protein_path: str or pathlib.Path
        Path to protein PDBQT file that should be docked to.
    out_path: str or pathlib.Path
        Path to which docking poses should be saved, SDF or PDB format.
    pocket_center: iterable of float or int
        Coordinates defining the center of the binding site.
    pocket_size: iterable of float or int
        Lengths of edges defining the binding site.
    num_poses: int
        Maximum number of poses to generate.
    exhaustiveness: int
        Accuracy of docking calculations.
    log_dir: str or pathlib.Path, optional
        Directory to write smina debug log. If None, uses the same directory as out_path.

    Returns
    -------
    output_text: str
        The output of the Smina calculation.
    """
    # Determine log file path
    if log_dir is None:
        log_dir = os.path.dirname(str(out_path))
    log_file = os.path.join(str(log_dir), 'smina_debug.log')

    try:
        result = subprocess.run(
            [
                smina_exe,
                "--ligand",
                str(ligand_path),
                "--receptor",
                str(protein_path),
                "--out",
                str(out_path),
                "--center_x",
                str(pocket_center[0]),
                "--center_y",
                str(pocket_center[1]),
                "--center_z",
                str(pocket_center[2]),
                "--size_x",
                str(pocket_size[0]),
                "--size_y",
                str(pocket_size[1]),
                "--size_z",
                str(pocket_size[2]),
                "--num_modes",
                str(num_poses),
                "--exhaustiveness",
                str(exhaustiveness),
                '--log', log_file
            ],
            check=True,
            capture_output=True,
            text=True,  # needed to capture output text
        )
        return result.stdout, result.stderr
    except subprocess.CalledProcessError as e:
        logger.error(f"Error running smina: {e}")
        logger.error(f"Command: {e.cmd}")
        logger.error(f"Return code: {e.returncode}")
        logger.error(f"Output: {e.output}")
        logger.error(f"Error output: {e.stderr}")
        raise


def parse_smina_log(text):
    # Extracting the relevant lines using regular expressions
    pattern = r'(\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)'
    matches = re.findall(pattern, text)

    if not matches:
        logger.warning(f"No docking poses found in SMINA output (text length: {len(text)})")

    # Creating a DataFrame from the matches
    columns = ['mode', 'affinity (kcal/mol)', 'rmsd l.b.', 'rmsd u.b.']
    data = pd.DataFrame(matches, columns=columns)

    # Convert appropriate columns to numeric types
    if not data.empty:
        data['mode'] = data['mode'].astype(int)
        data['affinity (kcal/mol)'] = data['affinity (kcal/mol)'].astype(float)
        data['rmsd l.b.'] = data['rmsd l.b.'].astype(float)
        data['rmsd u.b.'] = data['rmsd u.b.'].astype(float)

    # Return the DataFrame
    return data


def dock_ensemble(df_rep_pockets, ligand_folder, smina_exe, out_folder, num_poses=10, exhaustiveness=8, ph_value=7.4, box_size_x=20.0, box_size_y=20.0, box_size_z=20.0, pdb_source_dir=None):
    """
    Perform ensemble docking of ligands against multiple receptor pockets.

    Parameters
    ----------
    df_rep_pockets : pandas.DataFrame
        DataFrame containing representative pockets with 'File name' and 'residues' columns.
    ligand_folder : str
        Path to folder containing ligand PDBQT files.
    smina_exe : str
        Path to smina executable.
    out_folder : str
        Path to output folder.
    num_poses : int
        Maximum number of poses to generate per docking.
    exhaustiveness : int
        Accuracy of docking calculations.
    ph_value : float
        pH for protonation.
    box_size_x, box_size_y, box_size_z : float
        Docking box dimensions in Angstroms.
    pdb_source_dir : str, optional
        Directory containing the source PDB files. If None, will search in results directory.
    """
    list_outputs = list()

    for row in df_rep_pockets.iterrows():
        # Prepare receptor
        receptor_pdb_pred = row[1]['File name']

        # Extract base filename (remove _predictions suffix)
        if receptor_pdb_pred.endswith('_predictions'):
            receptor_pdb = receptor_pdb_pred[:-12]  # Remove '_predictions'
        else:
            receptor_pdb = receptor_pdb_pred

        # Construct path to actual PDB file
        if pdb_source_dir:
            # Use provided source directory
            receptor_pdb_path = os.path.join(pdb_source_dir, receptor_pdb)
        else:
            # Fallback: search in results directory (legacy behavior)
            results_dir = str(Config.RESULTS_DIR)
            extract_dirs = [d for d in os.listdir(results_dir) if d.startswith('extract_') and os.path.isdir(os.path.join(results_dir, d))]
            if not extract_dirs:
                raise FileNotFoundError("No extract directories found in results. Please provide pdb_source_dir parameter.")

            # Use the most recent extract directory
            extract_dirs.sort()
            extract_dir = os.path.join(results_dir, extract_dirs[-1], 'pdbs')
            receptor_pdb_path = os.path.join(extract_dir, receptor_pdb)

        # Verify the file exists
        if not os.path.exists(receptor_pdb_path):
            raise FileNotFoundError(f"PDB file not found: {receptor_pdb_path}")
        
        # Take only protein parts
        syst = parsePDB(receptor_pdb_path)
        protein = syst.select('protein')
        protein_pdb = os.path.join(out_folder, os.path.basename(receptor_pdb))
        writePDB(protein_pdb, protein)
        receptor_pdbqt = os.path.join(protein_pdb[:-4]+".pdbqt")
        pdb_to_pdbqt(protein_pdb, receptor_pdbqt, pH=ph_value)
        logger.info(f"Prepared {receptor_pdbqt}")

        # Calculate box center from residues, but use custom box size
        box_center, box_min, box_max = calc_box(protein_pdb, row[1]['residues'])
        box_size = [box_size_x, box_size_y, box_size_z]
        
        # Run smina docking
        docking_output_folder = protein_pdb[:-4]+'_smina'
        if not os.path.exists(docking_output_folder):
            os.makedirs(docking_output_folder)

        ligands = glob.glob(ligand_folder+'/*.pdbqt')
        if not ligands:
            raise FileNotFoundError(f"No PDBQT ligand files found in {ligand_folder}")

        for lig_path in tqdm.tqdm(ligands):
            out_path = os.path.join(docking_output_folder,os.path.basename(lig_path)[:-6]+'_smina.sdf')
            output, stderr = run_smina(lig_path, receptor_pdbqt, out_path, box_center, box_size, smina_exe, num_poses=num_poses, exhaustiveness=exhaustiveness, log_dir=docking_output_folder)
            df_output = parse_smina_log(output)
            df_output['library'] = ligand_folder
            df_output['ligand'] = os.path.basename(lig_path)[:-6]
            df_output['receptor'] = os.path.basename(receptor_pdb)
            df_output['receptor_path'] = receptor_pdbqt
            df_output['receptor_pdb_path'] = protein_pdb
            df_output['output_sdf'] = out_path
            list_outputs.append(df_output)

    if not list_outputs:
        raise ValueError("No docking results generated. Check that ligands and receptors are valid.")

    df_outputs = pd.concat(list_outputs, axis=0, ignore_index=True)
    return df_outputs










