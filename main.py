import streamlit as st
from streamlit_option_menu import option_menu
import extra_streamlit_components as stx
import os
from streamlit_extras.app_logo import add_logo
import pandas as pd
import plotly.graph_objs as go
import plotly.express as px
from datetime import datetime
import time
import json
import zipfile
import shutil
import uuid
import sys
from pathlib import Path
from tasks import run_pockethunter_pipeline, run_find_pockets_task, run_cluster_pockets_task, run_docking_task
from celery_app import celery_app

# Page configuration
st.set_page_config(
    page_title="PocketHunter Suite",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        'Get Help': 'https://github.com/your-repo/pockethunter',
        'Report a bug': "https://github.com/your-repo/pockethunter/issues",
        'About': "# PocketHunter Suite\nA modern molecular dynamics pocket detection and analysis tool."
    }
)

# Brutalist scientific aesthetic — bespoke header + option_menu overrides only.
# Global theming (fonts, colors, borders, radii) lives in .streamlit/config.toml.
st.markdown("""
<style>
    .bh {
        border: 2px solid #000;
        padding: 18px 22px;
        margin: 0 0 28px 0;
        background: #fff;
        font-family: 'JetBrains Mono', ui-monospace, monospace;
    }
    .bh-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 16px;
    }
    .bh-title {
        font-size: 1.55rem;
        font-weight: 800;
        letter-spacing: -0.01em;
        text-transform: uppercase;
        color: #000;
    }
    .bh-version {
        font-size: 0.72rem;
        font-weight: 600;
        color: #000;
        background: #d4ff00;
        padding: 3px 10px;
        border: 2px solid #000;
        white-space: nowrap;
    }
    .bh-rule { border-top: 1px solid #000; margin: 10px 0 10px 0; }
    .bh-bar {
        height: 10px;
        margin: 8px 0 14px 0;
        background-image: repeating-linear-gradient(
            90deg,
            #d4ff00 0 14px,
            #000 14px 16px
        );
    }
    .bh-stages {
        font-size: 0.78rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        color: #000;
        text-transform: uppercase;
    }
    .bh-stages span.sep { color: #d4ff00; padding: 0 6px; }

    /* option_menu navigation — brutalist override (theme.toml can't reach it) */
    [class*="nav-link"] {
        color: #000 !important;
        background-color: #fff !important;
        border: 2px solid #000 !important;
        border-radius: 0 !important;
        font-weight: 600 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.04em !important;
        transition: none !important;
    }
    [class*="nav-link"]:hover {
        background-color: #d4ff00 !important;
        color: #000 !important;
    }
    [class*="nav-link-selected"] {
        background-color: #000 !important;
        color: #fff !important;
        border: 2px solid #000 !important;
    }
    [class*="nav-link"] svg { color: inherit !important; opacity: 1; }
</style>
""", unsafe_allow_html=True)

# ── v2 Phase A: session routing ──────────────────────────────────────────
#
# At the top of every rerun, resolve the query string against the DB. The
# resolver is cheap (one indexed lookup) and the result drives:
#   • no short_code  →  render landing page, st.stop()
#   • short_code missing in DB  →  "session not found" page, st.stop()
#   • short_code present, expired_at set  →  "session expired" page, st.stop()
#   • short_code valid  →  set st.session_state.{current_session, is_editor,
#     is_session_expired} and continue into the existing app.
#
# Existing pages keep working unchanged in this phase: they don't yet read
# current_session. A3 wires the task layer to write to the DB; Phase B
# rewires pages to read from the session. For now, sessions are required
# but otherwise opportunistic.

from session_routes import resolve_session_from_query, set_session_in_state  # noqa: E402
from landing import (  # noqa: E402
    render_landing,
    render_session_chip,
    render_session_expired,
    render_session_not_found,
)

_resolved = resolve_session_from_query()
set_session_in_state(_resolved)

if _resolved.session is None and _resolved.short_code is None:
    # No ?s= in the URL — show the landing page and stop.
    render_landing()
    st.stop()

if _resolved.session is None:
    # ?s=<bogus> — short_code provided but didn't resolve.
    render_session_not_found(_resolved.short_code)
    st.stop()

if _resolved.is_expired:
    # Soft-expired session — DB row exists, volumes are gone.
    render_session_expired(_resolved)
    st.stop()

# A session is loaded. Touch last_active_at (best-effort — don't crash on it).
try:
    from db.sessions import touch_last_active
    touch_last_active(_resolved.session)
except Exception:
    pass

st.markdown("""
<div class="bh">
    <div class="bh-row">
        <span class="bh-title">PocketHunter/Suite</span>
        <span class="bh-version">[v.2.0]</span>
    </div>
    <div class="bh-rule"></div>
    <div class="bh-bar"></div>
    <div class="bh-stages">MD<span class="sep">►</span>POCKETS<span class="sep">►</span>CLUSTERS<span class="sep">►</span>DOCK</div>
</div>
""", unsafe_allow_html=True)

# Compact share-URL chip + Editor/Viewer indicator.
render_session_chip(_resolved)

# Initialize session state for job ID caching
if 'cached_job_ids' not in st.session_state:
    st.session_state.cached_job_ids = {
        'extract': None,
        'detect': None,
        'cluster': None,
        'docking': None,
        'pipeline': None,
    }

# Define the pages
pages = {
    "Full Pipeline": "pipeline_app.py",
    "Step 1: Find Pockets": "find_pockets_app.py",
    "Step 2: Cluster Pockets": "cluster_pockets_app.py",
    "Step 3: Molecular Docking": "docking_app.py",
    "Task Monitor": "task_monitor_app.py"
}

# Resolve pending navigation. option_menu uses its own JS state for the active tab;
# the correct way to switch programmatically is via manual_select (integer index).
_page_names = list(pages.keys())
_manual_select = None
if 'pending_nav' in st.session_state and st.session_state.pending_nav in _page_names:
    _manual_select = _page_names.index(st.session_state.pending_nav)
    del st.session_state.pending_nav

# Horizontal menu - styles handled by CSS for theme compatibility
selected = option_menu(
    None,
    _page_names,
    icons=['lightning-charge', 'search', 'diagram-3', 'flask', 'activity'],
    menu_icon="cast",
    default_index=0,
    manual_select=_manual_select,
    orientation="horizontal",
    key="main_menu",  # Add unique key to prevent caching issues
    styles={
        "container": {"padding": "0!important", "background-color": "transparent"},
        "icon": {"font-size": "18px"},
        "nav-link": {"font-size": "16px", "text-align": "left", "margin":"0px"},
        "nav-link-selected": {},
    }
)

# Route to the selected page using runpy for proper namespace isolation
import runpy

# Map page names to file paths
PAGE_FILES = pages  # Use the same dict

# Execute the selected page with isolated namespace
if selected in PAGE_FILES:
    page_file = PAGE_FILES[selected]
    page_path = Path(__file__).parent / page_file

    try:
        # Use runpy.run_path with a fresh namespace that includes streamlit
        # This prevents namespace pollution between page switches
        page_globals = {
            '__name__': '__main__',
            '__file__': str(page_path),
            'st': st,  # Pass streamlit module
        }

        # Run the page file with isolated namespace
        runpy.run_path(str(page_path), init_globals=page_globals, run_name='__main__')

    except Exception as e:
        st.error(f"❌ Error loading page '{selected}'")
        st.info(
            "Try selecting a different page from the menu above, or reload the "
            "browser tab. If this keeps happening, copy the technical details "
            "below into a bug report."
        )

        import traceback
        with st.expander("🐛 Technical details (for bug reports)"):
            st.code(traceback.format_exc())
else:
    st.error(f"Unknown page: {selected}") 