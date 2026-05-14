"""Convert per-frame PDBs into the multi-model file the Mol* viewer loads.

v2 Phase B commit B2. The find_pockets task already extracts one ``.pdb`` per
frame into ``results/<job>/pdbs/`` (and the PDB-ZIP-input mode produces the
same layout). This module concatenates that folder into a single multi-model
structure and writes it as ``viewer.cif`` in the job's results directory.

**Format note (mmCIF, not BCIF):** the strategy doc commits to BCIF, but
gemmi 0.7.5 doesn't yet expose its binary-CIF writer to Python. Text mmCIF
is what we ship for now — ~30% larger on disk than BCIF, but Mol* accepts
both via ``loadStructureFromUrl(url, "mmcif")``. When gemmi adds a Python
BCIF writer, swap the call here and the filename without touching anything
else.

**Failure semantics:** conversion is *opportunistic*. If anything in this
module raises, the calling task swallows the error and records it in
``Job.result_info["viewer_file_error"]``. The analysis itself never fails
because the viewer-format conversion failed.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from task_errors import ViewerConversionError

logger = logging.getLogger(__name__)


# Output filename in the job's results directory.
VIEWER_FILE_NAME = "viewer.pdb"

# Format identifier consumed by ``molstar_viewer.loadStructureFromUrl(url, fmt)``
# on the JS side. Multi-model PDB (NUMMDL + MODEL/ENDMDL blocks) is what
# Mol* parses cleanly: gemmi's mmCIF output omits ``label_seq_id`` /
# ``_entity_poly_seq``, which made Mol*'s auto-preset fall back to
# all-atom spheres / ball-and-stick. PDB carries polymer connectivity
# implicitly via consecutive ATOM records on the same chain — Mol*
# recognises it and renders cartoon by default.
VIEWER_FILE_FORMAT = "pdb"

# Hard cap on the input PDB-folder total size. Text multi-model PDB is
# roughly the same magnitude as the input PDBs. 1 GB headroom — Mol*
# handles trajectories of this size; below this it's safer to ship the
# file than skip the viewer entirely. (Bumped from 500 MB on
# 2026-05-13 after a 2501-frame trajectory was just over the old cap.)
# Revisit again once BCIF compression is available.
MAX_VIEWER_BYTES = 1024 * 1024 * 1024


def estimate_viewer_size(pdb_dir: Path) -> int:
    """Conservative estimate of the output file size from the input PDB folder.

    Returns the total byte count of all ``*.pdb`` files. mmCIF is similar
    size to PDB for the atom records that dominate; this is a safe upper
    bound for the size-flag check.
    """
    total = 0
    for p in Path(pdb_dir).glob("*.pdb"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


_FRAME_NUM_RE = re.compile(r"_(\d+)\.pdb$")


def frame_number_from_filename(name: str) -> int:
    """Extract the trailing frame index from a per-frame PDB filename.

    pockethunter names extracted frames ``trajectory_..._fit_<N>.pdb``
    or ``frame_<N>.pdb``. We pull ``<N>`` so the combined multi-model
    PDB can be ordered numerically — lexicographic sort would put
    ``_fit_10.pdb`` before ``_fit_2.pdb`` and break the
    "click-a-pocket-row jumps to its source frame" mapping.

    Returns ``0`` when the filename doesn't match the pattern.
    """
    m = _FRAME_NUM_RE.search(name)
    return int(m.group(1)) if m else 0


def sorted_frame_pdbs(pdb_dir: Path) -> list[Path]:
    """Return ``*.pdb`` files in numerical-frame order.

    Public helper so :mod:`panels._shared.frame_index_for_filename`
    uses the exact same ordering ``convert_pdb_dir_to_viewer`` uses
    when building the multi-model PDB.
    """
    return sorted(
        Path(pdb_dir).glob("*.pdb"),
        key=lambda p: (frame_number_from_filename(p.name), p.name),
    )


def convert_pdb_dir_to_viewer(pdb_dir: Path, out_path: Path) -> Path:
    """Concatenate every ``*.pdb`` in ``pdb_dir`` into a multi-model PDB.

    Args:
        pdb_dir: Directory of per-frame PDB files (one model per file).
            Files are processed in sorted filename order so frame order
            is deterministic across runs.
        out_path: Where to write the combined multi-model PDB.

    Returns:
        ``out_path`` on success.

    Raises:
        ViewerConversionError: No PDB files found, or any per-file read
            or final write fails. Caller (``tasks._write_viewer_file``)
            catches this; the analysis-side task never sees it.
    """
    import gemmi  # local import keeps the test fixtures fast

    pdb_dir = Path(pdb_dir)
    out_path = Path(out_path)

    pdb_files = sorted_frame_pdbs(pdb_dir)
    if not pdb_files:
        raise ViewerConversionError(f"No .pdb files in {pdb_dir}")

    combined = gemmi.Structure()
    combined.name = "trajectory"
    combined.spacegroup_hm = "P 1"

    for idx, pdb_file in enumerate(pdb_files, start=1):
        try:
            single = gemmi.read_structure(str(pdb_file))
        except Exception as e:  # gemmi raises a runtime error subclass
            raise ViewerConversionError(f"Failed to read {pdb_file.name}: {e}") from e

        # Each per-frame PDB normally contains exactly one model. Renumber
        # 1..N in the combined structure so Mol* shows a clean frame slider.
        for src_model in single:
            new_model = gemmi.Model(str(idx))
            for chain in src_model:
                new_model.add_chain(chain.clone())
            combined.add_model(new_model)

    if len(combined) == 0:
        raise ViewerConversionError(f"No models extracted from {pdb_dir}")

    # PDB doesn't strictly need ``setup_entities`` (entity blocks live in
    # mmCIF), but it's cheap and harmless — leaves gemmi's structure model
    # in a consistent state.
    combined.setup_entities()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        combined.write_pdb(str(out_path))
    except Exception as e:
        raise ViewerConversionError(f"PDB write to {out_path} failed: {e}") from e

    return out_path


# Streamlit static-serving URL prefix. With enableStaticServing=true,
# files under ``<repo-root>/static/<...>`` are served at ``/app/static/...``.
# Verify with ``curl -sI http://<host>:8501/app/static/<short>/viewer.cif``
# — a real file there returns ``content-type: chemical/x-cif``. (Note:
# ``/static/`` is Streamlit's own frontend asset directory and falls back
# to ``index.html`` for unknown paths, which is what tripped the B9
# investigation. ``/app/static/`` is the user-content route.)
_STATIC_URL_PREFIX = "/app/static"


def link_viewer_for_session(
    session_short: str,
    viewer_file_path: Path,
    base_dir: Path,
) -> str:
    """Materialise ``viewer_file_path`` under ``<base_dir>/static/<short>/`` so the
    browser can fetch it via Streamlit's static-files route.

    **Uses real file copies, not symlinks.** Streamlit's static-file
    handler returns HTTP 400 for any symlink whose target resolves
    outside ``./static/`` (security policy) — and our targets always
    live in ``./results/<job>/viewer.cif``, which is outside. A copy
    is bulletproof; the ~20 MB-per-session disk cost is bounded by the
    existing ``cleanup_job`` pruning the per-session results dir.

    The target is updated only when the source's mtime differs from
    the destination's (idempotent — re-renders that don't change the
    source don't recopy the 20 MB).

    Args:
        session_short: The session's ``short_code`` — used as the directory
            name under ``static/``. Cross-session collisions are prevented
            by short_code uniqueness.
        viewer_file_path: Absolute path to the source ``viewer.cif`` produced
            by :func:`convert_pdb_dir_to_viewer`.
        base_dir: Repo-root directory under which ``static/`` lives. Pass
            ``Path(__file__).parent`` for the canonical layout.

    Returns:
        URL path the Mol* component should fetch, e.g.
        ``"/app/static/<short>/viewer.cif"``.

    Raises:
        OSError: If the copy fails (disk full, source unreadable, target
            directory not writable).
    """
    session_short = str(session_short)
    viewer_file_path = Path(viewer_file_path).resolve()
    static_root = Path(base_dir) / "static" / session_short
    static_root.mkdir(parents=True, exist_ok=True)
    target = static_root / VIEWER_FILE_NAME

    # Idempotency: skip the copy when the destination already mirrors the
    # source. Compare size + nanosecond mtime (``shutil.copy2`` preserves
    # ``st_mtime_ns``, so the destination's ns-mtime equals the source's
    # after the first copy). Cheaper than a hash on a 20 MB file.
    try:
        src_stat = viewer_file_path.stat()
        if target.exists() and not target.is_symlink():
            dst_stat = target.stat()
            if (
                src_stat.st_size == dst_stat.st_size
                and src_stat.st_mtime_ns == dst_stat.st_mtime_ns
            ):
                return f"{_STATIC_URL_PREFIX}/{session_short}/{VIEWER_FILE_NAME}"
    except OSError:
        # If we can't stat one of them, fall through and recopy.
        pass

    # Remove any stale destination (symlink from a pre-B9 version, partial copy).
    if target.exists() or target.is_symlink():
        try:
            target.unlink()
        except OSError:
            # Fall through; copy2 below will raise a clearer error.
            pass

    shutil.copy2(viewer_file_path, target)
    return f"{_STATIC_URL_PREFIX}/{session_short}/{VIEWER_FILE_NAME}"
