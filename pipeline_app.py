import streamlit as st
import os
import zipfile
import uuid
import json
from datetime import datetime
import time
import math
import re
from pathlib import Path
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import py3Dmol
import streamlit.components.v1 as components
from tasks import run_pockethunter_pipeline, run_docking_task
from celery_app import celery_app
from config import Config
from security import handle_file_upload_secure, SecurityError
from rate_limiter import RateLimitExceeded, check_task_rate_limit
from logging_config import setup_logging
from session_state import initialize_session_state

UPLOAD_DIR = str(Config.UPLOAD_DIR)
RESULTS_DIR = str(Config.RESULTS_DIR)

logger = setup_logging(__name__)

# Custom CSS
st.markdown("""
<style>
    .pipeline-header {
        background: linear-gradient(135deg, #2E7D32 0%, #1565C0 50%, #F57C00 100%);
        padding: 2rem;
        border-radius: 20px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 8px 32px rgba(0,0,0,0.1);
    }
    .pipeline-header h1 { margin: 0; font-size: 2.2rem; font-weight: 700; }
    .stage-indicator {
        display: flex;
        justify-content: space-between;
        margin: 1.5rem 0;
    }
    .stage-box {
        flex: 1;
        text-align: center;
        padding: 0.8rem;
        border-radius: 10px;
        margin: 0 4px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .stage-done { background: #C8E6C9; color: #1B5E20; }
    .stage-active { background: #1565C0; color: white; }
    .stage-pending { background: #E0E0E0; color: #757575; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="pipeline-header">
    <h1>⚡ Full Pipeline</h1>
    <p style="font-size: 1.1rem; margin-top: 0.5rem;">
        Extract → Detect → Cluster → (optional) Dock — all in one shot
    </p>
</div>
""", unsafe_allow_html=True)

# ── Cluster results helpers ─────────────────────────────────────────────

def _resolve_pdb_path(file_name, job_id):
    pdb_name = file_name.replace('_predictions', '') if '_predictions' in file_name else file_name
    if not pdb_name.endswith('.pdb'):
        pdb_name += '.pdb'
    candidate = os.path.join(RESULTS_DIR, job_id, 'pdbs', pdb_name)
    if os.path.exists(candidate):
        return candidate
    candidate2 = os.path.join(RESULTS_DIR, job_id, 'pocket_clusters', pdb_name)
    if os.path.exists(candidate2):
        return candidate2
    return candidate


def _show_molecule_3d_with_pocket(pdb_path, pocket_residues, width=400, height=420):
    try:
        with open(pdb_path, 'r') as f:
            pdb_data = f.read()
        highlight_specs = []
        for res_str in pocket_residues:
            parts = res_str.strip().split('_', 1)
            if len(parts) == 2:
                try:
                    highlight_specs.append({'chain': parts[0], 'resi': int(parts[1])})
                except ValueError:
                    pass
        view = py3Dmol.view(width=width, height=height)
        view.addModel(pdb_data, 'pdb')
        view.setStyle({}, {'cartoon': {'color': 'spectrum'}})
        for spec in highlight_specs:
            view.setStyle({'chain': spec['chain'], 'resi': spec['resi']},
                          {'stick': {'color': 'orange', 'radius': 0.3}})
        if highlight_specs:
            chains = {}
            for s in highlight_specs:
                chains.setdefault(s['chain'], []).append(s['resi'])
            for chain, resis in chains.items():
                view.addSurface(py3Dmol.VDW, {'opacity': 0.4, 'color': 'orange'},
                                {'chain': chain, 'resi': resis})
            view.zoomTo({'resi': [s['resi'] for s in highlight_specs]})
        else:
            view.zoomTo()
        view.spin(False)
        html = f'<div style="border-radius:15px;overflow:hidden;">{view._make_html()}</div>'
        components.html(html, height=height + 50, scrolling=False)
    except Exception as e:
        st.error(f"Error loading 3D structure: {e}")


@st.cache_data(ttl=300)
def _load_clustered_data(path):
    return pd.read_csv(path)


@st.cache_data(ttl=300)
def _load_representatives(path):
    return pd.read_csv(path)


def _classify_affinity(affinity):
    if affinity < -10:
        return "excellent", "🟢"
    elif affinity < -8:
        return "good", "🟡"
    elif affinity < -6:
        return "moderate", "🟠"
    else:
        return "poor", "🔴"


# ── Docking 3D viewer helpers (ported from docking_app.py) ──────────────

_SDF_ELEMENTS = ('C', 'N', 'O', 'S', 'H', 'F', 'P', 'Cl', 'Br', 'I')


def _parse_ligand_coords_from_sdf(sdf_data):
    coords = []
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


def _get_binding_site_residues(pdb_data, sdf_data, distance=5.0):
    lig_coords = _parse_ligand_coords_from_sdf(sdf_data)
    if not lig_coords:
        return []
    resis = set()
    for line in pdb_data.split('\n'):
        if line.startswith('ATOM') or line.startswith('HETATM'):
            try:
                px, py, pz = float(line[30:38]), float(line[38:46]), float(line[46:54])
                resi = int(line[22:26].strip())
                for lx, ly, lz in lig_coords:
                    if math.sqrt((px-lx)**2 + (py-ly)**2 + (pz-lz)**2) <= distance:
                        resis.add(resi)
                        break
            except (ValueError, IndexError):
                pass
    return sorted(resis)


def _compute_pocket_view_quaternion(pdb_data, sdf_data):
    lig_coords = _parse_ligand_coords_from_sdf(sdf_data)
    prot_coords = []
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
    dx, dy, dz = lc[0]-pc[0], lc[1]-pc[1], lc[2]-pc[2]
    mag = math.sqrt(dx*dx + dy*dy + dz*dz)
    if mag < 0.001:
        return (0, 0, 0, 1)
    dx, dy, dz = dx/mag, dy/mag, dz/mag
    dot = dz
    if dot > 0.9999:
        qx, qy, qz, qw = 0, 0, 0, 1
    elif dot < -0.9999:
        qx, qy, qz, qw = 0, 1, 0, 0
    else:
        qw = 1 + dot
        qx, qy, qz = dy, -dx, 0
        norm = math.sqrt(qw*qw + qx*qx + qy*qy + qz*qz)
        qx, qy, qz, qw = qx/norm, qy/norm, qz/norm, qw/norm

    def _qmul(w1, x1, y1, z1, w2, x2, y2, z2):
        return (w1*w2 - x1*x2 - y1*y2 - z1*z2,
                w1*x2 + x1*w2 + y1*z2 - z1*y2,
                w1*y2 - x1*z2 + y1*w2 + z1*x2,
                w1*z2 + x1*y2 - y1*x2 + z1*w2)
    ax = math.radians(35) / 2
    rw, rx, ry, rz = _qmul(math.cos(ax), math.sin(ax), 0, 0, qw, qx, qy, qz)
    ay = math.radians(85) / 2
    rw, rx, ry, rz = _qmul(math.cos(ay), 0, math.sin(ay), 0, rw, rx, ry, rz)
    return (rx, ry, rz, rw)


def _extract_sdf_model(sdf_path, mode):
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


def _show_docking_molecule_3d(pdb_data, sdf_data=None, width=420, height=380,
                               style_protein="cartoon"):
    view = py3Dmol.view(width=width, height=height)
    if pdb_data:
        view.addModel(pdb_data, 'pdb')
    if sdf_data:
        view.addModel(sdf_data, 'sdf')
    if pdb_data:
        if style_protein == "binding site":
            view.setStyle({'model': 0}, {'stick': {'colorscheme': 'spectrum'}})
            if sdf_data:
                binding_resis = _get_binding_site_residues(pdb_data, sdf_data, distance=5.0)
                if binding_resis:
                    view.addSurface(py3Dmol.VDW, {'opacity': 0.85, 'color': 'white'},
                                    {'model': 0, 'resi': binding_resis}, {'model': 0})
        elif style_protein == "cartoon":
            view.setStyle({'model': 0}, {'cartoon': {'color': 'spectrum'}})
        elif style_protein == "surface":
            view.setStyle({'model': 0}, {'cartoon': {'color': 'spectrum', 'opacity': 0.3}})
            view.addSurface(py3Dmol.VDW, {'opacity': 0.7, 'color': 'spectrum'}, {'model': 0})
        elif style_protein == "stick":
            view.setStyle({'model': 0}, {'stick': {'colorscheme': 'spectrum'}})
    if sdf_data:
        view.setStyle({'model': 1}, {'stick': {'colorscheme': 'greenCarbon', 'radius': 0.2}})
        view.center({'model': 1})
    view.zoomTo()
    view.spin(False)

    viewer_html = view._make_html()
    viewer_match = re.search(r'(viewer_\w+)', viewer_html)
    viewer_var = viewer_match.group(1) if viewer_match else 'viewer'

    has_ligand = sdf_data is not None
    qx, qy, qz, qw = (0, 0, 0, 1)
    binding_resis_js = "[]"
    if has_ligand and pdb_data:
        qx, qy, qz, qw = _compute_pocket_view_quaternion(pdb_data, sdf_data)
        resis = _get_binding_site_residues(pdb_data, sdf_data, distance=8.0)
        if resis:
            binding_resis_js = str(resis)

    btn_style = ("padding:4px 10px; border:1px solid rgba(255,255,255,0.3); border-radius:6px; "
                 "background:rgba(0,0,0,0.45); color:white; cursor:pointer; font-size:11px; "
                 "backdrop-filter:blur(4px); transition:background 0.2s;")
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
            {'disabled' if not has_ligand else ''}
            onmouseover="this.style.background='rgba(0,0,0,0.65)'"
            onmouseout="this.style.background='rgba(0,0,0,0.45)'">🔍 Binding Site</button>
        <button onclick="{viewer_var}.zoomTo({{model:0}});{viewer_var}.render();"
            style="{btn_style}"
            onmouseover="this.style.background='rgba(0,0,0,0.65)'"
            onmouseout="this.style.background='rgba(0,0,0,0.45)'">🏠 Protein</button>
        <button onclick="var uri={viewer_var}.pngURI();var a=document.createElement('a');a.href=uri;a.download='docking_snapshot.png';a.click();"
            style="{btn_style}"
            onmouseover="this.style.background='rgba(0,0,0,0.65)'"
            onmouseout="this.style.background='rgba(0,0,0,0.45)'">📸 Snapshot</button>
    </div>"""

    html = f'<div style="position:relative;">{viewer_html}{buttons_html}</div>'
    components.html(html, height=height + 50, scrolling=False)


def _show_pipeline_cluster_inline(results_job_id):
    """Render the heatmap + 3D viewer + docking launch inline on the pipeline page."""
    cluster_output_dir = os.path.join(RESULTS_DIR, results_job_id, "pocket_clusters")
    representatives_file = os.path.join(cluster_output_dir, "cluster_representatives.csv")

    if not os.path.exists(representatives_file):
        st.info("Cluster results not yet available.")
        return

    # Clear heatmap state when job changes
    if st.session_state.get('heatmap_last_job_id') != results_job_id:
        st.session_state.heatmap_selected_cluster_id = None
        st.session_state.heatmap_selected_pdb_path = None
        st.session_state.heatmap_selected_residues = []
        st.session_state.heatmap_docking_clusters = []
        st.session_state.heatmap_last_job_id = results_job_id

    try:
        df_reps = _load_representatives(representatives_file)
        if 'residues' in df_reps.columns and df_reps['residues'].dtype == object:
            df_reps['num_residues'] = df_reps['residues'].apply(
                lambda x: len(str(x).split()) if pd.notna(x) else 0
            )
        else:
            df_reps['num_residues'] = df_reps.get('residues', 0)

        if len(df_reps) == 0:
            st.warning("⚠️ Clustering completed but no representative pockets found.")
            return

        st.markdown("---")
        st.markdown("### 🗺️ Clustering Results")

        clustered_file = os.path.join(cluster_output_dir, "pockets_clustered.csv")
        if not os.path.exists(clustered_file):
            st.info("Heatmap requires pockets_clustered.csv — not found for this job.")
            return

        df_clustered = _load_clustered_data(clustered_file)
        df_clustered = df_clustered[df_clustered['cluster'] != -1]

        meta_cols = {'Frame_pocket_index', 'File name', 'Frame', 'pocket_index',
                     'probability', 'residues', 'cluster', 'num_residues'}
        residue_cols = [c for c in df_clustered.columns if c not in meta_cols]
        if not residue_cols:
            st.warning("No residue columns found in clustered data.")
            return

        unique_clusters = sorted(df_clustered['cluster'].unique())

        if 'cluster' in df_reps.columns:
            cluster_to_rep = {int(row['cluster']): row for _, row in df_reps.iterrows()}
        else:
            cluster_to_rep = {
                clust: df_reps.iloc[i]
                for i, clust in enumerate(unique_clusters)
                if i < len(df_reps)
            }

        # Build consensus heatmap matrix
        consensus_rows, cluster_labels = [], []
        for clust in unique_clusters:
            clust_data = df_clustered[df_clustered['cluster'] == clust]
            consensus_rows.append(clust_data[residue_cols].mean().values)
            cluster_labels.append(
                f"Cluster {clust}  ({len(clust_data)} pockets, avg prob: {clust_data['probability'].mean():.3f})"
            )
        consensus_matrix = np.array(consensus_rows)
        col_mask = consensus_matrix.sum(axis=0) > 0
        filtered_residues = [r for r, m in zip(residue_cols, col_mask) if m]
        filtered_matrix = consensus_matrix[:, col_mask]

        def _res_sort_key(name):
            parts = name.rsplit('_', 1)
            try:
                return (parts[0], int(parts[1]))
            except (ValueError, IndexError):
                return (name, 0)

        sort_order = sorted(range(len(filtered_residues)), key=lambda i: _res_sort_key(filtered_residues[i]))
        filtered_residues = [filtered_residues[i] for i in sort_order]
        filtered_matrix = filtered_matrix[:, sort_order]

        _n_clust = len(unique_clusters)
        _heat_top_margin = 60
        _heat_bot_margin = 100
        height = max(400, _n_clust * 60 + 200)
        _plot_area_h = height - _heat_top_margin - _heat_bot_margin
        _row_h = _plot_area_h / _n_clust
        _cb_h = 36
        _top_pad = max(0, _heat_top_margin + _row_h / 2 - _cb_h / 2)
        _gap = max(0, _row_h - _cb_h)

        fig_heat = go.Figure(data=go.Heatmap(
            z=filtered_matrix,
            x=filtered_residues,
            y=cluster_labels,
            colorscale='YlOrRd',
            zmin=0, zmax=1,
            colorbar=dict(title="Frequency", tickvals=[0, 0.25, 0.5, 0.75, 1.0]),
            hovertemplate="<b>%{y}</b><br>Residue: %{x}<br>Frequency: %{z:.2f}<extra></extra>",
        ))
        fig_heat.update_layout(
            title="Residue Frequency per Cluster",
            xaxis_title="Residue",
            yaxis_title="",
            height=height,
            xaxis=dict(tickangle=45, tickfont=dict(size=9)),
            yaxis=dict(autorange="reversed", showticklabels=False),
            margin=dict(t=_heat_top_margin, l=20, r=20, b=_heat_bot_margin),
        )

        cb_col, heat_col, viewer_col = st.columns([1, 3, 2])

        with cb_col:
            st.markdown("**Select cluster:**")
            st.markdown(f'<div style="height:{_top_pad:.0f}px"></div>', unsafe_allow_html=True)
            for _cid in unique_clusters:
                _rep = cluster_to_rep.get(_cid)
                if _rep is None:
                    continue
                _clust_df = df_clustered[df_clustered['cluster'] == _cid]
                _n = len(_clust_df)
                _avg = _clust_df['probability'].mean()

                def _on_change(_cid=_cid, _rep=_rep, _rj=results_job_id):
                    cb_key = f"pipe_cluster_cb_{_cid}"
                    if st.session_state[cb_key]:
                        _pdb_path = _resolve_pdb_path(_rep['File name'], _rj)
                        _res_raw = str(_rep.get('residues', ''))
                        _res_list = [r.strip() for r in _res_raw.replace(',', ' ').split() if r.strip()]
                        st.session_state.heatmap_selected_cluster_id = _cid
                        st.session_state.heatmap_selected_pdb_path = _pdb_path
                        st.session_state.heatmap_selected_residues = _res_list
                    else:
                        if st.session_state.heatmap_selected_cluster_id == _cid:
                            st.session_state.heatmap_selected_cluster_id = None
                            st.session_state.heatmap_selected_pdb_path = None
                            st.session_state.heatmap_selected_residues = []

                st.checkbox(
                    f"Cluster {_cid}  ({_n} pockets, avg prob: {_avg:.3f})",
                    key=f"pipe_cluster_cb_{_cid}",
                    on_change=_on_change,
                )
                st.markdown(f'<div style="height:{_gap:.0f}px"></div>', unsafe_allow_html=True)

        with heat_col:
            st.plotly_chart(fig_heat, use_container_width=True, key="pipe_consensus_heatmap")
            st.caption(
                "Each row = a cluster. Each column = a residue. "
                "Color = how consistently the residue appears (0 = never, 1 = always)."
            )

        with viewer_col:
            sel_id = st.session_state.heatmap_selected_cluster_id
            if sel_id is not None:
                sel_path = st.session_state.heatmap_selected_pdb_path
                sel_residues = st.session_state.heatmap_selected_residues
                rep = cluster_to_rep.get(sel_id)
                st.markdown(f"**Cluster {sel_id}** — Representative Structure")
                if rep is not None:
                    m1, m2 = st.columns(2)
                    m1.metric("Probability", f"{rep.get('probability', 0):.3f}")
                    m2.metric("Residues", len(sel_residues))
                is_selected = sel_id in st.session_state.heatmap_docking_clusters
                if st.checkbox("Select for Docking", value=is_selected, key=f"pipe_dock_sel_{sel_id}"):
                    if sel_id not in st.session_state.heatmap_docking_clusters:
                        st.session_state.heatmap_docking_clusters.append(sel_id)
                else:
                    if sel_id in st.session_state.heatmap_docking_clusters:
                        st.session_state.heatmap_docking_clusters.remove(sel_id)
                if sel_path and os.path.exists(sel_path):
                    _show_molecule_3d_with_pocket(sel_path, sel_residues)
                else:
                    st.warning(f"PDB not found: `{sel_path}`")
            else:
                st.info("← Check a cluster to view its 3D structure here")

        # ── Inline Docking Section ──────────────────────────────────────────
        st.markdown("---")
        st.markdown("### 🧪 Molecular Docking")

        pipe_task_id = st.session_state.get('pipe_docking_task_id')
        pipe_job_id = st.session_state.get('pipe_docking_job_id')

        if pipe_task_id:
            dock_task = celery_app.AsyncResult(pipe_task_id)

            if dock_task.state == 'PENDING':
                st.info("⏳ Docking queued…")
                st.progress(0)
                time.sleep(3)
                st.rerun()

            elif dock_task.state == 'PROGRESS':
                info = dock_task.info or {}
                progress = info.get('progress', 0)
                current_step = info.get('current_step', 'Processing…')
                pairs_done = info.get('pairs_done', 0)
                pairs_total = info.get('pairs_total', 0)
                st.progress(progress / 100, text=f"{progress}% — {current_step}")
                if pairs_total > 0:
                    st.progress(pairs_done / pairs_total,
                                text=f"{pairs_done}/{pairs_total} receptor–ligand pairs")
                if progress <= 15:
                    st.caption("⏱️ Preparing receptors (converting PDB → PDBQT)…")
                time.sleep(3)
                st.rerun()

            elif dock_task.state == 'SUCCESS':
                results = dock_task.result or {}
                st.success("✅ Docking completed!")

                best_aff = results.get('best_affinity', 0)
                _, aff_emoji = _classify_affinity(best_aff)
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Total Poses", results.get('total_docking_poses', 0))
                col2.metric("Unique Ligands", results.get('unique_ligands', 0))
                col3.metric("Unique Receptors", results.get('unique_receptors', 0))
                col4.metric(f"Best Affinity {aff_emoji}", f"{best_aff:.2f} kcal/mol")

                results_file = results.get('docking_results_file')
                if results_file and os.path.exists(results_file):
                    df_dock = pd.read_csv(results_file)
                    if not df_dock.empty and 'ligand' in df_dock.columns and 'receptor' in df_dock.columns:
                        df_best_dock = df_dock.loc[
                            df_dock.groupby(['ligand', 'receptor'])['affinity (kcal/mol)'].idxmin()
                        ].sort_values('affinity (kcal/mol)')
                        df_best_dock['Quality'] = df_best_dock['affinity (kcal/mol)'].apply(
                            lambda x: f"{_classify_affinity(x)[1]} {_classify_affinity(x)[0]}"
                        )

                        # ── Split view: table + 3D viewer ──────────────────
                        st.markdown("#### 🎯 Results Explorer with 3D Visualization")

                        filt_col1, filt_col2 = st.columns([2, 1])
                        with filt_col1:
                            affinity_filter = st.multiselect(
                                "Filter by Affinity:",
                                options=['excellent', 'good', 'moderate', 'poor'],
                                default=['excellent', 'good'],
                                key="pipe_affinity_filter",
                            )
                        with filt_col2:
                            top_n = st.slider("Show top N:", 5, 50, 15, key="pipe_top_n")

                        df_filtered = df_best_dock[
                            df_best_dock['affinity (kcal/mol)'].apply(
                                lambda x: _classify_affinity(x)[0]
                            ).isin(affinity_filter)
                        ] if affinity_filter else df_best_dock
                        df_display = df_filtered.head(top_n)

                        table_col, viewer_col = st.columns([1, 1])

                        with table_col:
                            st.markdown("##### 🏆 Top Poses")
                            if not df_display.empty:
                                selected_idx = st.selectbox(
                                    "Select pose to view:",
                                    df_display.index.tolist(),
                                    format_func=lambda x: (
                                        f"{df_display.loc[x, 'Quality'].split()[0]} "
                                        f"{df_display.loc[x, 'ligand']} ↔ "
                                        f"{df_display.loc[x, 'receptor']} "
                                        f"({df_display.loc[x, 'affinity (kcal/mol)']:.2f} kcal/mol)"
                                    ),
                                    key="pipe_pose_selector",
                                )
                                if selected_idx is not None:
                                    st.session_state.pipe_selected_pose = df_display.loc[selected_idx].to_dict()
                                st.dataframe(
                                    df_display[['ligand', 'receptor', 'affinity (kcal/mol)',
                                                'Quality', 'rmsd l.b.', 'rmsd u.b.']].rename(columns={
                                        'affinity (kcal/mol)': 'Affinity', 'Quality': '🎯',
                                        'rmsd l.b.': 'RMSD LB', 'rmsd u.b.': 'RMSD UB',
                                    }),
                                    use_container_width=True, height=300,
                                )
                            else:
                                st.info("No poses match the selected filters.")

                        with viewer_col:
                            st.markdown("##### 🔬 3D Structure")
                            pose = st.session_state.get('pipe_selected_pose')
                            if pose:
                                cat, emoji = _classify_affinity(pose.get('affinity (kcal/mol)', 0))
                                st.markdown(
                                    f'<div style="background:linear-gradient(135deg,#667eea,#764ba2);'
                                    f'padding:0.7rem 1rem;border-radius:8px;color:white;margin-bottom:0.5rem;">'
                                    f'<strong>{emoji} {pose.get("ligand","?")} ↔ {pose.get("receptor","?")}</strong>'
                                    f'<br><span style="font-size:1.1rem;font-weight:bold;">'
                                    f'{pose.get("affinity (kcal/mol)",0):.2f} kcal/mol</span>'
                                    f'</div>',
                                    unsafe_allow_html=True,
                                )
                                viz_style = st.selectbox(
                                    "Style:", ["cartoon", "surface", "stick", "binding site"],
                                    key="pipe_viz_style",
                                )
                                show_ligand = st.checkbox("Show Ligand", value=True, key="pipe_show_lig")

                                ligand_sdf_data = None
                                if show_ligand:
                                    sdf_path = pose.get('output_sdf')
                                    mode = pose.get('mode')
                                    if sdf_path and mode is not None:
                                        ligand_sdf_data = _extract_sdf_model(sdf_path, mode)

                                receptor_data = None
                                for path_key in ('receptor_pdb_path', 'receptor_path'):
                                    rpath = pose.get(path_key)
                                    if rpath and os.path.exists(rpath):
                                        with open(rpath, 'r') as f:
                                            receptor_data = f.read()
                                        break
                                if receptor_data is None:
                                    docking_dir = results.get('docking_output_dir')
                                    if docking_dir:
                                        rec_name = pose.get('receptor', '')
                                        for candidate in [
                                            os.path.join(docking_dir, rec_name),
                                            os.path.join(docking_dir, rec_name + '.pdb'),
                                            os.path.join(docking_dir, rec_name + '.pdbqt'),
                                        ]:
                                            if os.path.exists(candidate):
                                                with open(candidate, 'r') as f:
                                                    receptor_data = f.read()
                                                break

                                if receptor_data:
                                    _show_docking_molecule_3d(
                                        receptor_data, ligand_sdf_data,
                                        style_protein=viz_style,
                                    )
                                else:
                                    st.warning("⚠️ Receptor structure file not found.")
                            else:
                                st.info("← Select a pose from the table to view it here.")

                        # Bar chart + download below
                        fig_bar = px.bar(
                            df_best_dock.head(20),
                            x='ligand', y='affinity (kcal/mol)', color='receptor',
                            title='Best Affinity per Ligand (lower = stronger binding)',
                            labels={'affinity (kcal/mol)': 'Affinity (kcal/mol)'},
                        )
                        fig_bar.update_layout(height=300)
                        st.plotly_chart(fig_bar, use_container_width=True, key="pipe_dock_bar")

                        st.download_button(
                            "📥 Download Full Results (CSV)",
                            data=df_dock.to_csv(index=False),
                            file_name=f"docking_results_{pipe_job_id}.csv",
                            mime="text/csv",
                        )
                    else:
                        st.warning("⚠️ Results file is empty or missing required columns.")
                else:
                    st.warning("No results file found.")

                if st.button("🔄 Re-run Docking with Different Ligands", key="pipe_redock"):
                    st.session_state.pipe_docking_task_id = None
                    st.session_state.pipe_docking_job_id = None
                    st.rerun()

            elif dock_task.state == 'FAILURE':
                st.error(f"❌ Docking failed: {dock_task.info}")
                if st.button("🔄 Retry Docking", key="pipe_retry_docking"):
                    st.session_state.pipe_docking_task_id = None
                    st.session_state.pipe_docking_job_id = None
                    st.rerun()

        else:
            # ── Docking input form (shown when no task is running) ──────────
            selected_clusters = st.session_state.heatmap_docking_clusters
            if selected_clusters:
                selected_str = ", ".join(str(c) for c in selected_clusters)
                st.info(f"Clusters selected for docking: **{selected_str}**")

                st.markdown("#### 📁 Ligand Files")
                ligand_files = st.file_uploader(
                    f"Upload ligand files (PDBQT or ZIP, max {Config.MAX_DOCKING_LIGANDS})",
                    type=['pdbqt', 'zip'],
                    accept_multiple_files=True,
                    key="pipe_dock_ligands",
                )

                st.markdown("#### ⚙️ Docking Parameters")
                dc1, dc2, dc3, dc4 = st.columns(4)
                with dc1:
                    d_poses = st.slider("Poses per Ligand", 1, 20, 10, key="pipe_dock_poses")
                with dc2:
                    d_exhaust = st.slider(
                        "Exhaustiveness", 1, Config.MAX_DOCKING_EXHAUSTIVENESS,
                        min(8, Config.MAX_DOCKING_EXHAUSTIVENESS), key="pipe_dock_exhaust",
                    )
                with dc3:
                    d_ph = st.slider("pH", 4.0, 10.0, 7.4, step=0.1, key="pipe_dock_ph")
                with dc4:
                    d_box = st.slider("Box Size (Å)", 10.0, 50.0, 20.0, step=1.0, key="pipe_dock_box")

                if st.button("🚀 Start Docking with Selected Clusters", type="primary",
                             use_container_width=True, key="pipe_start_docking"):
                    if not ligand_files:
                        st.error("❌ Please upload at least one ligand file.")
                    else:
                        dock_job_id = f"dock_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
                        ligand_dir = os.path.join(UPLOAD_DIR, dock_job_id, 'ligands')
                        os.makedirs(ligand_dir, exist_ok=True)
                        collected = []
                        for lf in ligand_files:
                            lf_path = os.path.join(ligand_dir, lf.name)
                            with open(lf_path, 'wb') as f:
                                f.write(lf.getbuffer())
                            if lf.name.endswith('.zip'):
                                with zipfile.ZipFile(lf_path, 'r') as zfh:
                                    zfh.extractall(ligand_dir)
                                    for fname in zfh.namelist():
                                        if fname.endswith('.pdbqt'):
                                            collected.append(os.path.join(ligand_dir, fname))
                            elif lf.name.endswith('.pdbqt'):
                                collected.append(lf_path)

                        if len(collected) > Config.MAX_DOCKING_LIGANDS:
                            st.warning(f"⚠️ Capped to {Config.MAX_DOCKING_LIGANDS} ligands.")
                            for excess in collected[Config.MAX_DOCKING_LIGANDS:]:
                                try:
                                    os.remove(excess)
                                except OSError:
                                    pass
                            collected = collected[:Config.MAX_DOCKING_LIGANDS]

                        # Build filtered reps CSV for selected clusters only
                        if 'cluster' in df_reps.columns:
                            filtered_reps = df_reps[
                                df_reps['cluster'].isin([int(c) for c in selected_clusters])
                            ]
                        else:
                            filtered_reps = df_reps
                        dock_upload_dir = os.path.join(UPLOAD_DIR, dock_job_id)
                        os.makedirs(dock_upload_dir, exist_ok=True)
                        filtered_reps_file = os.path.join(dock_upload_dir, 'filtered_reps.csv')
                        filtered_reps.to_csv(filtered_reps_file, index=False)

                        pdb_source_dir = os.path.join(RESULTS_DIR, results_job_id, 'pdbs')

                        dock_task_obj = run_docking_task.delay(
                            cluster_representatives_csv=filtered_reps_file,
                            ligand_folder=ligand_dir,
                            job_id=dock_job_id,
                            smina_exe_path=Config.SMINA_PATH,
                            num_poses=d_poses,
                            exhaustiveness=d_exhaust,
                            ph_value=d_ph,
                            box_size_x=d_box,
                            box_size_y=d_box,
                            box_size_z=d_box,
                            pdb_source_dir=pdb_source_dir,
                        )
                        st.session_state.pipe_docking_task_id = dock_task_obj.id
                        st.session_state.pipe_docking_job_id = dock_job_id
                        st.rerun()
            else:
                st.info("← Select clusters in the heatmap above to configure and launch docking here.")

    except Exception as e:
        st.error(f"Error loading clustering results: {e}")
        logger.error(f"Pipeline inline cluster results error: {e}", exc_info=True)


# Session state
initialize_session_state()

if 'pipeline_job_id' not in st.session_state:
    st.session_state.pipeline_job_id = None
if 'pipeline_task_id' not in st.session_state:
    st.session_state.pipeline_task_id = None
if 'pipeline_status' not in st.session_state:
    st.session_state.pipeline_status = 'idle'


# Real progress boundaries matching _run_stage calls in tasks.py:
#   0-25   extract_to_pdb
#   25-60  detect_pockets
#   60-80  cluster_pockets
#   80-97  docking (optional)
_STAGES = [
    ('Extract Frames',    0,  25, 'file-earmark-arrow-down'),
    ('Detect Pockets',   25,  60, 'search'),
    ('Cluster Pockets',  60,  80, 'diagram-3'),
    ('Dock (optional)',  80, 100, 'flask'),
]


def _stage_class(progress, start, end):
    if progress >= end:
        return 'stage-done'
    elif progress >= start:
        return 'stage-active'
    return 'stage-pending'


def show_stage_indicators(progress, current_step=''):
    boxes = "".join(
        f'<div class="stage-box {_stage_class(progress, s, e)}">{name}</div>'
        for name, s, e, _ in _STAGES
    )
    st.markdown(f'<div class="stage-indicator">{boxes}</div>', unsafe_allow_html=True)
    if current_step:
        st.caption(f"▶ {current_step}")


# ── Status banner if a task is running ─────────────────────────────────
if st.session_state.pipeline_task_id:
    try:
        _task = celery_app.AsyncResult(st.session_state.pipeline_task_id)
        if _task.state == 'PENDING':
            show_stage_indicators(0, 'Waiting in queue…')
            st.progress(0, text="Pending…")
            st.info("⏳ Pipeline task is pending in the Celery queue.")
            time.sleep(4)
            st.rerun()
        elif _task.state == 'PROGRESS':
            _info = _task.info or {}
            _prog = _info.get('progress', 0)
            _step = _info.get('current_step', 'Processing…')
            _stage = _info.get('stage', '')

            show_stage_indicators(_prog, _step)
            st.progress(_prog / 100, text=f"{_prog}%")

            # Contextual metrics depending on stage
            if _stage in ('detect', 'cluster', 'cluster_done', 'docking'):
                _frames = _info.get('frames_extracted')
                if _frames is not None:
                    st.caption(f"📁 {_frames} frames extracted")
            if _stage in ('cluster', 'cluster_done', 'docking'):
                _pockets = _info.get('pockets_detected')
                if _pockets is not None:
                    st.caption(f"🔍 {_pockets} pockets detected")
            if _stage == 'docking':
                _pairs_done = _info.get('pairs_done', 0)
                _pairs_total = _info.get('pairs_total', 0)
                if _pairs_total > 0:
                    st.progress(_pairs_done / _pairs_total,
                                text=f"Docking: {_pairs_done}/{_pairs_total} pairs")

            time.sleep(3)
            st.rerun()
        elif _task.state == 'SUCCESS':
            result = _task.result or {}
            show_stage_indicators(100, 'All stages complete')
            st.success("✅ Full pipeline completed successfully!")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Frames extracted", result.get('frames_extracted', '—'))
            col2.metric("Pockets detected", result.get('pockets_detected', '—'))
            col3.metric("Cluster representatives", result.get('representatives', '—'))
            col4.metric("Docking poses", result.get('docking_poses', '—') or '—')
            if result.get('docking_error'):
                st.warning(f"⚠️ Docking encountered an error: {result['docking_error']}")

            cluster_job = result.get('cluster_job_id', st.session_state.pipeline_job_id)
            if cluster_job:
                _show_pipeline_cluster_inline(cluster_job)
        elif _task.state == 'FAILURE':
            show_stage_indicators(0, 'Pipeline failed')
            st.error("❌ Pipeline failed.")
            _info = _task.info or {}
            st.error(f"Error: {_info.get('exc_message', str(_task.info))}")
    except Exception as e:
        st.warning(f"Could not retrieve task status: {e}")

    if st.button("🔄 Reset / Start New Pipeline"):
        st.session_state.pipeline_task_id = None
        st.session_state.pipeline_job_id = None
        st.session_state.pipeline_status = 'idle'
        st.session_state.pipe_docking_task_id = None
        st.session_state.pipe_docking_job_id = None
        st.session_state.heatmap_docking_clusters = []
        st.session_state.heatmap_selected_cluster_id = None
        st.rerun()

    st.stop()


# ── Input form ──────────────────────────────────────────────────────────
st.markdown("### 📁 Input Files")

with st.form("pipeline_form"):
    col_traj, col_topo = st.columns(2)

    with col_traj:
        traj_file = st.file_uploader(
            "Trajectory (.xtc)",
            type=['xtc'],
            help="GROMACS XTC trajectory file"
        )
    with col_topo:
        topo_file = st.file_uploader(
            "Topology (.pdb or .gro)",
            type=['pdb', 'gro'],
            help="Topology/structure file matching the trajectory"
        )

    st.markdown("### ⚙️ Processing Parameters")

    col_stride, col_threads = st.columns(2)
    with col_stride:
        stride = st.slider("Frame Stride", min_value=1, max_value=100, value=10,
                           help="Extract every Nth frame from trajectory")
    with col_threads:
        num_threads = st.slider("CPU Threads", min_value=1, max_value=16, value=4,
                                help="Number of parallel threads for pocket detection")

    col_prob, col_method = st.columns(2)
    with col_prob:
        min_prob = st.slider("Min Pocket Probability", min_value=0.0, max_value=1.0, value=0.5, step=0.05,
                             help="Minimum p2rank probability threshold")
    with col_method:
        clustering_method = st.selectbox("Clustering Method", options=['dbscan', 'kmeans', 'hierarchical'],
                                         help="Algorithm for grouping similar pockets")

    st.markdown("### 🔬 Optional: Molecular Docking")
    include_docking = st.checkbox("Run docking after clustering", value=False)
    if include_docking:
        st.info("ℹ️ Docking will run automatically on **all** cluster representatives. To hand-pick specific clusters first, leave this unchecked — then use the heatmap in Step 3 to select clusters before docking.")

    ligand_files_uploaded = None
    num_poses = 10
    exhaustiveness = 8
    ph_value = 7.4
    box_size = 20.0

    if include_docking:
        st.info(f"Ligand limit: {Config.MAX_DOCKING_LIGANDS} files. Exhaustiveness cap: {Config.MAX_DOCKING_EXHAUSTIVENESS}.")
        ligand_files_uploaded = st.file_uploader(
            "Ligand Files (PDBQT or ZIP)",
            type=['pdbqt', 'zip'],
            accept_multiple_files=True,
            help="Upload PDBQT ligands or a ZIP archive of PDBQT files"
        )
        dock_col1, dock_col2, dock_col3, dock_col4 = st.columns(4)
        with dock_col1:
            num_poses = st.slider("Poses per Ligand", 1, 20, 10)
        with dock_col2:
            exhaustiveness = st.slider("Exhaustiveness", 1, Config.MAX_DOCKING_EXHAUSTIVENESS,
                                       min(8, Config.MAX_DOCKING_EXHAUSTIVENESS))
        with dock_col3:
            ph_value = st.slider("pH", 4.0, 10.0, 7.4, step=0.1)
        with dock_col4:
            box_size = st.slider("Box Size (Å)", 10.0, 50.0, 20.0, step=1.0)

    submitted = st.form_submit_button("🚀 Launch Full Pipeline", type="primary", use_container_width=True)


if submitted:
    if not traj_file:
        st.error("❌ Please upload a trajectory (.xtc) file.")
        st.stop()
    if not topo_file:
        st.error("❌ Please upload a topology (.pdb or .gro) file.")
        st.stop()
    if include_docking and not ligand_files_uploaded:
        st.error("❌ Please upload at least one ligand file to enable docking.")
        st.stop()

    # Rate limit check
    try:
        check_task_rate_limit()
    except RateLimitExceeded as e:
        st.error(f"⏳ Task rate limit exceeded: {e}")
        st.stop()

    job_id = f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
    job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_upload_dir, exist_ok=True)

    # Save trajectory
    traj_path = os.path.join(job_upload_dir, traj_file.name)
    with open(traj_path, 'wb') as f:
        f.write(traj_file.getbuffer())

    # Save topology
    topo_path = os.path.join(job_upload_dir, topo_file.name)
    with open(topo_path, 'wb') as f:
        f.write(topo_file.getbuffer())

    # Save ligands if provided
    ligand_folder = None
    if include_docking and ligand_files_uploaded:
        ligand_dir = os.path.join(job_upload_dir, 'ligands')
        os.makedirs(ligand_dir, exist_ok=True)
        collected = []
        for lf in ligand_files_uploaded:
            lf_path = os.path.join(ligand_dir, lf.name)
            with open(lf_path, 'wb') as f:
                f.write(lf.getbuffer())
            if lf.name.endswith('.zip'):
                with zipfile.ZipFile(lf_path, 'r') as zf:
                    zf.extractall(ligand_dir)
                    for fname in zf.namelist():
                        if fname.endswith('.pdbqt'):
                            collected.append(os.path.join(ligand_dir, fname))
            elif lf.name.endswith('.pdbqt'):
                collected.append(lf_path)
        if len(collected) > Config.MAX_DOCKING_LIGANDS:
            st.warning(f"⚠️ Ligand count capped from {len(collected)} to {Config.MAX_DOCKING_LIGANDS}.")
            for excess in collected[Config.MAX_DOCKING_LIGANDS:]:
                try:
                    os.remove(excess)
                except OSError:
                    pass
            collected = collected[:Config.MAX_DOCKING_LIGANDS]
        ligand_folder = ligand_dir

    # Submit Celery task
    task = run_pockethunter_pipeline.delay(
        xtc_file_path=traj_path,
        topology_file_path=topo_path,
        job_id=job_id,
        stride=stride,
        num_threads=num_threads,
        min_prob=min_prob,
        clustering_method=clustering_method,
        run_docking=include_docking,
        ligand_folder=ligand_folder,
        num_poses=num_poses,
        exhaustiveness=exhaustiveness,
        ph_value=ph_value,
        box_size_x=box_size,
        box_size_y=box_size,
        box_size_z=box_size,
    )

    st.session_state.pipeline_job_id = job_id
    st.session_state.pipeline_task_id = task.id
    st.session_state.pipeline_status = 'running'
    st.session_state.cached_job_ids['pipeline'] = job_id

    st.success(f"✅ Pipeline started! Job ID: `{job_id}`")
    time.sleep(1)
    st.rerun()
