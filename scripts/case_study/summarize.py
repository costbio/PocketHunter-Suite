"""
Step 6 — Summary table + scatter + histogram for the TEM-1 CBT re-dock case study.

Reports two RMSD metrics per arm:
  - **best-affinity** RMSD: the RMSD of the pose smina ranked #1 by score
    (what a real VS run would report).
  - **closest-pose** RMSD: the minimum RMSD across all 10 generated poses
    (answers "could smina find the crystal pose at all from this receptor?").

The split is necessary because CBT is flexible (4 active torsions): even
when smina samples poses in the correct pocket region, the *highest-scoring*
pose can be a different rotamer. The self-dock control demonstrates the
floor — even with the true crystal receptor, vinardo can't recover the
crystal pose below ~4 Å. PocketHunter's cluster-1 reps reach the same floor.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

APP = Path("/app")
OUT = APP / "results/case_study_tem1_redock"
RMSD_CSV = OUT / "rmsd.csv"
FIGURE_DATA_CSV = OUT / "figure_data.csv"
SUMMARY_TXT = OUT / "summary.txt"
SCATTER_PNG = OUT / "figure_affinity_vs_rmsd.png"
HIST_PNG = OUT / "figure_rmsd_histogram.png"

ARM_LABEL = {
    "self_dock": "Self-dock 1PZO",
    "cluster1": "Cluster 1 (cryptic)",
    "cluster0": "Cluster 0 (orthosteric, neg. ctrl)",
}
ARM_COLOR = {"self_dock": "#000000", "cluster1": "#1f77b4", "cluster0": "#d62728"}


def summarize_arm(rmsd_df: pd.DataFrame, fig_df: pd.DataFrame, arm: str) -> dict:
    arm_all = rmsd_df[rmsd_df["arm"] == arm]
    arm_best = fig_df[fig_df["arm"] == arm]
    closest_per_recv = arm_all.groupby("tag")["rmsd_to_crystal_A"].min()
    return {
        "n_receptors": arm_best["tag"].nunique(),
        "mean_best_affinity": arm_best["affinity_kcal_mol"].mean(),
        "best_affinity": arm_best["affinity_kcal_mol"].min(),
        "mean_best_affinity_RMSD": arm_best["rmsd_to_crystal_A"].mean(),
        "min_best_affinity_RMSD": arm_best["rmsd_to_crystal_A"].min(),
        "mean_closest_pose_RMSD": closest_per_recv.mean(),
        "min_closest_pose_RMSD": closest_per_recv.min(),
        "n_closest_below_5A": int((closest_per_recv < 5.0).sum()),
    }


def main() -> int:
    rmsd = pd.read_csv(RMSD_CSV)
    figure_df = pd.read_csv(FIGURE_DATA_CSV)

    rows = []
    for arm in ["self_dock", "cluster1", "cluster0"]:
        if (figure_df["arm"] == arm).any():
            rows.append({"arm": ARM_LABEL[arm], **summarize_arm(rmsd, figure_df, arm)})
    summary_df = pd.DataFrame(rows).set_index("arm")
    text = summary_df.to_string(float_format="%.2f")
    print(text)
    SUMMARY_TXT.write_text(text + "\n")
    print(f"\nwrote {SUMMARY_TXT}")

    # ── Scatter: best-affinity pose per receptor ───────────────────────────
    fig, ax = plt.subplots(figsize=(7.5, 5.5), dpi=140)
    for arm in ["cluster0", "cluster1", "self_dock"]:
        arm_data = figure_df[figure_df["arm"] == arm]
        if arm_data.empty:
            continue
        ax.scatter(
            arm_data["rmsd_to_crystal_A"], arm_data["affinity_kcal_mol"],
            color=ARM_COLOR[arm], label=f"{ARM_LABEL[arm]} (n={len(arm_data)})",
            s=140 if arm == "self_dock" else 50,
            edgecolors="white" if arm != "self_dock" else "black",
            linewidths=1.5 if arm == "self_dock" else 0.7,
            alpha=0.9, marker="*" if arm == "self_dock" else "o", zorder=3,
        )
    self_rmsd = figure_df[figure_df["arm"] == "self_dock"]["rmsd_to_crystal_A"].iloc[0]
    ax.axvspan(0, self_rmsd, color="#dceeff", alpha=0.4, zorder=0,
               label=f"≤ self-dock floor ({self_rmsd:.1f} Å)")
    ax.set_xlabel("RMSD of best-affinity pose to crystal CBT (Å)")
    ax.set_ylabel("smina affinity (kcal/mol, lower is better)")
    ax.set_title("TEM-1 CBT re-docking — best-affinity pose per receptor")
    ax.invert_yaxis()
    ax.legend(loc="upper right", frameon=True, framealpha=0.95)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(SCATTER_PNG)
    print(f"wrote {SCATTER_PNG}")

    # ── Histogram: closest-pose RMSD per receptor ──────────────────────────
    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=140)
    bins = np.arange(0, 25, 1.0)
    for arm in ["cluster1", "cluster0", "self_dock"]:
        arm_all = rmsd[rmsd["arm"] == arm]
        if arm_all.empty:
            continue
        closest = arm_all.groupby("tag")["rmsd_to_crystal_A"].min().values
        ax.hist(
            closest, bins=bins, alpha=0.55, color=ARM_COLOR[arm],
            edgecolor=ARM_COLOR[arm], linewidth=1.5, density=False,
            label=f"{ARM_LABEL[arm]} (n={len(closest)})",
        )
    ax.axvline(self_rmsd, color="black", linestyle="--", linewidth=1.2,
               label=f"self-dock floor ({self_rmsd:.1f} Å)")
    ax.set_xlabel("Closest-pose RMSD to crystal CBT (Å)")
    ax.set_ylabel("Number of receptors")
    ax.set_title("Closest-of-10-poses RMSD distribution — how often does the right region get sampled?")
    ax.legend(loc="upper right", frameon=True, framealpha=0.95)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(HIST_PNG)
    print(f"wrote {HIST_PNG}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
