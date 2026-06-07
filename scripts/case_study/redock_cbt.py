"""
Step 1-4 — Dock CBT into:
  - all cluster-1 representatives (cryptic-pocket arm)
  - 5 cluster-0 representatives (orthosteric-site negative control)
  - apo 1PZO (self-docking ground-truth control)

Reuses the pipeline's step4_docking helpers verbatim so every dock uses
the same smina flags (vinardo, exhaustiveness=8, num_modes=10) and the
same box-padding/clamp rules.

Output: results/case_study_tem1_redock/scores.csv + per-receptor SDFs.
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from prody import parsePDB, writePDB

# /app inside worker container
APP = Path("/app")
sys.path.insert(0, str(APP))
from step4_docking import calc_box, pdb_to_pdbqt, parse_smina_log, run_smina
from tasks import _normalize_sdf_elements  # MDL V2000 uppercase-element fix

FP_DIR = APP / "results/find_pockets_20260523_212540_ce8948a0"
CL_DIR = APP / "results/cluster_20260523_213431_67e98d92/pocket_clusters"
OUT = APP / "results/case_study_tem1_redock"

CRYSTAL_PDB = OUT / "crystal/1pzo.pdb"
CRYSTAL_APO_PDB = OUT / "crystal/1pzo_apo.pdb"
CRYSTAL_CBT_PDB = OUT / "crystal/crystal_CBT.pdb"
CRYSTAL_CBT_SDF = OUT / "crystal/crystal_CBT.sdf"
LIGAND_SDF = OUT / "ligand/1pzo_B_CBT.sdf"
LIGAND_PDBQT = OUT / "ligand/CBT.pdbqt"

SMINA_KW = dict(num_poses=10, exhaustiveness=8, scoring_function="vinardo")
PAD_EACH_SIDE = 4.0
BOX_MIN = 10.0
BOX_MAX = 50.0


def box_size_from_extents(box_min: np.ndarray, box_max: np.ndarray) -> list[float]:
    raw = (box_max - box_min) + 2 * PAD_EACH_SIDE
    return [float(min(max(v, BOX_MIN), BOX_MAX)) for v in raw]


def prepare_crystal_and_ligand() -> None:
    """One-time prep: split 1PZO, extract CBT 300, convert ligand SDF→PDBQT."""
    if not CRYSTAL_APO_PDB.exists():
        full = parsePDB(str(CRYSTAL_PDB))
        protein = full.select("protein and chain A")
        writePDB(str(CRYSTAL_APO_PDB), protein)
        print(f"wrote {CRYSTAL_APO_PDB}")

    if not CRYSTAL_CBT_PDB.exists():
        full = parsePDB(str(CRYSTAL_PDB))
        cbt = full.select("resname CBT and resnum 300")
        writePDB(str(CRYSTAL_CBT_PDB), cbt)
        print(f"wrote {CRYSTAL_CBT_PDB} ({cbt.numAtoms()} atoms)")

    if not CRYSTAL_CBT_SDF.exists():
        # use obabel to make an SDF in canonical form for obrms comparison
        subprocess.run(
            ["obabel", str(CRYSTAL_CBT_PDB), "-O", str(CRYSTAL_CBT_SDF)],
            check=True, capture_output=True,
        )
        print(f"wrote {CRYSTAL_CBT_SDF}")

    if not LIGAND_PDBQT.exists():
        # Mol*/RCSB SDF exports use mmCIF-style uppercase elements (CL, BR).
        # obabel silently mistypes those → smina parse error. The pipeline
        # normalizes in place before conversion (see tasks._normalize_sdf_elements).
        fixed = _normalize_sdf_elements(str(LIGAND_SDF))
        if fixed:
            print(f"normalized {fixed} uppercase-element atom records in {LIGAND_SDF.name}")
        # Crystal SDF already has good 3D coords — skip --gen3d. Matches
        # pipeline behaviour for SDF inputs.
        subprocess.run(
            ["obabel", str(LIGAND_SDF), "-O", str(LIGAND_PDBQT)],
            check=True, capture_output=True,
        )
        print(f"wrote {LIGAND_PDBQT}")


def load_pockets() -> list[dict]:
    with open(CL_DIR / "pockets_clustered.csv") as f:
        return list(csv.DictReader(f))


def dock_one(receptor_pdb: Path, residues: str, out_dir: Path, tag: str) -> pd.DataFrame:
    """Dock CBT into one receptor with the supplied residue-defined box.

    `residues` is the space-separated "A_NN A_NN ..." string from the cluster CSV
    (matches calc_box's format).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    receptor_pdbqt = out_dir / f"{tag}_receptor.pdbqt"
    out_sdf = out_dir / f"{tag}_poses.sdf"

    if not receptor_pdbqt.exists():
        pdb_to_pdbqt(str(receptor_pdb), str(receptor_pdbqt), pH=7.4)

    center, lo, hi = calc_box(str(receptor_pdb), residues)
    size = box_size_from_extents(lo, hi)

    if not out_sdf.exists():
        stdout, _ = run_smina(
            ligand_path=str(LIGAND_PDBQT),
            protein_path=str(receptor_pdbqt),
            out_path=str(out_sdf),
            pocket_center=center,
            pocket_size=size,
            **SMINA_KW,
        )
    else:
        # already docked — re-parse smina log
        log_path = out_dir / "smina_debug.log"
        stdout = log_path.read_text() if log_path.exists() else ""

    df = parse_smina_log(stdout)
    df["tag"] = tag
    df["receptor_pdb"] = str(receptor_pdb)
    df["pose_sdf"] = str(out_sdf)
    df["box_center_x"] = center[0]
    df["box_center_y"] = center[1]
    df["box_center_z"] = center[2]
    df["box_size_x"] = size[0]
    df["box_size_y"] = size[1]
    df["box_size_z"] = size[2]
    df["residues"] = residues.strip()
    return df


def residues_from_crystal_cbt() -> str:
    """Build a residue-list string for calc_box from residues within 4.5Å of CBT 300.

    Same box-from-residue-cloud logic as the pipeline; gives an apples-to-apples box.
    """
    full = parsePDB(str(CRYSTAL_PDB))
    cbt = full.select("resname CBT and resnum 300")
    cbt_coords = cbt.getCoords()
    prot = full.select("protein and chain A")
    ca = prot.select("calpha")
    keep = []
    for resnum, coord in zip(ca.getResnums(), ca.getCoords()):
        if np.min(np.linalg.norm(cbt_coords - coord, axis=1)) < 8.0:
            keep.append(int(resnum))
    return " ".join(f"A_{r}" for r in sorted(set(keep)))


def main() -> int:
    prepare_crystal_and_ligand()
    pockets = load_pockets()

    rows: list[pd.DataFrame] = []

    # cluster 1 — all 56 cryptic-pocket frames
    c1 = [r for r in pockets if r["cluster"] == "1"]
    print(f"\n=== Arm: cluster1 ({len(c1)} frames) ===")
    for i, r in enumerate(c1, 1):
        frame = int(r["Frame"])
        pocket_idx = int(r["pocket_index"])
        tag = f"frame{frame}_p{pocket_idx}"
        pdb = FP_DIR / f"pdbs/trajectory_samples_{frame}.pdb"
        print(f"[{i}/{len(c1)}] cluster1 {tag} prob={r['probability']}")
        try:
            df = dock_one(pdb, r["residues"], OUT / "cluster1" / tag, tag)
            df["arm"] = "cluster1"
            df["frame"] = frame
            df["pocket_index"] = pocket_idx
            df["pocket_prob"] = float(r["probability"])
            rows.append(df)
        except Exception as exc:
            print(f"  FAILED: {exc}")

    # cluster 0 — top 5 by probability (orthosteric negative control)
    c0 = sorted([r for r in pockets if r["cluster"] == "0"],
                key=lambda r: -float(r["probability"]))[:5]
    print(f"\n=== Arm: cluster0 (top {len(c0)} frames) ===")
    for i, r in enumerate(c0, 1):
        frame = int(r["Frame"])
        pocket_idx = int(r["pocket_index"])
        tag = f"frame{frame}_p{pocket_idx}"
        pdb = FP_DIR / f"pdbs/trajectory_samples_{frame}.pdb"
        print(f"[{i}/{len(c0)}] cluster0 {tag} prob={r['probability']}")
        try:
            df = dock_one(pdb, r["residues"], OUT / "cluster0" / tag, tag)
            df["arm"] = "cluster0"
            df["frame"] = frame
            df["pocket_index"] = pocket_idx
            df["pocket_prob"] = float(r["probability"])
            rows.append(df)
        except Exception as exc:
            print(f"  FAILED: {exc}")

    # self-dock — 1PZO apo + box defined by residues near crystal CBT 300
    print(f"\n=== Arm: self_dock (1 receptor) ===")
    self_residues = residues_from_crystal_cbt()
    print(f"  self-dock box residues: {self_residues}")
    try:
        df = dock_one(CRYSTAL_APO_PDB, self_residues, OUT / "crystal" / "self_dock", "self_dock")
        df["arm"] = "self_dock"
        df["frame"] = -1
        df["pocket_index"] = -1
        df["pocket_prob"] = float("nan")
        rows.append(df)
    except Exception as exc:
        print(f"  FAILED: {exc}")

    if not rows:
        print("ERROR: nothing docked", file=sys.stderr)
        return 1
    all_df = pd.concat(rows, ignore_index=True)
    cols = ["arm", "frame", "pocket_index", "pocket_prob", "mode",
            "affinity (kcal/mol)", "rmsd l.b.", "rmsd u.b.",
            "receptor_pdb", "pose_sdf", "tag", "residues",
            "box_center_x", "box_center_y", "box_center_z",
            "box_size_x", "box_size_y", "box_size_z"]
    all_df = all_df[cols]
    out_csv = OUT / "scores.csv"
    all_df.to_csv(out_csv, index=False)
    print(f"\nwrote {out_csv} ({len(all_df)} pose rows across {all_df['tag'].nunique()} receptors)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
