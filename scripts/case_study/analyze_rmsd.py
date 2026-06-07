"""
Step 5 — Receptor Cα-alignment + symmetry-aware ligand RMSD.

For every (receptor, mode) row in scores.csv:
  1. Cα-align the receptor PDB to 1PZO chain A; extract rotation + translation.
  2. Apply that rigid transform to the docked pose's atom coords.
  3. obrms against crystal_CBT.sdf (handles SDF atom-order ambiguity + symmetry).
  4. Write rmsd.csv and figure_data.csv (best-pose row per receptor).
"""
from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from openbabel import openbabel, pybel
from prody import calcTransformation, matchAlign, parsePDB, writePDB

APP = Path("/app")
OUT = APP / "results/case_study_tem1_redock"
CRYSTAL = OUT / "crystal/1pzo.pdb"
CRYSTAL_APO = OUT / "crystal/1pzo_apo.pdb"
CRYSTAL_CBT_SDF = OUT / "crystal/crystal_CBT.sdf"
SCORES_CSV = OUT / "scores.csv"
RMSD_CSV = OUT / "rmsd.csv"
FIGURE_DATA_CSV = OUT / "figure_data.csv"
ALIGNED_DIR = OUT / "aligned_poses"


def kabsch_transform(before: np.ndarray, after: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Solve for (R, t) such that `R @ before + t = after` (column-vector form),
    given matched point clouds (N×3 each). Used to extract the rigid transform
    matchAlign applied to a receptor so we can apply the same to the docked pose.
    """
    cb_b = before.mean(axis=0)
    cb_a = after.mean(axis=0)
    P = before - cb_b
    Q = after - cb_a
    H = P.T @ Q
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
    t = cb_a - R @ cb_b
    return R, t


def split_sdf(in_sdf: Path, out_dir: Path, prefix: str) -> list[Path]:
    """Split a multi-record SDF into per-mode SDFs. Returns paths in order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, mol in enumerate(pybel.readfile("sdf", str(in_sdf)), start=1):
        path = out_dir / f"{prefix}_mode{i}.sdf"
        mol.write("sdf", str(path), overwrite=True)
        paths.append(path)
    return paths


def transform_sdf(in_sdf: Path, out_sdf: Path, R: np.ndarray, t: np.ndarray) -> None:
    """Rigid-transform a single-record SDF, writing to a new file."""
    mol = next(pybel.readfile("sdf", str(in_sdf)))
    for atom in mol.atoms:
        x, y, z = atom.coords
        new = R @ np.array([x, y, z]) + t
        atom.OBAtom.SetVector(float(new[0]), float(new[1]), float(new[2]))
    mol.write("sdf", str(out_sdf), overwrite=True)


def run_obrms(ref_sdf: Path, target_sdf: Path) -> float:
    """obrms ref target → "RMSD <ref-title>:<target-title> <value>". Symmetry-aware."""
    res = subprocess.run(
        ["obrms", str(ref_sdf), str(target_sdf)],
        capture_output=True, text=True, check=True,
    )
    for line in res.stdout.strip().splitlines():
        toks = line.split()
        if not toks:
            continue
        try:
            return float(toks[-1])
        except ValueError:
            continue
    raise RuntimeError(f"obrms produced no parseable RMSD: {res.stdout!r}")


def transform_for_receptor(receptor_pdb: Path, crystal_apo: object) -> tuple[np.ndarray, np.ndarray]:
    """Cα-align receptor PDB to 1PZO chain A; return (R, t)."""
    mob = parsePDB(str(receptor_pdb))
    ca_before = mob.select("calpha").getCoords().copy()
    matchAlign(mob, crystal_apo, tarsel="calpha", mosel="calpha")
    ca_after = mob.select("calpha").getCoords()
    return kabsch_transform(ca_before, ca_after)


def main() -> int:
    scores = pd.read_csv(SCORES_CSV)
    print(f"loaded {len(scores)} pose rows from {SCORES_CSV.name}")
    ALIGNED_DIR.mkdir(parents=True, exist_ok=True)

    crystal_apo = parsePDB(str(CRYSTAL_APO))

    rmsd_rows: list[dict] = []
    receptor_groups = scores.groupby(["arm", "frame", "pocket_index", "receptor_pdb", "pose_sdf", "tag"])
    n = len(receptor_groups)
    for i, ((arm, frame, pkt, receptor_pdb, pose_sdf, tag), grp) in enumerate(receptor_groups, 1):
        print(f"[{i}/{n}] {arm} {tag}")
        receptor_pdb_path = Path(receptor_pdb)
        pose_sdf_path = Path(pose_sdf)
        if not pose_sdf_path.exists():
            print(f"  skip — {pose_sdf_path} missing")
            continue

        # Receptor transform (identity for self_dock)
        if arm == "self_dock":
            R = np.eye(3)
            t = np.zeros(3)
        else:
            R, t = transform_for_receptor(receptor_pdb_path, crystal_apo)

        # Split + transform poses
        per_mode_dir = ALIGNED_DIR / f"{arm}_{tag}"
        per_mode_dir.mkdir(parents=True, exist_ok=True)
        raw_split = per_mode_dir / "raw"
        raw_split.mkdir(exist_ok=True)
        mode_paths = split_sdf(pose_sdf_path, raw_split, prefix=tag)

        for mode_idx, mode_sdf in enumerate(mode_paths, start=1):
            aligned_sdf = per_mode_dir / f"{tag}_mode{mode_idx}_aligned.sdf"
            transform_sdf(mode_sdf, aligned_sdf, R, t)
            try:
                rmsd = run_obrms(CRYSTAL_CBT_SDF, aligned_sdf)
            except Exception as exc:
                print(f"  mode {mode_idx} obrms failed: {exc}")
                rmsd = float("nan")
            # join with the score for this mode
            row = grp[grp["mode"] == mode_idx]
            if row.empty:
                affinity = float("nan")
                pocket_prob = float("nan")
            else:
                affinity = float(row["affinity (kcal/mol)"].iloc[0])
                pocket_prob = float(row["pocket_prob"].iloc[0])
            rmsd_rows.append({
                "arm": arm,
                "frame": int(frame),
                "pocket_index": int(pkt),
                "pocket_prob": pocket_prob,
                "tag": tag,
                "mode": mode_idx,
                "affinity_kcal_mol": affinity,
                "rmsd_to_crystal_A": rmsd,
                "aligned_sdf": str(aligned_sdf),
            })

    rmsd_df = pd.DataFrame(rmsd_rows)
    rmsd_df.to_csv(RMSD_CSV, index=False)
    print(f"wrote {RMSD_CSV} ({len(rmsd_df)} rows)")

    # figure_data: one row per receptor = the lowest-affinity (best) pose
    if not rmsd_df.empty:
        best_idx = rmsd_df.groupby(["arm", "tag"])["affinity_kcal_mol"].idxmin()
        figure_df = rmsd_df.loc[best_idx].reset_index(drop=True)
        figure_df.to_csv(FIGURE_DATA_CSV, index=False)
        print(f"wrote {FIGURE_DATA_CSV} ({len(figure_df)} best-pose rows)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
