"""Tests for tasks._normalize_sdf_elements — the ligand-prep guard that
rewrites mmCIF-style uppercase element symbols (CL, BR, FE) in SDF atom
blocks to the canonical mixed case (Cl, Br, Fe) the MDL spec requires.

Regression: RCSB / Mol* ModelServer SDF exports use the uppercase form,
which OpenBabel mistypes into untyped PDBQT atoms that smina then rejects
with "ATOM syntax incorrect" — failing the whole docking job.
"""
from tasks import _normalize_sdf_elements


def _atom(x: float, y: float, z: float, symbol: str) -> str:
    """One V2000 atom line: 3x10-char coords, a space, then the element
    symbol left-justified in the 3-char field at columns 32-34."""
    return (
        f"{x:>10.4f}{y:>10.4f}{z:>10.4f} {symbol:<3}"
        "0  0  0  0  0  0  0  0  0  0  0  0\n"
    )


def _molfile(atom_lines: str, natoms: int, *, title: str = "LIG",
             version: str = " V2000") -> str:
    """A minimal single-record V2000 molfile wrapping ``atom_lines``."""
    counts = f"{natoms:>3}  0  0  0  0  0  0  0  0  0999{version}"
    return f"{title}\n  prog\n\n{counts}\n{atom_lines}M  END\n$$$$\n"


def _symbols(path: str) -> list[str]:
    """Element symbols read back from a single-record molfile."""
    lines = open(path).read().split("\n")
    natoms = int(lines[3][0:3])
    return [lines[4 + i][31:34].strip() for i in range(natoms)]


def test_fixes_uppercase_two_letter_elements(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(_molfile(
        _atom(9.135, 8.453, 25.214, "CL")
        + _atom(8.800, 9.470, 26.266, "C")
        + _atom(7.196, 19.565, 29.679, "BR"),
        3,
    ))
    assert _normalize_sdf_elements(str(sdf)) == 2
    assert _symbols(str(sdf)) == ["Cl", "C", "Br"]


def test_canonical_file_is_untouched(tmp_path):
    sdf = tmp_path / "lig.sdf"
    original = _molfile(
        _atom(0.0, 0.0, 0.0, "Cl") + _atom(1.0, 1.0, 1.0, "Fe"), 2,
    )
    sdf.write_text(original)
    assert _normalize_sdf_elements(str(sdf)) == 0
    assert sdf.read_text() == original  # byte-identical, no rewrite


def test_single_letter_elements_unaffected(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(_molfile(
        _atom(0.0, 0.0, 0.0, "C")
        + _atom(1.0, 1.0, 1.0, "N")
        + _atom(2.0, 2.0, 2.0, "O"),
        3,
    ))
    assert _normalize_sdf_elements(str(sdf)) == 0


def test_query_and_wildcard_atoms_left_alone(tmp_path):
    # '*', 'R#' and 'LP' are not elements — title-casing must not touch them.
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(_molfile(
        _atom(0.0, 0.0, 0.0, "*")
        + _atom(1.0, 1.0, 1.0, "R#")
        + _atom(2.0, 2.0, 2.0, "LP"),
        3,
    ))
    assert _normalize_sdf_elements(str(sdf)) == 0
    assert _symbols(str(sdf)) == ["*", "R#", "LP"]


def test_multi_molecule_sdf(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(
        _molfile(_atom(0.0, 0.0, 0.0, "CL"), 1, title="MOL1")
        + _molfile(_atom(1.0, 1.0, 1.0, "BR"), 1, title="MOL2")
    )
    assert _normalize_sdf_elements(str(sdf)) == 2
    content = sdf.read_text()
    assert " Cl " in content and " Br " in content
    assert " CL " not in content and " BR " not in content


def test_v3000_block_is_noop(tmp_path):
    # V3000 counts line reports 0 atoms inline — nothing to walk, no-op.
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(
        "LIG\n  prog\n\n  0  0  0  0  0  0  0  0  0  0999 V3000\n"
        "M  V30 BEGIN CTAB\nM  V30 END CTAB\nM  END\n$$$$\n"
    )
    assert _normalize_sdf_elements(str(sdf)) == 0


def test_coordinates_and_trailing_fields_preserved(tmp_path):
    sdf = tmp_path / "lig.sdf"
    sdf.write_text(_molfile(_atom(9.135, -8.453, 25.214, "CL"), 1))
    _normalize_sdf_elements(str(sdf))
    atom_line = sdf.read_text().split("\n")[4]
    assert atom_line[:31] == f"{9.135:>10.4f}{-8.453:>10.4f}{25.214:>10.4f} "
    assert atom_line[31:34] == "Cl "
    assert atom_line[34:].startswith("0  0  0")


def test_modelserver_cbt_regression(tmp_path):
    # The actual failure: a ModelServer export whose counts line carries
    # no version tag and whose two chlorines are written 'CL'.
    sdf = tmp_path / "1pzo_B_CBT.sdf"
    sdf.write_text(_molfile(
        _atom(9.135, 8.453, 25.214, "CL")
        + _atom(8.800, 9.470, 26.266, "C")
        + _atom(7.196, 19.565, 29.679, "CL"),
        3, title="CBT", version="",
    ))
    assert _normalize_sdf_elements(str(sdf)) == 2
    assert _symbols(str(sdf)) == ["Cl", "C", "Cl"]


def test_missing_file_returns_zero():
    assert _normalize_sdf_elements("/nonexistent/path/lig.sdf") == 0
