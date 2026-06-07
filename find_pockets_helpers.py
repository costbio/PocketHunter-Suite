"""Pure helpers for the Find Pockets page + task.

Kept in its own module so the validation logic stays unit-testable without
booting Celery, Streamlit, or the PocketHunter CLI.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def validate_find_pockets_inputs(
    xtc_file_path: Optional[str],
    topology_file_path: Optional[str],
    pdb_input_dir: Optional[str],
) -> str:
    """Validate the input combination for run_find_pockets_task.

    Returns the mode string:
      - ``"trajectory"`` — both ``xtc_file_path`` and ``topology_file_path`` supplied
      - ``"pdb_dir"`` — only ``pdb_input_dir`` supplied

    Raises ``ValueError`` if the combination is invalid (mixed, partial, or empty).
    """
    has_traj = bool(xtc_file_path) and bool(topology_file_path)
    partial_traj = (bool(xtc_file_path) ^ bool(topology_file_path))
    has_pdbs = bool(pdb_input_dir)

    if partial_traj:
        raise ValueError(
            "Trajectory mode requires BOTH xtc_file_path and topology_file_path."
        )
    if has_traj and has_pdbs:
        raise ValueError(
            "Cannot mix trajectory inputs and pdb_input_dir; pick one mode."
        )
    if has_traj:
        return "trajectory"
    if has_pdbs:
        return "pdb_dir"
    raise ValueError(
        "No inputs provided — supply either (xtc + topology) or pdb_input_dir."
    )


def write_pdb_list_for_detect(pdb_dir: str | Path) -> Path:
    """Write ``pdb_list.ds`` so PocketHunter's ``detect_pockets`` finds PDB inputs.

    The vendored CLI only writes this listing in its own ``extract_to_pdb``
    stage; the ``pdb_dir`` mode of ``run_find_pockets_task`` skips extract
    entirely. Without this file p2rank silently emits zero pockets.

    Returns the written file's path. Raises ``FileNotFoundError`` if the
    directory has no PDB files (cheap sanity check; callers already verify
    upload contents earlier).
    """
    pdb_dir = Path(pdb_dir)
    pdb_files = sorted(p.name for p in pdb_dir.iterdir() if p.suffix == ".pdb")
    if not pdb_files:
        raise FileNotFoundError(f"No .pdb files found in {pdb_dir}")
    out = pdb_dir / "pdb_list.ds"
    out.write_text("\n".join(pdb_files) + "\n")
    return out


def progress_ranges(mode: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return ``((extract_lo, extract_hi), (detect_lo, detect_hi))`` for a mode.

    Trajectory mode splits 0–100 evenly across the two stages (0–50 / 50–100).
    PDB-dir mode skips extraction and gives detect the full ramp (0–100).
    """
    if mode == "trajectory":
        return (0, 50), (50, 100)
    if mode == "pdb_dir":
        return (0, 0), (0, 100)
    raise ValueError(f"Unknown mode: {mode!r}")
