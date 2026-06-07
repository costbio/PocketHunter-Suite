"""
Build receptor + ligand complex PDBs for the interesting cases of the TEM-1
re-dock case study. Each output is a single PDB: the receptor protein
(Cα-aligned to 1PZO chain A) followed by the docked CBT pose as a HETATM
block (resname CBT, chain L, resnum 999). All structures end up in the
same coordinate frame so they can be loaded together and compared visually.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from prody import matchAlign, parsePDB, writePDB

APP = Path("/app")
OUT_BASE = APP / "results/case_study_tem1_redock"
ALIGNED_POSES = OUT_BASE / "aligned_poses"
COMPLEX_DIR = OUT_BASE / "complexes"
COMPLEX_DIR.mkdir(exist_ok=True)

CRYSTAL_PDB = OUT_BASE / "crystal/1pzo.pdb"
CRYSTAL_APO = OUT_BASE / "crystal/1pzo_apo.pdb"
CRYSTAL_CBT_PDB = OUT_BASE / "crystal/crystal_CBT.pdb"
FP_DIR = APP / "results/find_pockets_20260523_212540_ce8948a0"


@dataclass
class Case:
    out_name: str
    arm: str
    tag: str
    mode: int
    receptor_pdb: Path
    label: str  # one-line description for README


CASES: list[Case] = [
    # Crystal reference + self-dock control are handled specially below.
    # Cluster-1 cryptic-pocket winners (best-anchor mode, anchor < 2 Å):
    Case("10_cluster1_frame63_mode1_BEST.pdb", "cluster1", "frame63_p2", 1,
         FP_DIR / "pdbs/trajectory_samples_63.pdb",
         "Cluster-1 frame 63 mode 1 — best overall: anchor 0.94 Å, 14/22 atoms within 2 Å of crystal (beats crystal self-dock)."),
    Case("11_cluster1_frame43_mode5_bestAnchor.pdb", "cluster1", "frame43_p1", 5,
         FP_DIR / "pdbs/trajectory_samples_43.pdb",
         "Cluster-1 frame 43 mode 5 — anchor 1.28 Å, 13 atoms within 2 Å. Affinity -4.6 (best-aff mode 1 is similar)."),
    Case("12_cluster1_frame46_mode1_bestAFFINITY.pdb", "cluster1", "frame46_p2", 1,
         FP_DIR / "pdbs/trajectory_samples_46.pdb",
         "Cluster-1 frame 46 mode 1 (BEST-AFFINITY -4.90 kcal/mol) — but ANCHOR 15.0 Å. Wrong region."),
    Case("13_cluster1_frame46_mode8_bestANCHOR.pdb", "cluster1", "frame46_p2", 8,
         FP_DIR / "pdbs/trajectory_samples_46.pdb",
         "Cluster-1 frame 46 mode 8 (anchor-correct, ANCHOR 1.37 Å) — affinity -4.4 (0.5 kcal weaker than best-aff). Right region."),
    Case("14_cluster1_frame2_mode5_bestANCHOR.pdb", "cluster1", "frame2_p2", 5,
         FP_DIR / "pdbs/trajectory_samples_2.pdb",
         "Cluster-1 frame 2 mode 5 — anchor 1.59 Å. (Best-aff mode 1 has anchor 14.9 Å.)"),
    Case("15_cluster1_frame47_mode4_bestANCHOR.pdb", "cluster1", "frame47_p2", 4,
         FP_DIR / "pdbs/trajectory_samples_47.pdb",
         "Cluster-1 frame 47 mode 4 — anchor 1.85 Å. (Best-aff mode 1 has anchor 15.1 Å.)"),
    Case("16_cluster1_frame40_mode6_bestANCHOR.pdb", "cluster1", "frame40_p1", 6,
         FP_DIR / "pdbs/trajectory_samples_40.pdb",
         "Cluster-1 frame 40 mode 6 — anchor 1.92 Å, affinity -4.4."),
    # The famous frame-41 affinity-vs-anchor decorrelation case:
    Case("20_cluster1_frame41_mode1_BESTAFF_wrong.pdb", "cluster1", "frame41_p2", 1,
         FP_DIR / "pdbs/trajectory_samples_41.pdb",
         "Cluster-1 frame 41 mode 1 (BEST-AFFINITY -6.80 kcal/mol of all 56 frames) — ANCHOR 6.68 Å, wrong region."),
    Case("21_cluster1_frame41_mode2_anchorCorrect.pdb", "cluster1", "frame41_p2", 2,
         FP_DIR / "pdbs/trajectory_samples_41.pdb",
         "Cluster-1 frame 41 mode 2 — anchor 3.07 Å. Right region; 0.7 kcal weaker than mode 1. Smina ranked it lower."),
    # Negative control:
    Case("30_cluster0_frame24_mode1_negCtrl.pdb", "cluster0", "frame24_p1", 1,
         FP_DIR / "pdbs/trajectory_samples_24.pdb",
         "Cluster-0 frame 24 mode 1 (best-affinity -6.70) — anchor 16.76 Å. Negative control: orthosteric site."),
    Case("31_cluster0_frame73_mode8_negCtrl.pdb", "cluster0", "frame73_p2", 8,
         FP_DIR / "pdbs/trajectory_samples_73.pdb",
         "Cluster-0 frame 73 mode 8 — best-anchor for the orthosteric cluster (8.70 Å). Still 9× worse than cluster-1 winner."),
]


def cbt_pdb_from_sdf(sdf: Path, dest_pdb: Path) -> None:
    """Convert SDF → PDB and rewrite atom lines so the ligand has clean
    HETATM / resname=CBT / chain=L / resnum=999 — easy to highlight in any viewer.

    PDB columns (1-indexed): 1-6 record, 7-11 serial, 13-16 name, 17 altloc,
    18-20 resname, 22 chain, 23-26 resnum, 27 icode, 31-38 x, 39-46 y, 47-54 z.
    """
    tmp = dest_pdb.with_suffix(".raw.pdb")
    subprocess.run(["obabel", str(sdf), "-O", str(tmp)], check=True, capture_output=True)
    lines = []
    for orig in tmp.read_text().splitlines():
        if orig.startswith(("HETATM", "ATOM  ")) and len(orig) >= 26:
            # rebuild a single line from the ORIGINAL (not the half-built one)
            ln = ("HETATM" + orig[6:17] + "CBT" + orig[20]
                  + "L" + " 999" + orig[26:])
            lines.append(ln)
        elif orig.startswith("CONECT") or orig.startswith("END"):
            lines.append(orig)
    dest_pdb.write_text("\n".join(lines) + "\n")
    tmp.unlink(missing_ok=True)


def write_complex(receptor_pdb: Path, ligand_pdb: Path, out_pdb: Path) -> None:
    """Concatenate receptor + ligand PDBs with proper TER and END."""
    rec_lines = receptor_pdb.read_text().splitlines()
    lig_lines = ligand_pdb.read_text().splitlines()
    # Strip terminal END/MASTER from receptor; keep TER lines; append ligand
    out = []
    for ln in rec_lines:
        if ln.startswith(("END", "MASTER")):
            continue
        out.append(ln)
    out.append("TER")
    for ln in lig_lines:
        if ln.startswith("END") or ln.startswith("MASTER"):
            continue
        out.append(ln)
    out.append("TER")
    out.append("END")
    out_pdb.write_text("\n".join(out) + "\n")


def crystal_reference() -> Path:
    """1PZO chain A protein + CBT 300, original coords. Reference for everything."""
    out = COMPLEX_DIR / "00_crystal_1PZO_chainA_CBT300.pdb"
    full = parsePDB(str(CRYSTAL_PDB))
    prot = full.select("protein and chain A")
    cbt = full.select("resname CBT and resnum 300")
    rec_tmp = COMPLEX_DIR / "_rec_crystal.pdb"
    lig_tmp = COMPLEX_DIR / "_lig_crystal.pdb"
    writePDB(str(rec_tmp), prot)
    writePDB(str(lig_tmp), cbt)
    # Normalize CBT atom records (already PDB; just retag chain L, resnum 999 for consistency)
    lines = []
    for orig in lig_tmp.read_text().splitlines():
        if orig.startswith(("ATOM  ", "HETATM")) and len(orig) >= 26:
            ln = ("HETATM" + orig[6:17] + "CBT" + orig[20]
                  + "L" + " 999" + orig[26:])
            lines.append(ln)
    lig_tmp.write_text("\n".join(lines) + "\n")
    write_complex(rec_tmp, lig_tmp, out)
    rec_tmp.unlink(missing_ok=True)
    lig_tmp.unlink(missing_ok=True)
    return out


def self_dock_complex() -> Path:
    """1PZO chain A protein (apo) + self-dock mode-1 pose (anchor 1.18 Å)."""
    out = COMPLEX_DIR / "01_self_dock_1PZO_apo_mode1.pdb"
    pose_sdf = ALIGNED_POSES / "self_dock_self_dock" / "self_dock_mode1_aligned.sdf"
    lig_tmp = COMPLEX_DIR / "_lig_selfdock.pdb"
    cbt_pdb_from_sdf(pose_sdf, lig_tmp)
    write_complex(CRYSTAL_APO, lig_tmp, out)
    lig_tmp.unlink(missing_ok=True)
    return out


def case_complex(case: Case) -> Path:
    """Cα-align a BioEmu frame to 1PZO chain A, write the aligned receptor + the docked pose."""
    out = COMPLEX_DIR / case.out_name
    receptor = parsePDB(str(case.receptor_pdb))
    crystal_apo = parsePDB(str(CRYSTAL_APO))
    matchAlign(receptor, crystal_apo, tarsel="calpha", mosel="calpha")
    rec_tmp = COMPLEX_DIR / f"_rec_{case.tag}.pdb"
    writePDB(str(rec_tmp), receptor)

    pose_sdf = ALIGNED_POSES / f"{case.arm}_{case.tag}" / f"{case.tag}_mode{case.mode}_aligned.sdf"
    lig_tmp = COMPLEX_DIR / f"_lig_{case.tag}_m{case.mode}.pdb"
    cbt_pdb_from_sdf(pose_sdf, lig_tmp)
    write_complex(rec_tmp, lig_tmp, out)
    rec_tmp.unlink(missing_ok=True)
    lig_tmp.unlink(missing_ok=True)
    return out


def write_readme(produced: list[tuple[str, str]]) -> None:
    lines = [
        "TEM-1 CBT re-docking — complex PDB files",
        "=========================================",
        "",
        "Each PDB contains:",
        "  • Protein receptor (TEM-1 β-lactamase, Cα-aligned to 1PZO chain A frame)",
        "  • Docked CBT pose as HETATM (resname CBT, chain L, resnum 999)",
        "",
        "All files are in the same coordinate frame, so loading any two together",
        "(e.g. crystal + a cluster-1 winner) lets you compare the binding pose",
        "directly without further alignment.",
        "",
        "Viewer suggestion (PyMOL):",
        "  load 00_crystal_1PZO_chainA_CBT300.pdb, crystal",
        "  load 10_cluster1_frame63_mode1_BEST.pdb, ph_winner",
        "  hide everything; show cartoon; show sticks, resname CBT",
        "  color cyan, crystal and resname CBT",
        "  color orange, ph_winner and resname CBT",
        "",
        "Files:",
        "------",
    ]
    for name, label in produced:
        lines.append(f"  {name}")
        lines.append(f"      {label}")
        lines.append("")
    (COMPLEX_DIR / "README.txt").write_text("\n".join(lines))


def main() -> int:
    produced: list[tuple[str, str]] = []
    print("crystal reference …")
    p = crystal_reference()
    produced.append((p.name, "Crystal 1PZO chain A protein + crystallographic CBT (resnum 300). Reference structure."))

    print("self-dock control …")
    p = self_dock_complex()
    produced.append((p.name, "Self-dock control: 1PZO apo receptor + best-affinity smina pose of CBT. Anchor RMSD 1.18 Å, 9/22 atoms within 2 Å of crystal."))

    for case in CASES:
        print(f"{case.out_name} …")
        case_complex(case)
        produced.append((case.out_name, case.label))

    write_readme(produced)
    print(f"\nwrote {len(produced)} complex PDBs to {COMPLEX_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
