import streamlit as st
import runpy
from pathlib import Path

st.set_page_config(
    page_title="PocketHunter Suite",
    page_icon="assets/favicon.ico" if Path("assets/favicon.ico").exists() else None,
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Global CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');

html, body, [data-testid="stApp"] {
    font-family: 'Roboto', sans-serif !important;
    background: #F5F6F8 !important;
}
[data-testid="stHeader"]          { display: none !important; }
#MainMenu, footer, .stDeployButton { display: none !important; }
.block-container {
    padding-top: 0.5rem !important;
    padding-bottom: 2rem !important;
    max-width: 900px;
}

/* ── Panels ── */
.ph-panel {
    background: rgba(255,255,255,0.92);
    border: 2px solid #B0BDD0;
    border-radius: 12px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.06);
    margin-bottom: 12px;
    overflow: hidden;
}
.ph-panel-active  { border-color: #6B7FA8; box-shadow: 0 4px 20px rgba(107,127,168,.18); }
.ph-panel-done    { border-color: #c0cad8; }
.ph-panel-locked  { border-color: #e8e8e8; opacity: .55; }
.ph-panel-header  { padding: 13px 18px; display: flex; align-items: center; gap: 10px; }
.ph-panel-title   { font-size: 13px; font-weight: 500; color: #2a3a4a; flex: 1; margin: 0; }
.ph-panel-body    { padding: 0 18px 18px; }

/* ── Step strip ── */
.ph-step-strip   { display: flex; align-items: flex-start; margin-bottom: 22px; padding: 0 4px; }
.ph-step-node    { display: flex; flex-direction: column; align-items: center; min-width: 64px; }
.ph-step-circle  {
    width: 28px; height: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700;
    border: 2px solid #ddd; background: white; color: #bbb;
}
.ph-step-done    { background: #6B7FA8 !important; border-color: #6B7FA8 !important; color: white !important; }
.ph-step-active  {
    background: white !important; border-color: #6B7FA8 !important; color: #6B7FA8 !important;
    box-shadow: 0 0 0 3px rgba(107,127,168,.18);
}
.ph-step-locked  { background: #f5f5f5 !important; border-color: #ddd !important; color: #ccc !important; }
.ph-step-label   { font-size: 10px; color: #999; margin-top: 5px; text-align: center; line-height: 1.3; max-width: 72px; }
.ph-label-done   { color: #6B7FA8; font-weight: 500; }
.ph-label-active { color: #5a6e97; font-weight: 500; }
.ph-step-line    { flex: 1; height: 2px; margin-top: 13px; border-radius: 1px; background: #e0e0e0; }
.ph-line-done    { background: #6B7FA8; }

/* ── Badges ── */
.ph-badge        { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px; border: 1px solid; display: inline-block; text-transform: uppercase; letter-spacing: .04em; }
.ph-badge-active { background: rgba(45,116,218,.08); color: #2d74da; border-color: rgba(45,116,218,.25); }
.ph-badge-done   { background: rgba(0,168,133,.08);  color: #00a085; border-color: rgba(0,168,133,.25); }
.ph-badge-locked { background: #f5f5f5; color: #bbb; border-color: #e0e0e0; }
.ph-badge-ready  { background: rgba(107,127,168,.1); color: #5a6e97; border-color: rgba(107,127,168,.3); }

/* ── Job ID banner ── */
.ph-job-banner   {
    background: white; border: 1px solid #B0BDD0; border-left: 3px solid #6B7FA8;
    border-radius: 8px; padding: 12px 16px; margin-bottom: 18px;
}
.ph-job-label    { font-size: 10px; font-weight: 700; color: #9aa0b8; text-transform: uppercase; letter-spacing: .06em; }
.ph-job-value    { font-size: 13px; font-family: monospace; color: #2a3a4a; font-weight: 700; margin-top: 2px; }
.ph-job-warn     { font-size: 11px; color: #c0863a; margin-top: 4px; }

/* ── Progress ── */
.ph-prog-outer   { background: #e8ebf0; border-radius: 4px; height: 6px; overflow: hidden; margin: 8px 0; }
.ph-prog-inner   { height: 100%; border-radius: 4px; background: linear-gradient(90deg, #6B7FA8, #8B9FC8); }
.ph-prog-meta    { display: flex; justify-content: space-between; align-items: center; }
.ph-prog-text    { font-size: 12px; color: #888; }
.ph-prog-pct     { font-size: 12px; font-weight: 700; color: #6B7FA8; }

/* ── Stage chips ── */
.ph-chip         { font-size: 11px; padding: 2px 10px; border-radius: 12px; border: 1px solid; display: inline-block; margin: 8px 4px 0 0; }
.ph-chip-done    { background: rgba(0,168,133,.08);  color: #00a085; border-color: rgba(0,168,133,.25); }
.ph-chip-active  { background: rgba(45,116,218,.08); color: #2d74da; border-color: rgba(45,116,218,.25); }
.ph-chip-wait    { background: #f5f5f5; color: #bbb; border-color: #e0e0e0; }

/* ── Log box ── */
.ph-log          { background: #f5f6f8; border: 1px solid #dde2ec; border-radius: 6px; padding: 10px 14px; font-family: monospace; font-size: 11px; color: #777; line-height: 1.9; margin-top: 12px; }
.ph-log-label    { font-size: 10px; color: #aaa; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 4px; }

/* ── Metrics ── */
.ph-metric       { background: #F5F6F8; border-radius: 8px; padding: 10px 14px; border: 1px solid #dde2ec; text-align: center; }
.ph-metric-label { font-size: 10px; color: #999; text-transform: uppercase; letter-spacing: .06em; font-weight: 700; }
.ph-metric-value { font-size: 22px; font-weight: 700; color: #5a6e97; margin-top: 2px; }

/* ── Resume bar ── */
.ph-resume       { background: white; border: 1px solid #B0BDD0; border-radius: 10px; padding: 12px 16px; margin-bottom: 20px; }
.ph-resume-label { font-size: 11px; font-weight: 700; color: #9aa0b8; text-transform: uppercase; letter-spacing: .06em; }
.ph-resume-desc  { font-size: 13px; color: #666; margin-top: 2px; }

/* ── Success banner ── */
.ph-success {
    background: rgba(0,168,133,.08); border: 1px solid rgba(0,168,133,.25);
    border-left: 3px solid #00a085; border-radius: 8px; padding: 14px 18px; margin-bottom: 18px;
}

/* ── Nav button overrides ── */
[data-testid="stBaseButton-secondary"] {
    border: 1px solid #B0BDD0 !important;
    color: #6B7FA8 !important;
    background: white !important;
    font-size: 13px !important;
    font-weight: 500 !important;
    padding: 5px 14px !important;
    border-radius: 6px !important;
}
[data-testid="stBaseButton-primary"] {
    background: #f0f2f6 !important;
    border: 1px solid #B0BDD0 !important;
    color: #5a6e97 !important;
    font-size: 13px !important;
    font-weight: 600 !important;
    border-radius: 6px !important;
}
</style>
""", unsafe_allow_html=True)

# ── Session state ────────────────────────────────────────────────────────────
if 'active_page' not in st.session_state:
    st.session_state.active_page = 'wizard'

# ── Header ───────────────────────────────────────────────────────────────────
h_brand, h_gap, h_nav1, h_nav2 = st.columns([5, 2, 1, 1])

with h_brand:
    st.markdown("""
    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;">
        <div style="width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,#6B7FA8,#4a5e87);flex-shrink:0;"></div>
        <div>
            <span style="font-size:15px;font-weight:700;color:#2a3a4a;">PocketHunter Suite</span>
            <span style="font-size:10px;color:#9aa0b8;margin-left:6px;">MD Trajectory Pocket Analysis</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

with h_nav1:
    if st.button(
        "Analysis",
        key="nav_wizard",
        type="primary" if st.session_state.active_page == 'wizard' else "secondary",
        use_container_width=True,
    ):
        st.session_state.active_page = 'wizard'
        st.rerun()

with h_nav2:
    if st.button(
        "Task Monitor",
        key="nav_monitor",
        type="primary" if st.session_state.active_page == 'monitor' else "secondary",
        use_container_width=True,
    ):
        st.session_state.active_page = 'monitor'
        st.rerun()

st.markdown('<hr style="border:none;border-top:2px solid #B0BDD0;margin:0 0 20px;">', unsafe_allow_html=True)

# ── Routing ──────────────────────────────────────────────────────────────────
_here = Path(__file__).parent

_routes = {
    'wizard':  _here / 'wizard_app.py',
    'monitor': _here / 'task_monitor_app.py',
}

page_path = _routes.get(st.session_state.active_page, _routes['wizard'])

try:
    runpy.run_path(
        str(page_path),
        init_globals={'__name__': '__main__', '__file__': str(page_path), 'st': st},
        run_name='__main__',
    )
except Exception as e:
    st.error(f"Error loading page")
    import traceback
    with st.expander("Details"):
        st.code(traceback.format_exc())
