"""Helpers for tracking per-receptor/ligand docking pair failures.

``run_docking_task`` and ``run_pockethunter_pipeline`` loop over receptor ×
ligand pairs and call smina for each. When a pair fails (segfault, OOM,
malformed PDBQT, smina returning unparseable output), the task would
otherwise log a warning and silently omit the pair from the results.
This module is the bookkeeping layer: ``build_pair_failure_record``
produces a structured dict the task appends to a list, and
``summarize_pair_failures`` turns that list into a one-line headline for
the UI (rendered by ``failure_view.render_pair_failures_callout``).

The dicts are JSON-friendly so the same shape can also serialise into the
status JSON via ``tasks._update_status_file``'s ``error`` channel — or live
in a plain-text ``docking_pair_failures.log`` next to the existing
``error.log`` from ``_run_stage``.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable


def build_pair_failure_record(
    receptor: str,
    ligand: str,
    exc: Exception,
    stderr_tail: str = "",
    *,
    max_stderr_chars: int = 1500,
) -> dict:
    """Build a JSON-serialisable record for one failed docking pair.

    If ``stderr_tail`` is empty, the helper checks ``exc.stderr`` (which
    ``subprocess.CalledProcessError`` carries) and falls back to ``""``.
    The result is truncated from the *tail* (keeping the most recent
    output) to ``max_stderr_chars`` so a long Java stack trace doesn't
    bloat the in-memory record.
    """
    if not stderr_tail:
        attr = getattr(exc, "stderr", None)
        if attr:
            stderr_tail = str(attr)
    if stderr_tail and len(stderr_tail) > max_stderr_chars:
        stderr_tail = stderr_tail[-max_stderr_chars:]
    return {
        "receptor": receptor,
        "ligand": ligand,
        "exc_type": type(exc).__name__,
        "exc_message": str(exc),
        "stderr_tail": stderr_tail or "",
    }


def summarize_pair_failures(pair_failures: Iterable[dict]) -> str:
    """One-line summary, grouped by exception type (most common first).

    ``"3 pairs failed (2× CalledProcessError, 1× NoPosesParsed)"``
    """
    failures = list(pair_failures)
    if not failures:
        return "No pair failures"

    counts = Counter(rec.get("exc_type") or "Unknown" for rec in failures)
    grouped = ", ".join(f"{n}× {kind}" for kind, n in counts.most_common())
    pluralised = "pair" if len(failures) == 1 else "pairs"
    return f"{len(failures)} {pluralised} failed ({grouped})"
