"""SDF-model extraction + affinity classification for the docking flow.

Two pure helpers shared by the panels package and the Mol* annotation
builders:

* :func:`extract_sdf_model` — pulls one model out of a multi-model SDF
  written by smina. Consumed by
  :mod:`components.molstar_annotations.ligand_pose_annotation_from_pose`.
* :func:`classify_affinity` — buckets a smina affinity into
  ``(label, emoji)``. Consumed by :mod:`panels.docking` for the
  results table.

The py3Dmol-based ``show_molecule_3d`` viewer used to live here as well;
it was the docking page's inline 3D pose renderer. B5 replaced it with
the persistent Mol* viewer (see ``components.molstar_viewer``), and B7
dropped the function along with the legacy page.
"""
from __future__ import annotations

import os
from typing import Optional


def extract_sdf_model(sdf_path: str, mode: int) -> Optional[str]:
    """Pull a single model out of a multi-model SDF file.

    ``mode`` is 1-indexed to match SMINA's ``mode`` column. Returns the SDF
    chunk for that model (with a trailing ``$$$$`` delimiter) or ``None``
    if anything is off (missing file, mode out of range, parse error).
    """
    try:
        if not sdf_path or not os.path.exists(sdf_path):
            return None
        with open(sdf_path, 'r') as f:
            content = f.read()
        models = [m for m in content.split('$$$$') if m.strip()]
        idx = int(mode) - 1
        if 0 <= idx < len(models):
            model_text = models[idx].lstrip('\n')
            lines = model_text.split('\n')
            if lines and 'V2000' not in lines[0] and len(lines) > 2:
                for i, line in enumerate(lines[:5]):
                    if 'V2000' in line or 'V3000' in line:
                        if i < 3:
                            model_text = '\n' * (3 - i) + model_text
                        break
            return model_text + '\n$$$$\n'
        return None
    except Exception:
        return None


def classify_affinity(affinity: float) -> tuple[str, str]:
    """Bucket a smina affinity (kcal/mol) into ``(label, emoji)``."""
    if affinity < -10:
        return "excellent", "🟢"
    if affinity < -8:
        return "good", "🟡"
    if affinity < -6:
        return "moderate", "🟠"
    return "poor", "🔴"
