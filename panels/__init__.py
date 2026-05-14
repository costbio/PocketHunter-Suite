"""Right-pane stage panels for the v2 analysis app.

Each panel exposes ``render(session, is_editor) -> None`` and is dispatched
from ``analysis_app.py`` based on ``st.session_state.active_stage``.
"""
