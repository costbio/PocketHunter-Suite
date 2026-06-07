"""
Final per-receptor table the user explicitly asked for:

  arm | frame | affinity | RMSD(C, crystal) | RMSD(C, self_dock_pose)
                              ↑                      ↑
                         crystal pose         "re-docked" pose

Plus printout-friendly tabular summary of cluster1 top-10 closest to crystal.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

APP = Path("/app")
OUT = APP / "results/case_study_tem1_redock"
ALIGNED = OUT / "aligned_poses"
CRYSTAL_CBT_SDF = OUT / "crystal/crystal_CBT.sdf"
SELF_DOCK_BEST_SDF = ALIGNED / "self_dock_self_dock/self_dock_mode1_aligned.sdf"
FINAL_CSV = OUT / "final_table.csv"


def obrms_rmsd(ref: Path, target: Path) -> float:
    res = subprocess.run(
        ["obrms", str(ref), str(target)],
        capture_output=True, text=True, check=True,
    )
    for line in res.stdout.strip().splitlines():
        toks = line.split()
        if toks:
            try:
                return float(toks[-1])
            except ValueError:
                pass
    return float("nan")


def main() -> int:
    rmsd = pd.read_csv(OUT / "rmsd.csv")

    if not SELF_DOCK_BEST_SDF.exists():
        print(f"ERROR: {SELF_DOCK_BEST_SDF} missing", file=sys.stderr)
        return 1

    # Self-dock best pose vs crystal — already in rmsd.csv but recompute explicitly
    A_vs_B = obrms_rmsd(CRYSTAL_CBT_SDF, SELF_DOCK_BEST_SDF)
    print(f"A↔B: self-dock best pose vs crystal CBT  = {A_vs_B:.2f} Å")

    # For each (arm, tag, mode), compute RMSD(aligned pose, self_dock best pose)
    rows = []
    for _, r in rmsd.iterrows():
        if r["arm"] == "self_dock":
            A_vs_C = 0.0 if int(r["mode"]) == 1 else obrms_rmsd(SELF_DOCK_BEST_SDF, Path(r["aligned_sdf"]))
        else:
            A_vs_C = obrms_rmsd(SELF_DOCK_BEST_SDF, Path(r["aligned_sdf"]))
        rows.append({
            "arm": r["arm"],
            "frame": int(r["frame"]),
            "tag": r["tag"],
            "mode": int(r["mode"]),
            "affinity_kcal_mol": r["affinity_kcal_mol"],
            "rmsd_vs_crystal_A": r["rmsd_to_crystal_A"],
            "rmsd_vs_selfdock_pose_A": A_vs_C,
        })
    df = pd.DataFrame(rows)
    df.to_csv(FINAL_CSV, index=False)
    print(f"wrote {FINAL_CSV} ({len(df)} rows)")

    # Best-affinity pose per receptor
    best_idx = df.groupby(["arm", "tag"])["affinity_kcal_mol"].idxmin()
    best = df.loc[best_idx].reset_index(drop=True)

    # User-facing summary table — cluster 1 sorted by closest-to-crystal
    print("\n=== Top-15 cluster-1 frames (best-affinity pose), sorted by RMSD to crystal CBT ===")
    c1_best = best[best["arm"] == "cluster1"].sort_values("rmsd_vs_crystal_A")
    cols = ["frame", "affinity_kcal_mol", "rmsd_vs_crystal_A", "rmsd_vs_selfdock_pose_A"]
    print(c1_best[cols].head(15).to_string(index=False, float_format="%.2f"))

    print("\n=== Self-dock and cluster-0 best-affinity poses ===")
    others = best[best["arm"].isin(["self_dock", "cluster0"])].sort_values(["arm", "frame"])
    print(others[["arm", "frame"] + cols[1:]].to_string(index=False, float_format="%.2f"))

    print("\n=== Cluster 1: closest-of-10-poses to crystal, per receptor (top 15) ===")
    c1_all = df[df["arm"] == "cluster1"]
    closest_per = c1_all.groupby("frame").agg(
        min_rmsd_vs_crystal=("rmsd_vs_crystal_A", "min"),
        min_rmsd_vs_selfdock=("rmsd_vs_selfdock_pose_A", "min"),
        best_affinity=("affinity_kcal_mol", "min"),
    ).sort_values("min_rmsd_vs_crystal")
    print(closest_per.head(15).to_string(float_format="%.2f"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
