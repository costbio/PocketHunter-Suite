"""Compact human-readable labels for protein binding-pocket clusters.

The clustering output stores per-pocket residue lists as space-separated
``<chain>_<resnum>`` tokens (e.g. ``"A_807 A_810 B_45"``). These helpers
parse those tokens and render a short spatial summary suitable for UI
labels — so a biologist can tell which physical pocket a cluster
corresponds to without exporting to PyMOL.
"""
from __future__ import annotations

import math
import re
from typing import Optional

_TOKEN_RE = re.compile(r"^([A-Za-z0-9]+)_(\d+)$")


def parse_residue_tokens(residues: Optional[str | float]) -> dict[str, list[int]]:
    """Parse a residues string into {chain: [resnum, ...sorted]}.

    Accepts space- or comma-separated tokens. Silently skips malformed tokens.
    Returns an empty dict for None, NaN, or empty strings.
    """
    if residues is None:
        return {}
    if isinstance(residues, float) and math.isnan(residues):
        return {}
    text = str(residues).strip()
    if not text:
        return {}

    by_chain: dict[str, list[int]] = {}
    for raw in re.split(r"[\s,]+", text):
        if not raw:
            continue
        m = _TOKEN_RE.match(raw)
        if not m:
            continue
        chain, num = m.group(1), int(m.group(2))
        by_chain.setdefault(chain, []).append(num)

    for chain in by_chain:
        by_chain[chain].sort()
    return by_chain


def _compress_runs(nums: list[int]) -> str:
    """Turn a sorted residue list into 'a, b–c, d' form."""
    if not nums:
        return ""
    parts: list[str] = []
    run_start = nums[0]
    prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(str(run_start) if run_start == prev else f"{run_start}–{prev}")
        run_start = prev = n
    parts.append(str(run_start) if run_start == prev else f"{run_start}–{prev}")
    return ", ".join(parts)


def describe_cluster_spatially(residues: Optional[str | float]) -> str:
    """Compact spatial summary of a pocket's residues.

    Examples
    --------
    >>> describe_cluster_spatially("A_5 A_6 A_7 A_8")
    'A · 5–8'
    >>> describe_cluster_spatially("A_5 A_6 B_45")
    'A · 5–6 | B · 45'
    >>> describe_cluster_spatially("")
    '—'
    """
    by_chain = parse_residue_tokens(residues)
    if not by_chain:
        return "—"
    chains = sorted(by_chain.keys())
    return " | ".join(f"{c} · {_compress_runs(by_chain[c])}" for c in chains)
