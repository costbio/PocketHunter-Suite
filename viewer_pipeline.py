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

import json
import logging
import math
import os
import re
import shutil
from pathlib import Path

from task_errors import ViewerConversionError

logger = logging.getLogger(__name__)


# Output filename in the job's results directory.
VIEWER_FILE_NAME = "viewer.pdb"
# Manifest written alongside viewer.pdb. Records the uniform stride
# applied at build time + the logical-filename → viewer-model-index map
# so panels can tell which logical frames are in the strided set and
# which need on-demand single-frame loading.
VIEWER_INDEX_NAME = "viewer_index.json"

# Format identifier consumed by ``molstar_viewer.loadStructureFromUrl(url, fmt)``
# on the JS side. Multi-model PDB (NUMMDL + MODEL/ENDMDL blocks) is what
# Mol* parses cleanly: gemmi's mmCIF output omits ``label_seq_id`` /
# ``_entity_poly_seq``, which made Mol*'s auto-preset fall back to
# all-atom spheres / ball-and-stick. PDB carries polymer connectivity
# implicitly via consecutive ATOM records on the same chain — Mol*
# recognises it and renders cartoon by default.
VIEWER_FILE_FORMAT = "pdb"

# Hard cap on the input PDB-folder total size. Now env-configurable via
# settings.MAX_VIEWER_BYTES (default 1 GB). Read at module-load time;
# orchestrator-spawned workers re-import on respawn so .env changes
# propagate after a worker recycle. Tests can monkeypatch this module
# attribute directly to exercise the size-cap branch.
from settings import settings as _settings  # local import: viewer_pipeline ships in worker image
MAX_VIEWER_BYTES = _settings.MAX_VIEWER_BYTES
# Default cap on viewer-baked model count. Tests monkeypatch this module
# attribute directly to exercise the stride branch on tiny fixtures.
MAX_VIEWER_LOADED_FRAMES = _settings.MAX_VIEWER_LOADED_FRAMES


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


def _compute_stride(n: int, cap: int) -> int:
    """Smallest stride ``s`` such that ``ceil(n/s) <= cap`` (and ``s >= 1``).

    ``n <= cap`` → ``s = 1`` (no decimation). Otherwise the strided slice
    ``[::s]`` is guaranteed to land inside ``cap`` elements.
    """
    if n <= 0 or cap <= 0:
        return 1
    if n <= cap:
        return 1
    return math.ceil(n / cap)


def write_viewer_index(
    out_path: Path,
    all_files: list[Path],
    selected: list[Path],
    stride: int,
) -> Path:
    """Serialise the stride manifest next to ``viewer.pdb``.

    Manifest schema::

        {
          "stride": 20,
          "n_extracted_frames": 4000,
          "n_viewer_models": 200,
          "logical_to_viewer": {"<filename>": <1-based model index>, ...}
        }

    Filenames not in ``logical_to_viewer`` are by definition out-of-strided
    and must be loaded as single-frame structures from
    ``static/<short>/frames/<filename>``.
    """
    out_path = Path(out_path)
    manifest_path = out_path.parent / VIEWER_INDEX_NAME
    logical_to_viewer = {
        p.name: idx for idx, p in enumerate(selected, start=1)
    }
    payload = {
        "stride": int(stride),
        "n_extracted_frames": len(all_files),
        "n_viewer_models": len(selected),
        "logical_to_viewer": logical_to_viewer,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2))
    return manifest_path


def convert_pdb_dir_to_viewer(pdb_dir: Path, out_path: Path) -> dict:
    """Concatenate every ``*.pdb`` in ``pdb_dir`` into a multi-model PDB.

    Applies a uniform stride when the per-frame PDB count exceeds
    :data:`MAX_VIEWER_LOADED_FRAMES`: the strided slice ``[::stride]``
    becomes the set baked into the viewer file; the omitted PDBs stay
    on disk and load on demand when a user picks a non-baked frame via
    a pocket / cluster rep / docking receptor row.

    A manifest ``viewer_index.json`` is written next to ``out_path`` so
    panels can answer "is this logical filename in the strided set?"
    without re-reading the directory.

    Args:
        pdb_dir: Directory of per-frame PDB files (one model per file).
            Files are processed in sorted filename order so frame order
            is deterministic across runs.
        out_path: Where to write the combined multi-model PDB.

    Returns:
        Dict with ``viewer_file_path`` (str), ``viewer_index_path`` (str),
        ``viewer_stride`` (int), ``n_viewer_models`` (int),
        ``n_extracted_frames`` (int).

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

    stride = _compute_stride(len(pdb_files), MAX_VIEWER_LOADED_FRAMES)
    selected = pdb_files[::stride] if stride > 1 else list(pdb_files)

    combined = gemmi.Structure()
    combined.name = "trajectory"
    combined.spacegroup_hm = "P 1"

    for idx, pdb_file in enumerate(selected, start=1):
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

    manifest_path = write_viewer_index(out_path, pdb_files, selected, stride)

    return {
        "viewer_file_path": str(out_path),
        "viewer_index_path": str(manifest_path),
        "viewer_stride": int(stride),
        "n_viewer_models": len(selected),
        "n_extracted_frames": len(pdb_files),
    }


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

    Also mirrors the manifest (``viewer_index.json``) and copies every
    per-frame PDB from ``<viewer-dir>/pdbs/`` into ``static/<short>/frames/``
    so the browser can fetch any logical frame on demand for the
    out-of-strided / single-frame mode.

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
    needs_copy = True
    try:
        src_stat = viewer_file_path.stat()
        if target.exists() and not target.is_symlink():
            dst_stat = target.stat()
            if (
                src_stat.st_size == dst_stat.st_size
                and src_stat.st_mtime_ns == dst_stat.st_mtime_ns
            ):
                needs_copy = False
    except OSError:
        # If we can't stat one of them, fall through and recopy.
        pass

    if needs_copy:
        # Remove any stale destination (symlink from a pre-B9 version, partial copy).
        if target.exists() or target.is_symlink():
            try:
                target.unlink()
            except OSError:
                # Fall through; copy2 below will raise a clearer error.
                pass
        shutil.copy2(viewer_file_path, target)

    # Mirror the stride manifest alongside the viewer file. Failures here
    # are non-fatal — without a manifest the viewer fragment treats every
    # frame as out-of-strided (degraded but correct).
    src_manifest = viewer_file_path.parent / VIEWER_INDEX_NAME
    dst_manifest = static_root / VIEWER_INDEX_NAME
    if src_manifest.exists():
        try:
            shutil.copy2(src_manifest, dst_manifest)
        except OSError as exc:
            logger.warning(f"viewer_index copy failed for {session_short}: {exc}")

    # Mirror the per-frame PDBs into static/<short>/frames/ so the
    # focused (single-frame) mode can request any logical frame the user
    # picks. Copies are size-cheap (per-frame PDBs ~ tens of KB without
    # solvent); idempotent via the same size+mtime check.
    src_pdb_dir = viewer_file_path.parent / "pdbs"
    if src_pdb_dir.exists() and src_pdb_dir.is_dir():
        frames_root = static_root / "frames"
        frames_root.mkdir(parents=True, exist_ok=True)
        for src in src_pdb_dir.glob("*.pdb"):
            dst = frames_root / src.name
            try:
                if dst.exists() and not dst.is_symlink():
                    src_stat = src.stat()
                    dst_stat = dst.stat()
                    if (
                        src_stat.st_size == dst_stat.st_size
                        and src_stat.st_mtime_ns == dst_stat.st_mtime_ns
                    ):
                        continue
                if dst.exists() or dst.is_symlink():
                    try:
                        dst.unlink()
                    except OSError:
                        pass
                shutil.copy2(src, dst)
            except OSError as exc:
                logger.warning(
                    f"frame copy {src.name} → {dst} failed: {exc}"
                )

    return f"{_STATIC_URL_PREFIX}/{session_short}/{VIEWER_FILE_NAME}"
