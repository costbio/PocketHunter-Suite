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
from tasks import run_pockethunter_pipeline, run_extract_to_pdb_task, run_detect_pockets_task, run_cluster_pockets_task, run_docking_task
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

# Custom CSS for theme-compatible design with molecular dynamics aesthetic
st.markdown("""
<style>
    /* Molecular dynamics color palette - adapts to theme */
    :root {
        --pocket-primary: #2E7D32;
        --pocket-secondary: #1565C0;
        --pocket-accent: #F57C00;
        --success-bg: rgba(46, 125, 50, 0.15);
        --error-bg: rgba(198, 40, 40, 0.15);
        --info-bg: rgba(21, 101, 192, 0.15);
        --border-opacity: 0.2;
    }

    /* Main header - molecular structure inspired */
    .main-header {
        background: linear-gradient(135deg,
            var(--pocket-primary) 0%,
            var(--pocket-secondary) 50%,
            var(--pocket-accent) 100%);
        padding: 1.5rem;
        border-radius: 12px;
        margin-bottom: 2rem;
        color: white;
        text-align: center;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        border: 1px solid rgba(255, 255, 255, 0.1);
    }

    /* Metric cards - theme adaptive with subtle molecular grid pattern */
    .metric-card {
        background: rgba(var(--secondary-background-color-rgb, 240, 242, 246), 0.5);
        backdrop-filter: blur(10px);
        padding: 1.5rem;
        border-radius: 12px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
        border-left: 4px solid var(--pocket-primary);
        border-top: 1px solid rgba(var(--text-color-rgb, 49, 51, 63), var(--border-opacity));
        margin: 1rem 0;
        transition: all 0.3s ease;
    }

    .metric-card:hover {
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        transform: translateY(-2px);
    }

    /* Status indicators - theme adaptive */
    .status-success {
        background: var(--success-bg);
        border: 1px solid rgba(46, 125, 50, 0.3);
        color: var(--text-color);
        padding: 1rem;
        border-radius: 10px;
        margin: 1rem 0;
        border-left: 4px solid #2E7D32;
    }

    .status-error {
        background: var(--error-bg);
        border: 1px solid rgba(198, 40, 40, 0.3);
        color: var(--text-color);
        padding: 1rem;
        border-radius: 10px;
        margin: 1rem 0;
        border-left: 4px solid #C62828;
    }

    .status-info {
        background: var(--info-bg);
        border: 1px solid rgba(21, 101, 192, 0.3);
        color: var(--text-color);
        padding: 1rem;
        border-radius: 10px;
        margin: 1rem 0;
        border-left: 4px solid #1565C0;
    }

    /* Upload area - molecular pocket visualization inspired */
    .upload-area {
        border: 2px dashed var(--pocket-primary);
        border-radius: 12px;
        padding: 2rem;
        text-align: center;
        background: rgba(var(--secondary-background-color-rgb, 240, 242, 246), 0.3);
        margin: 1rem 0;
        transition: all 0.3s ease;
    }

    .upload-area:hover {
        border-color: var(--pocket-accent);
        background: rgba(var(--secondary-background-color-rgb, 240, 242, 246), 0.5);
    }

    /* Job ID display - monospace with molecular theme */
    .job-id-display {
        background: linear-gradient(135deg,
            var(--pocket-primary) 0%,
            var(--pocket-secondary) 100%);
        color: white;
        padding: 0.75rem 1.25rem;
        border-radius: 8px;
        font-family: 'Monaco', 'Menlo', 'Courier New', monospace;
        font-size: 0.9rem;
        margin: 0.5rem 0;
        display: inline-block;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.2);
        border: 1px solid rgba(255, 255, 255, 0.1);
        letter-spacing: 0.5px;
    }

    /* Enhanced metrics for molecular data */
    .stMetric {
        background: rgba(var(--secondary-background-color-rgb, 240, 242, 246), 0.3);
        padding: 0.5rem;
        border-radius: 8px;
        border: 1px solid rgba(var(--text-color-rgb, 49, 51, 63), 0.1);
    }

    /* Navigation menu theme compatibility */
    nav[data-testid="stHorizontalBlock"] {
        background: transparent !important;
    }

    /* Option menu container - theme adaptive */
    [class*="nav-link"] {
        color: var(--text-color) !important;
        background-color: rgba(var(--secondary-background-color-rgb, 240, 242, 246), 0.3) !important;
        border: 1px solid rgba(var(--text-color-rgb, 49, 51, 63), 0.1) !important;
        transition: all 0.3s ease !important;
    }

    [class*="nav-link"]:hover {
        background-color: rgba(var(--pocket-primary), 0.1) !important;
        border-color: var(--pocket-primary) !important;
    }

    /* Selected nav link - visible in both themes */
    [class*="nav-link-selected"] {
        background: linear-gradient(135deg, var(--pocket-primary) 0%, var(--pocket-secondary) 100%) !important;
        color: white !important;
        border: 1px solid rgba(255, 255, 255, 0.2) !important;
    }

    /* Nav icons - theme adaptive */
    [class*="nav-link"] svg {
        color: var(--text-color) !important;
        opacity: 0.7;
    }

    [class*="nav-link-selected"] svg {
        color: white !important;
        opacity: 1;
    }

    /* Menu container background */
    .css-1544g2n, [data-testid="stVerticalBlock"] > div:first-child {
        background: transparent !important;
    }
</style>
""", unsafe_allow_html=True)

# Header with logo
st.markdown("""
<div class="main-header">
    <h1>🧬 PocketHunter Suite</h1>
    <p>Advanced Molecular Dynamics Pocket Detection & Analysis</p>
</div>
""", unsafe_allow_html=True)

# Initialize session state for job ID caching
if 'cached_job_ids' not in st.session_state:
    st.session_state.cached_job_ids = {
        'extract': None,
        'detect': None,
        'cluster': None
    }

# Clear old session state that might cause issues
def clear_old_session_state():
    """Clear old session state that might cause Celery errors"""
    old_keys = [
        'current_pipeline_job_id', 'pipeline_task_id', 'pipeline_done',
        'extract_task_id', 'detect_task_id', 'cluster_task_id'
    ]
    for key in old_keys:
        if key in st.session_state:
            del st.session_state[key]

# Clear old session state
clear_old_session_state()

# Define the pages (removed Full Pipeline)
pages = {
    "Step 1: Extract Frames": "extract_frames_app.py", 
    "Step 2: Detect Pockets": "detect_pockets_app.py",
    "Step 3: Cluster Pockets": "cluster_pockets_app.py",
    "Step 4: Molecular Docking": "docking_app.py",
    "Task Monitor": "task_monitor_app.py"
}

# Horizontal menu - styles handled by CSS for theme compatibility
selected = option_menu(
    None,
    list(pages.keys()),
    icons=['file-earmark-arrow-down', 'search', 'diagram-3', 'flask', 'activity'],
    menu_icon="cast",
    default_index=0,
    orientation="horizontal",
    styles={
        "container": {"padding": "0!important", "background-color": "transparent"},
        "icon": {"font-size": "18px"},
        "nav-link": {"font-size": "16px", "text-align": "left", "margin":"0px"},
        "nav-link-selected": {},
    }
)

# Get the corresponding file and import it safely
import importlib

# Map page names to module names (without .py extension)
PAGE_MODULES = {
    "Step 1: Extract Frames": "extract_frames_app",
    "Step 2: Detect Pockets": "detect_pockets_app",
    "Step 3: Cluster Pockets": "cluster_pockets_app",
    "Step 4: Molecular Docking": "docking_app",
    "Task Monitor": "task_monitor_app"
}

# Import and execute the selected module
if selected in PAGE_MODULES:
    module_name = PAGE_MODULES[selected]
    try:
        # Import the module dynamically (SAFE - no exec())
        importlib.import_module(module_name)
    except ImportError as e:
        st.error(f"Failed to load page '{selected}': {e}")
        st.stop()
else:
    st.error(f"Unknown page: {selected}")
    st.stop() 