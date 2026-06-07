"""Vina-scoring rescue test: self-dock + 3 best cluster-1 frames.
Goal: check whether vina (the default) recovers crystal pose better than vinardo."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, "/app/scripts/case_study")
sys.path.insert(0, "/app")
from redock_cbt import dock_one, OUT, FP_DIR, CRYSTAL_APO_PDB, load_pockets, residues_from_crystal_cbt
import redock_cbt
redock_cbt.SMINA_KW = dict(num_poses=10, exhaustiveness=8, scoring_function="vina")

import pandas as pd

OUT_VINA = OUT / "vina_test"
OUT_VINA.mkdir(exist_ok=True)

rows = []

# 3 cluster-1 frames with best vinardo RMSD
pockets = load_pockets()
target_frames = [2, 83, 63]
c1 = {int(r["Frame"]): r for r in pockets if r["cluster"] == "1"}
for f in target_frames:
    r = c1[f]
    tag = f"vina_frame{f}_p{r['pocket_index']}"
    pdb = FP_DIR / f"pdbs/trajectory_samples_{f}.pdb"
    print(f"dock {tag}")
    df = dock_one(pdb, r["residues"], OUT_VINA / tag, tag)
    df["arm"] = "cluster1_vina"
    df["frame"] = f
    df["pocket_index"] = int(r["pocket_index"])
    df["pocket_prob"] = float(r["probability"])
    rows.append(df)

# Self-dock with vina
print("dock self_dock_vina")
self_residues = residues_from_crystal_cbt()
df = dock_one(CRYSTAL_APO_PDB, self_residues, OUT_VINA / "self_dock_vina", "self_dock_vina")
df["arm"] = "self_dock_vina"
df["frame"] = -1
df["pocket_index"] = -1
df["pocket_prob"] = float("nan")
rows.append(df)

all_df = pd.concat(rows, ignore_index=True)
all_df.to_csv(OUT_VINA / "scores_vina.csv", index=False)
print(f"wrote {OUT_VINA / 'scores_vina.csv'}")
