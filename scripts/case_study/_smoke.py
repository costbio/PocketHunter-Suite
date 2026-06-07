"""Quick smoke test: one frame from cluster 1 + the self-dock setup."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/app/scripts/case_study")
sys.path.insert(0, "/app")

from redock_cbt import (
    prepare_crystal_and_ligand,
    dock_one,
    residues_from_crystal_cbt,
    FP_DIR,
    OUT,
    CRYSTAL_APO_PDB,
    load_pockets,
)

print(">> prep")
prepare_crystal_and_ligand()
print(">> self-dock box residues from CBT contact shell")
print(residues_from_crystal_cbt())
print(">> dock one cluster1 frame (highest probability)")
pockets = load_pockets()
c1_top = sorted([r for r in pockets if r["cluster"] == "1"],
                key=lambda r: -float(r["probability"]))[0]
print(f"   frame={c1_top['Frame']} prob={c1_top['probability']}")
pdb = FP_DIR / f"pdbs/trajectory_samples_{c1_top['Frame']}.pdb"
df = dock_one(pdb, c1_top["residues"], OUT / "cluster1" / "smoke", "smoke")
print(df[["mode", "affinity (kcal/mol)"]].head())
print(">> smoke OK")
