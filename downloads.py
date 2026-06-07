"""Pure helpers for the cross-panel download buttons.

Centralised so the cluster panel, docking panel, and the Mol* viewer
fragment share one ZIP-builder + view-bytes pipeline. No Streamlit
imports — every helper takes bytes / paths / dicts and returns the
same, so the tests can exercise the contracts directly.

Three primitives:

* :func:`build_pocket_zip` — ZIP of per-frame PDBs + a ``metadata.csv``.
  Reused for cluster-bulk and docking-bucket downloads.
* :func:`build_complex_zip` — ZIP of a receptor PDB + a ligand-pose SDF
  for per-row complex downloads in the docking results.
* :func:`current_view_artifact` — turn the Mol* viewer's currently
  rendered frame (+ any active ligand pose) into a single downloadable
  blob with a predictable filename + MIME.

All three honour ``cap_bytes`` where relevant; the caller passes the
shared :data:`config.Config.MAX_DOWNLOAD_ZIP_SIZE`.
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Iterable, Optional


# MIME types used by Streamlit's download_button — kept as module
# constants so call sites don't have to remember the strings.
MIME_PDB = "chemical/x-pdb"
MIME_SDF = "chemical/x-mdl-sdfile"
MIME_ZIP = "application/zip"


def _metadata_csv_bytes(rows: list[dict]) -> bytes:
    """Render metadata rows as a UTF-8 CSV.

    The first row's keys define the columns; subsequent rows are
    ``.get()``-ed against the same keys (missing values render as
    empty cells).
    """
    if not rows:
        return b""
    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    return buf.getvalue().encode("utf-8")


def build_pocket_zip(
    pdb_sources: Iterable[tuple[Path, str]],
    metadata_rows: list[dict],
    *,
    cap_bytes: int,
) -> tuple[bytes, list[str]]:
    """Build a ZIP of per-frame PDBs alongside an embedded ``metadata.csv``.

    Args:
        pdb_sources: Iterable of ``(on_disk_path, arcname)`` pairs. The
            arcname is what appears inside the ZIP — typically the
            original filename (``trajectory_..._fit_84.pdb``).
        metadata_rows: One row per included pocket. Keys define the
            CSV columns; ``filename`` should be present so the user
            can join the CSV back to the PDB files.
        cap_bytes: Soft cap on the resulting archive's source size.
            Add files **smallest-first**; once the next file would
            push the running total past the cap, omit it (and every
            subsequent file) — the caller surfaces ``omitted_names``
            so the UI can warn.

    Returns:
        ``(zip_bytes, omitted_names)`` — ``omitted_names`` lists arc
        names that were dropped because of the cap.
    """
    # Materialise + filter to existing files. Carry size for the cap.
    entries: list[tuple[Path, str, int]] = []
    for src, arc in pdb_sources:
        try:
            size = src.stat().st_size
        except OSError:
            continue
        entries.append((src, arc, size))

    # Smallest-first so the cap maximises the count of included files.
    entries.sort(key=lambda e: e[2])

    buf = io.BytesIO()
    omitted: list[str] = []
    running = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if metadata_rows:
            zf.writestr("metadata.csv", _metadata_csv_bytes(metadata_rows))
        for src, arc, size in entries:
            if cap_bytes > 0 and running + size > cap_bytes:
                omitted.append(arc)
                continue
            zf.write(src, arcname=arc)
            running += size
    return buf.getvalue(), omitted


def build_complex_zip(
    receptor_pdb: Path,
    ligand_sdf_bytes: bytes,
    *,
    receptor_arcname: str = "receptor.pdb",
    ligand_arcname: str = "ligand.sdf",
    extra_metadata: Optional[dict] = None,
) -> bytes:
    """ZIP a receptor PDB with a ligand-pose SDF for a per-complex download.

    The SDF is passed as bytes (not a path) because docking ligand
    poses are often in-memory results read from a multi-model SDF —
    callers typically extract the best pose's record before zipping.

    ``extra_metadata`` is optional; when present it lands as
    ``metadata.csv`` with a single row in the archive.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(receptor_pdb, arcname=receptor_arcname)
        zf.writestr(ligand_arcname, ligand_sdf_bytes)
        if extra_metadata:
            zf.writestr(
                "metadata.csv", _metadata_csv_bytes([extra_metadata])
            )
    return buf.getvalue()


def current_view_artifact(
    *,
    structure_path: Path,
    ligand_pose_sdf: Optional[str],
    session_short: str,
    frame_label: str,
    ligand_label: Optional[str] = None,
) -> tuple[bytes, str, str]:
    """Render the Mol* viewer's current view as a downloadable artefact.

    With no active ligand pose, returns the single-frame PDB as raw
    bytes. With a ligand pose active, bundles the receptor PDB + the
    pose SDF into a ZIP.

    Args:
        structure_path: On-disk path to the currently rendered PDB.
        ligand_pose_sdf: SDF text of the active ligand pose, or
            ``None`` when no pose is shown.
        session_short: Already-sanitised session short_code used in
            the filename so off-platform downloads stay attributable.
        frame_label: Human-friendly frame identifier (``"F84"``,
            ``"frame_84"``).
        ligand_label: Ligand name embedded in the ZIP filename when
            a pose is active. Optional.

    Returns:
        ``(bytes, filename, mime)`` tuple — feeds straight into
        ``st.download_button``.
    """
    pdb_bytes = structure_path.read_bytes()
    base = f"{session_short}_{frame_label}"

    if not ligand_pose_sdf:
        return pdb_bytes, f"{base}.pdb", MIME_PDB

    # Receptor + ligand pose → ZIP. ``build_complex_zip`` already
    # handles the SDF-from-bytes path; just route the receptor file
    # through the same packer.
    suffix = f"_{ligand_label}" if ligand_label else ""
    zip_bytes = build_complex_zip(
        receptor_pdb=structure_path,
        ligand_sdf_bytes=ligand_pose_sdf.encode("utf-8"),
        receptor_arcname=f"{base}.pdb",
        ligand_arcname=f"{base}{suffix}_pose.sdf",
    )
    return zip_bytes, f"{base}{suffix}_complex.zip", MIME_ZIP
