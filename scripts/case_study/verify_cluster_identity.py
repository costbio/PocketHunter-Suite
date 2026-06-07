"""
Step 0 — Cluster-identity verification before TEM-1 redock.

Aligns a BioEmu PDB to 1PZO chain B by Cα, then checks where each cluster's
residue centroid lands relative to (a) the crystallographic CBT centroid
and (b) Ser70 Cα (active-site landmark). Also prints the BioEmu→Ambler
residue map for the catalytic Ser-X-X-Lys motif.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from prody import parsePDB, matchAlign

REPO = Path("/home/onur/repos/pockethunter-suite")
FP_DIR = REPO / "results/find_pockets_20260523_212540_ce8948a0"
CL_DIR = REPO / "results/cluster_20260523_213431_67e98d92/pocket_clusters"
CRYSTAL = REPO / "results/case_study_tem1_redock/crystal/1pzo.pdb"

REP_FRAME_CLUSTER1 = 83
REP_FRAME_CLUSTER0 = 24


def load_pockets():
    with open(CL_DIR / "pockets_clustered.csv") as f:
        return list(csv.DictReader(f))


def parse_residues_field(s: str) -> list[int]:
    return [int(tok.split("_")[1]) for tok in s.split() if tok.startswith("A_")]


def centroid_of_resids(atoms, resids: list[int]) -> np.ndarray:
    coords = atoms.select(f"calpha resnum {' '.join(map(str, resids))}").getCoords()
    return coords.mean(axis=0)


def main() -> int:
    pockets = load_pockets()
    by_cluster: dict[str, list[dict]] = {}
    for r in pockets:
        by_cluster.setdefault(r["cluster"], []).append(r)
    print("cluster sizes:", {k: len(v) for k, v in by_cluster.items()})

    # Cα-align cluster-1 rep frame to 1PZO chain A (only chain)
    crystal_full = parsePDB(str(CRYSTAL))
    crystal_A = crystal_full.select("protein and chain A").copy()
    ser70_ca = crystal_A.select("resnum 70 and name CA").getCoords()[0]
    print(f"crystal Ser70 Cα (1PZO chA):     {ser70_ca}")

    cbt_300 = crystal_full.select("resname CBT and resnum 300")
    cbt_301 = crystal_full.select("resname CBT and resnum 301")
    for tag, sel in [("CBT 300", cbt_300), ("CBT 301", cbt_301)]:
        if sel is None:
            continue
        c = sel.getCoords().mean(axis=0)
        d = np.linalg.norm(c - ser70_ca)
        print(f"  {tag} centroid: {c}  → Ser70 dist: {d:.2f} Å")

    # Pick the cryptic one — farther from Ser70 (>15 Å is cryptic; <8 Å is orthosteric)
    cbt = cbt_300  # default; user supplied CBT 300 SDF
    cbt_centroid = cbt.getCoords().mean(axis=0)
    print(f"using CBT 300 (matches user SDF) as crystal reference: centroid={cbt_centroid}")

    # Check Ser70 Ambler mapping in BioEmu PDB:
    biemu_rep = parsePDB(str(FP_DIR / f"pdbs/trajectory_samples_{REP_FRAME_CLUSTER1}.pdb"))
    # find Ser-X-X-Lys motif → catalytic Ser70 in TEM-1
    biemu_ca = biemu_rep.select("calpha")
    resn_seq = list(biemu_ca.getResnames())
    resnum_seq = list(biemu_ca.getResnums())
    for i in range(len(resn_seq) - 3):
        if (resn_seq[i] == "SER" and resn_seq[i+3] == "LYS"
                and resn_seq[i+1] in ("THR", "ALA", "VAL") and resn_seq[i+2] in ("PHE", "TYR", "LEU", "ILE")):
            print(f"BioEmu candidate SXXK motif: Ser at resnum {resnum_seq[i]} → Ambler 70 (catalytic)")
            break

    # Spatial sanity for each cluster rep
    for cl_label, frame in [("cluster1", REP_FRAME_CLUSTER1), ("cluster0", REP_FRAME_CLUSTER0)]:
        cluster_rows = [r for r in pockets if r["cluster"] == cl_label[-1]]
        # collect ALL residues across the cluster (frequency-weighted centroid would be similar)
        all_residues = sorted({rid for r in cluster_rows for rid in parse_residues_field(r["residues"])})
        biemu = parsePDB(str(FP_DIR / f"pdbs/trajectory_samples_{frame}.pdb"))
        result = matchAlign(biemu, crystal_A, tarsel="calpha", mosel="calpha")
        mobile_aligned = result[0]
        seqid = result[3] if len(result) > 3 else 0.0
        overlap = result[4] if len(result) > 4 else 0.0
        # centroid of all cluster residues in the aligned BioEmu structure
        ca = mobile_aligned.select("calpha")
        present = set(ca.getResnums())
        target_ids = [r for r in all_residues if r in present]
        coords = ca.select(f"resnum {' '.join(map(str, target_ids))}").getCoords()
        centroid = coords.mean(axis=0)
        d_cbt = float(np.linalg.norm(centroid - cbt_centroid))
        d_ser70 = float(np.linalg.norm(centroid - ser70_ca))
        rep_count = Counter(rid for r in cluster_rows for rid in parse_residues_field(r["residues"]))
        top10 = rep_count.most_common(10)
        print(f"\n--- {cl_label} (rep frame {frame}, {len(cluster_rows)} pockets, {len(target_ids)} residues mapped) ---")
        print(f"  Cα-align seqid={seqid:.1f}% overlap={overlap:.1f}%")
        print(f"  cluster centroid (in 1PZO frame): {centroid}")
        print(f"  centroid → CBT distance:    {d_cbt:.2f} Å")
        print(f"  centroid → Ser70 Cα dist:   {d_ser70:.2f} Å")
        print(f"  most frequent residues:     {top10}")
        if cl_label == "cluster1":
            verdict = "CRYPTIC pocket (close to CBT)" if d_cbt < d_ser70 else "ACTIVE site (close to Ser70)"
        else:
            verdict = "ACTIVE site (close to Ser70)" if d_ser70 < d_cbt else "CRYPTIC pocket (close to CBT)"
        print(f"  spatial verdict: {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
