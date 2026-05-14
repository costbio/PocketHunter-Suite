"""B11.21: tests for tasks._sdf_molecule_names — the SDF molecule-name
parser that lets the docking grid show real ligand names instead of
``<stem>_<N>`` PDBQT stems.
"""
from tasks import _sdf_molecule_names


def _record(title: str, name_field: str | None) -> str:
    """Minimal SDF record. The parser only looks at the title line and
    a ``> <name>`` data field — the molblock body is irrelevant."""
    body = f"{title}\n  prog\n  comment\n  1  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n"
    if name_field is not None:
        body += f"> <name>\n{name_field}\n\n"
    return body


def test_prefers_name_field_over_blank_title(tmp_path):
    # e-Drug3D shape: blank title line, identity in a `> <name>` field.
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(
        _record("", "NITISINONE") + "$$$$\n"
        + _record("", "PAROXETINE") + "$$$$\n"
    )
    assert _sdf_molecule_names(str(sdf)) == ["NITISINONE", "PAROXETINE"]


def test_falls_back_to_title_line(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(_record("Aspirin", None) + "$$$$\n")
    assert _sdf_molecule_names(str(sdf)) == ["Aspirin"]


def test_nameless_record_yields_empty_string(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(_record("", None) + "$$$$\n")
    assert _sdf_molecule_names(str(sdf)) == [""]


def test_record_order_is_preserved(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(
        "".join(_record("", f"MOL{i}") + "$$$$\n" for i in range(1, 6))
    )
    assert _sdf_molecule_names(str(sdf)) == ["MOL1", "MOL2", "MOL3", "MOL4", "MOL5"]


def test_name_tag_is_case_insensitive(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text("blank\n prog\n\n M  END\n> <NAME>\nUPPER\n\n$$$$\n")
    assert _sdf_molecule_names(str(sdf)) == ["UPPER"]


def test_non_sdf_input_returns_single_empty(tmp_path):
    pdb = tmp_path / "lig.pdb"
    pdb.write_text("ATOM      1  C   LIG A   1       0.0   0.0   0.0\n")
    assert _sdf_molecule_names(str(pdb)) == [""]


def test_missing_file_returns_single_empty():
    assert _sdf_molecule_names("/nonexistent/path/lig.sdf") == [""]
