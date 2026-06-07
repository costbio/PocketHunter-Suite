"""
For each best-affinity pose, compute Hungarian-matched per-atom distances
to crystal CBT atoms (same-element 1-to-1 assignment). Then decompose:

  - anchor_rmsd_A: RMSD of the 11 atoms with smallest matched distance
  - tail_rmsd_A:   RMSD of the 11 atoms with largest matched distance
  - n_within_1A / n_within_2A: heavy atoms in canonical position

This captures the "half-right" pattern we noticed in the self-dock pose
(one chlorine atom within 0.1 Å of crystal, the other 9 Å off due to a
flexed torsion).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from openbabel import pybel
from scipy.optimize import linear_sum_assignment

APP = Path("/app")
OUT = APP / "results/case_study_tem1_redock"
CRYSTAL_CBT_SDF = OUT / "crystal/crystal_CBT.sdf"
FINAL_CSV = OUT / "final_table.csv"
OUT_CSV = OUT / "final_table_with_anchor.csv"


def load_atoms(sdf: Path) -> list[tuple[int, np.ndarray]]:
    mol = next(pybel.readfile("sdf", str(sdf)))
    return [(a.atomicnum, np.array(a.coords)) for a in mol.atoms]


def hungarian_per_atom(docked_atoms, crystal_atoms) -> np.ndarray:
    """1-to-1 same-element assignment; returns sorted per-atom distances."""
    n = len(docked_atoms)
    m = len(crystal_atoms)
    cost = np.full((n, m), 1e9)
    for i, (e1, c1) in enumerate(docked_atoms):
        for j, (e2, c2) in enumerate(crystal_atoms):
            if e1 == e2:
                cost[i, j] = np.linalg.norm(c1 - c2)
    row, col = linear_sum_assignment(cost)
    return np.sort(np.array([cost[i, j] for i, j in zip(row, col)]))


def decompose(distances: np.ndarray) -> dict:
    n = len(distances)
    half = n // 2  # 11 for 22-atom CBT
    return {
        "n_atoms_within_1A": int((distances < 1.0).sum()),
        "n_atoms_within_2A": int((distances < 2.0).sum()),
        "n_atoms_within_3A": int((distances < 3.0).sum()),
        "anchor_rmsd_A": float(np.sqrt((distances[:half] ** 2).mean())),
        "tail_rmsd_A": float(np.sqrt((distances[half:] ** 2).mean())),
        "all_rmsd_A": float(np.sqrt((distances ** 2).mean())),
        "max_atom_dist_A": float(distances[-1]),
    }


def main() -> int:
    final = pd.read_csv(FINAL_CSV)
    crystal_atoms = load_atoms(CRYSTAL_CBT_SDF)

    rows = []
    for _, r in final.iterrows():
        aligned_sdf = OUT / "aligned_poses" / f"{r['arm']}_{r['tag']}" / f"{r['tag']}_mode{int(r['mode'])}_aligned.sdf"
        if not aligned_sdf.exists():
            continue
        docked_atoms = load_atoms(aligned_sdf)
        if len(docked_atoms) != len(crystal_atoms):
            print(f"WARN atom-count mismatch on {aligned_sdf}: {len(docked_atoms)} vs {len(crystal_atoms)}")
            continue
        d = hungarian_per_atom(docked_atoms, crystal_atoms)
        dec = decompose(d)
        rows.append({
            "arm": r["arm"], "frame": r["frame"], "tag": r["tag"], "mode": int(r["mode"]),
            "affinity_kcal_mol": r["affinity_kcal_mol"],
            "rmsd_vs_crystal_obrms": r["rmsd_vs_crystal_A"],  # symmetry-aware total RMSD
            **dec,
        })

    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False)
    print(f"wrote {OUT_CSV} ({len(df)} rows)")

    # Best-affinity pose per receptor
    best_idx = df.groupby(["arm", "tag"])["affinity_kcal_mol"].idxmin()
    best = df.loc[best_idx].reset_index(drop=True)

    # The headline table the user asked for
    cols = ["arm", "frame", "affinity_kcal_mol",
            "rmsd_vs_crystal_obrms", "anchor_rmsd_A", "tail_rmsd_A",
            "n_atoms_within_1A", "n_atoms_within_2A", "max_atom_dist_A"]

    print("\n=== Self-dock ===")
    self_dock = best[best["arm"] == "self_dock"]
    print(self_dock[cols].to_string(index=False, float_format="%.2f"))

    print("\n=== Cluster 1: top 15 by affinity (best-affinity pose per receptor) ===")
    c1 = best[best["arm"] == "cluster1"].sort_values("affinity_kcal_mol")
    print(c1[cols].head(15).to_string(index=False, float_format="%.2f"))

    print("\n=== Cluster 1: top 10 by closest anchor RMSD ===")
    c1_anchor = best[best["arm"] == "cluster1"].sort_values("anchor_rmsd_A")
    print(c1_anchor[cols].head(10).to_string(index=False, float_format="%.2f"))

    print("\n=== Cluster 0 (orthosteric, neg ctrl) ===")
    c0 = best[best["arm"] == "cluster0"].sort_values("affinity_kcal_mol")
    print(c0[cols].to_string(index=False, float_format="%.2f"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
