"""PocketHunter Suite entry point.

After v2 Phase B B7, ``main.py`` is a thin dispatcher:

1. Set the Streamlit page config + global brutalist styling.
2. Resolve the URL's session via :mod:`session_routes`.
3. Branch on session state:
   - no ``?s=`` → landing page (start new / open existing)
   - unknown ``?s=`` → "session not found" page
   - expired session → "session expired" page
   - ``?spike=molstar`` → Mol* embedding spike (diagnostic; unlinked)
   - otherwise → render the analysis app

The legacy multi-page nav (option_menu + runpy + cached_job_ids +
pending_nav) is gone. Old ``?v=2`` URLs keep working — the query param
is ignored, and analysis_app is the only path.
"""
from __future__ import annotations

import streamlit as st

from analysis_app import render_analysis_app
from landing import (
    render_landing,
    render_session_chip,
    render_session_expired,
    render_session_not_found,
)
from session_routes import resolve_session_from_query, set_session_in_state
import recent_sessions


st.set_page_config(
    page_title="PocketHunter Suite",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="collapsed",
    menu_items={
        'Get Help': 'https://github.com/your-repo/pockethunter',
        'Report a bug': "https://github.com/your-repo/pockethunter/issues",
        'About': "# PocketHunter Suite\nA modern molecular dynamics pocket detection and analysis tool.",
    },
)


# Brutalist scientific aesthetic — bespoke header styles only.
# Global theming (fonts, colors, borders, radii) lives in .streamlit/config.toml.
st.markdown("""
<style>
    /* Reclaim vertical space: hide Streamlit's top toolbar + tighten
       the main block's top padding. This frees roughly 80–110px of
       viewport height so the viewer + slider + panel fit without
       scrolling on smaller laptop screens. */
    header[data-testid="stHeader"] { display: none !important; }
    .stMainBlockContainer, .main .block-container {
        padding-top: 0.6rem !important;
        padding-bottom: 1rem !important;
        max-width: 1600px !important;  /* cap on ultra-wide so the viewer
                                          doesn't stretch awkwardly */
    }

    .bh {
        border: 2px solid #000;
        padding: 10px 18px;
        margin: 0 0 14px 0;
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
        font-size: 1.15rem;
        font-weight: 800;
        letter-spacing: -0.01em;
        text-transform: uppercase;
        color: #000;
    }
    .bh-version {
        font-size: 0.7rem;
        font-weight: 600;
        color: #000;
        background: #d4ff00;
        padding: 2px 8px;
        border: 2px solid #000;
        white-space: nowrap;
    }
    .bh-rule { border-top: 1px solid #000; margin: 6px 0; }
    .bh-bar {
        height: 6px;
        margin: 5px 0 8px 0;
        background-image: repeating-linear-gradient(
            90deg,
            #d4ff00 0 14px,
            #000 14px 16px
        );
    }
    .bh-stages {
        font-size: 0.7rem;
        font-weight: 600;
        letter-spacing: 0.08em;
        color: #000;
        text-transform: uppercase;
    }
    .bh-stages span.sep { color: #d4ff00; padding: 0 6px; }
</style>
""", unsafe_allow_html=True)


_resolved = resolve_session_from_query()
set_session_in_state(_resolved)

# B11.21: mount the per-browser recent-sessions cookie component once,
# before the page dispatch — both the landing page (lists recent
# sessions) and the analysis page (records the current one) use it.
recent_sessions.init()

# Mol* embedding spike entry-point. Unlinked from the main nav; used for
# diagnostic checks when the viewer misbehaves.
if st.query_params.get("spike") == "molstar":
    from spike_molstar import render_spike_page
    render_spike_page()
    st.stop()

if _resolved.session is None and _resolved.short_code is None:
    render_landing()
    st.stop()

if _resolved.session is None:
    render_session_not_found(_resolved.short_code)
    st.stop()

if _resolved.is_expired:
    render_session_expired(_resolved)
    st.stop()

# A session is loaded. Touch last_active_at (best-effort).
try:
    from db.sessions import touch_last_active
    touch_last_active(_resolved.session)
except Exception:
    pass

# B11.21: remember this session in the browser's recent-sessions list.
recent_sessions.record_session(
    _resolved.session.short_code,
    display_name=_resolved.session.display_name,
    edit_secret=_resolved.session.edit_secret if _resolved.is_editor else None,
)

# Compact share-URL chip + Editor/Viewer indicator.
render_session_chip(_resolved)

# Render the analysis app — single page, no nav strip.
render_analysis_app(_resolved)
