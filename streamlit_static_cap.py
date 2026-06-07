"""Lift Streamlit's hardcoded 200 MB cap on ``/app/static/`` files.

Streamlit's ``_app_static_endpoint`` rejects any static file larger
than ``MAX_APP_STATIC_FILE_SIZE = 200 * 1024 * 1024`` with HTTP 404
"File is too large" (source: ``starlette/starlette_server_config.py``,
used in ``starlette/starlette_routes.py``). Our Mol* viewer artefacts
(multi-model PDBs) can easily exceed that for 1k+ frame trajectories;
the symptom is "viewport renders nothing" because the browser silently
404s on the structure URL with no in-app error.

Our own ``Config.MAX_VIEWER_BYTES`` is the user-facing knob — workers
refuse to *build* viewer files above it. Patching Streamlit's
constant to that value (or higher) wires the delivery side to respect
the same limit. ``max(...)`` keeps Streamlit's default if a user
sets ``MAX_VIEWER_BYTES`` below 200 MB.

Technique: ``_app_static_endpoint`` is a closure that resolves
``MAX_APP_STATIC_FILE_SIZE`` from its enclosing module's globals at
request time — re-binding the name on ``starlette_routes`` takes
effect immediately. We also patch the source-of-truth module in
``starlette_server_config`` so any future Streamlit refactor that
re-imports the constant doesn't silently reset the cap.

Defensive: any import / attribute error is logged and swallowed —
we don't want a Streamlit refactor to break the app import.

Lives in its own module (rather than inline in main.py) so it can
be unit-tested without dragging in main's page-script side effects.
"""
from __future__ import annotations


def lift_streamlit_static_cap() -> None:
    try:
        from streamlit.web.server.starlette import (
            starlette_routes,
            starlette_server_config,
        )
        from config import Config

        current = starlette_server_config.MAX_APP_STATIC_FILE_SIZE
        new_cap = max(int(Config.MAX_VIEWER_BYTES), current)
        starlette_routes.MAX_APP_STATIC_FILE_SIZE = new_cap
        starlette_server_config.MAX_APP_STATIC_FILE_SIZE = new_cap
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "Could not lift Streamlit static-file cap (%s) — large viewer "
            "files (>200 MB) will 404 from /app/static/", e,
        )
