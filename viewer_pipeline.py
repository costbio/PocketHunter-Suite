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
from pathlib import Path

from task_errors import ViewerConversionError

logger = logging.getLogger(__name__)


# Output filename in the job's results directory.
VIEWER_FILE_NAME = "viewer.cif"

# Format identifier consumed by ``molstar_viewer.loadStructureFromUrl(url, fmt)``
# on the JS side. Stays "mmcif" while we ship text CIF; switch to "bcif"
# when we move to binary.
VIEWER_FILE_FORMAT = "mmcif"

# Hard cap on the input PDB-folder total size. Text mmCIF is roughly the
# same magnitude as the input PDBs. 500 MB keeps the browser-side parse
# tractable; revisit when BCIF arrives.
MAX_VIEWER_BYTES = 500 * 1024 * 1024


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


def convert_pdb_dir_to_viewer(pdb_dir: Path, out_path: Path) -> Path:
    """Concatenate every ``*.pdb`` in ``pdb_dir`` into a multi-model mmCIF.

    Args:
        pdb_dir: Directory of per-frame PDB files (one model per file).
            Files are processed in sorted filename order so frame order
            is deterministic across runs.
        out_path: Where to write the combined mmCIF.

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

    pdb_files = sorted(pdb_dir.glob("*.pdb"))
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

    # mmCIF requires entity assignment to validate; setup_entities does
    # that based on the structure's content (residue types, chain breaks).
    combined.setup_entities()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc = combined.make_mmcif_document()
        doc.write_file(str(out_path))
    except Exception as e:
        raise ViewerConversionError(f"mmCIF write to {out_path} failed: {e}") from e

    return out_path
