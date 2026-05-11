"""Shared 3D visualization + affinity helpers for the docking pages.

Until now, ``pipeline_app.py`` and ``docking_app.py`` each carried their own
near-identical copies of the SDF parser, binding-site finder, pocket-view
quaternion math, multi-model SDF extractor, the py3Dmol viewer renderer, and
the affinity-classification function. This module is the one source of
truth — both pages import from here.

The Streamlit-touching ``show_molecule_3d`` is module-scoped but does its
``streamlit`` and ``streamlit.components.v1`` imports at call time so the
pure helpers (parser / binding-site / quaternion / extractor) can be
imported in non-UI contexts (tests, Celery workers) without dragging
Streamlit along.
"""
from __future__ import annotations

import math
import os
import re
from typing import Iterable, Optional

# Element list used to recognise atom records inside an SDF block.
_SDF_ELEMENTS: tuple[str, ...] = ('C', 'N', 'O', 'S', 'H', 'F', 'P', 'Cl', 'Br', 'I')


def parse_ligand_coords_from_sdf(sdf_data: str) -> list[tuple[float, float, float]]:
    """Parse (x, y, z) atom coordinates from an SDF molecule block."""
    coords: list[tuple[float, float, float]] = []
    for line in sdf_data.split('\n'):
        parts = line.split()
        if len(parts) >= 4:
            try:
                x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                if parts[3] in _SDF_ELEMENTS:
                    coords.append((x, y, z))
            except (ValueError, IndexError):
                pass
    return coords


def get_binding_site_residues(pdb_data: str, sdf_data: str,
                              distance: float = 5.0) -> list[int]:
    """Return sorted protein residue numbers within ``distance`` Å of any ligand atom."""
    lig_coords = parse_ligand_coords_from_sdf(sdf_data)
    if not lig_coords:
        return []
    resis: set[int] = set()
    for line in pdb_data.split('\n'):
        if line.startswith('ATOM') or line.startswith('HETATM'):
            try:
                px = float(line[30:38])
                py = float(line[38:46])
                pz = float(line[46:54])
                resi = int(line[22:26].strip())
                for lx, ly, lz in lig_coords:
                    dx, dy, dz = px - lx, py - ly, pz - lz
                    if math.sqrt(dx * dx + dy * dy + dz * dz) <= distance:
                        resis.add(resi)
                        break
            except (ValueError, IndexError):
                pass
    return sorted(resis)


def compute_pocket_view_quaternion(pdb_data: str,
                                   sdf_data: str) -> tuple[float, float, float, float]:
    """Quaternion that orients the camera to look into the binding pocket.

    Returns ``(qx, qy, qz, qw)`` suitable for 3Dmol.js ``setView``. Falls back to
    the identity quaternion ``(0, 0, 0, 1)`` if either set of coords is empty.
    """
    lig_coords = parse_ligand_coords_from_sdf(sdf_data)
    prot_coords: list[tuple[float, float, float]] = []
    for line in pdb_data.split('\n'):
        if line.startswith('ATOM') and line[12:16].strip() == 'CA':
            try:
                prot_coords.append((float(line[30:38]), float(line[38:46]), float(line[46:54])))
            except (ValueError, IndexError):
                pass
    if not lig_coords or not prot_coords:
        return (0, 0, 0, 1)
    lc = [sum(c[i] for c in lig_coords) / len(lig_coords) for i in range(3)]
    pc = [sum(c[i] for c in prot_coords) / len(prot_coords) for i in range(3)]
    dx, dy, dz = lc[0] - pc[0], lc[1] - pc[1], lc[2] - pc[2]
    mag = math.sqrt(dx * dx + dy * dy + dz * dz)
    if mag < 0.001:
        return (0, 0, 0, 1)
    dx, dy, dz = dx / mag, dy / mag, dz / mag
    dot = dz
    if dot > 0.9999:
        qx, qy, qz, qw = 0.0, 0.0, 0.0, 1.0
    elif dot < -0.9999:
        qx, qy, qz, qw = 0.0, 1.0, 0.0, 0.0
    else:
        qw = 1 + dot
        qx, qy, qz = dy, -dx, 0.0
        norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
        qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm

    def _qmul(w1, x1, y1, z1, w2, x2, y2, z2):
        return (w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2)

    ax = math.radians(35) / 2
    rw, rx, ry, rz = _qmul(math.cos(ax), math.sin(ax), 0, 0, qw, qx, qy, qz)
    ay = math.radians(85) / 2
    rw, rx, ry, rz = _qmul(math.cos(ay), 0, math.sin(ay), 0, rw, rx, ry, rz)
    return (rx, ry, rz, rw)


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


def show_molecule_3d(
    pdb_data: str,
    sdf_data: Optional[str] = None,
    *,
    width: int = 800,
    height: int = 600,
    style_protein: str = "cartoon",
    style_ligand: str = "stick",
    color_scheme: str = "spectrum",
    surface_opacity: float = 0.7,
) -> None:
    """Render a protein + optional ligand in an interactive py3Dmol viewer.

    Imports Streamlit at call time so the pure helpers above can be imported
    in non-UI contexts. ``style_protein`` ∈ {cartoon, surface, stick, binding site}.
    """
    import py3Dmol
    from streamlit.components.v1 import html as st_html

    view = py3Dmol.view(width=width, height=height)

    # Add both models first so 'within' / cross-model selectors work.
    if pdb_data:
        view.addModel(pdb_data, 'pdb')
    if sdf_data:
        view.addModel(sdf_data, 'sdf')

    if pdb_data:
        if style_protein == "binding site":
            view.setStyle({'model': 0}, {'stick': {'colorscheme': color_scheme}})
            if sdf_data:
                binding_resis = get_binding_site_residues(pdb_data, sdf_data, distance=5.0)
                if binding_resis:
                    view.addSurface(py3Dmol.VDW, {'opacity': 0.85, 'color': 'white'},
                                    {'model': 0, 'resi': binding_resis}, {'model': 0})
        elif style_protein == "cartoon":
            view.setStyle({'model': 0}, {'cartoon': {'color': color_scheme}})
        elif style_protein == "surface":
            view.setStyle({'model': 0}, {'cartoon': {'color': color_scheme, 'opacity': 0.3}})
            view.addSurface(py3Dmol.VDW,
                            {'opacity': surface_opacity, 'color': color_scheme},
                            {'model': 0})
        elif style_protein == "stick":
            view.setStyle({'model': 0}, {'stick': {'colorscheme': color_scheme}})

    if sdf_data:
        view.setStyle({'model': 1}, {'stick': {'colorscheme': 'greenCarbon', 'radius': 0.2}})
        view.center({'model': 1})

    view.zoomTo()
    view.spin(False)

    viewer_html = view._make_html()
    viewer_match = re.search(r'(viewer_\w+)', viewer_html)
    viewer_var = viewer_match.group(1) if viewer_match else 'viewer'

    has_ligand = sdf_data is not None
    qx, qy, qz, qw = (0.0, 0.0, 0.0, 1.0)
    binding_resis_js = "[]"
    if has_ligand and pdb_data:
        qx, qy, qz, qw = compute_pocket_view_quaternion(pdb_data, sdf_data)
        resis = get_binding_site_residues(pdb_data, sdf_data, distance=8.0)
        if resis:
            binding_resis_js = str(resis)

    btn_style = (
        "padding:4px 10px; border:1px solid rgba(255,255,255,0.3); border-radius:6px; "
        "background:rgba(0,0,0,0.45); color:white; cursor:pointer; font-size:11px; "
        "backdrop-filter:blur(4px); transition:background 0.2s;"
    )
    btn_disabled_style = btn_style + "opacity:0.3;pointer-events:none;"

    focus_js = (
        f"var v={viewer_var}.getView();"
        f"v[4]={qx:.6f};v[5]={qy:.6f};v[6]={qz:.6f};v[7]={qw:.6f};"
        f"{viewer_var}.setView(v);"
        f"{viewer_var}.zoomTo({{model:0,resi:{binding_resis_js}}},{{padding:5}});"
        f"{viewer_var}.render();"
    )

    buttons_html = f"""
    <div style="position:absolute; bottom:8px; left:50%; transform:translateX(-50%);
                display:flex; gap:6px; z-index:10;">
        <button onclick="{focus_js}"
            style="{btn_disabled_style if not has_ligand else btn_style}"
            onmouseover="this.style.background='rgba(0,0,0,0.65)'"
            onmouseout="this.style.background='rgba(0,0,0,0.45)'"
            {'disabled' if not has_ligand else ''}>🔍 Binding Site</button>
        <button onclick="{viewer_var}.zoomTo({{model:0}});{viewer_var}.render();"
            style="{btn_style}"
            onmouseover="this.style.background='rgba(0,0,0,0.65)'"
            onmouseout="this.style.background='rgba(0,0,0,0.45)'">🏠 Protein</button>
        <button onclick="
            var uri = {viewer_var}.pngURI();
            var a = document.createElement('a');
            a.href = uri;
            a.download = 'docking_snapshot.png';
            a.click();"
            style="{btn_style}"
            onmouseover="this.style.background='rgba(0,0,0,0.65)'"
            onmouseout="this.style.background='rgba(0,0,0,0.45)'">📸 Snapshot</button>
    </div>"""

    html = f'<div style="position:relative;">{viewer_html}{buttons_html}</div>'
    st_html(html, height=height + 50, scrolling=False)
