"""Pure helpers for the Find Pockets page + task.

Kept in its own module so the validation logic stays unit-testable without
booting Celery, Streamlit, or the PocketHunter CLI.
"""
from __future__ import annotations

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
