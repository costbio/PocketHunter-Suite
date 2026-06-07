"""Smoke tests for ``streamlit_static_cap.lift_streamlit_static_cap``.

Guard against Streamlit refactoring the 200 MB ``MAX_APP_STATIC_FILE_SIZE``
constant out from under us — the production symptom is a blank Mol*
viewport with no in-app error, so a fast unit-test trip is worth
the small import cost.
"""
from __future__ import annotations


def test_lift_raises_cap_to_max_viewer_bytes():
    from streamlit_static_cap import lift_streamlit_static_cap

    lift_streamlit_static_cap()

    from config import Config
    from streamlit.web.server.starlette import (
        starlette_routes,
        starlette_server_config,
    )

    assert starlette_routes.MAX_APP_STATIC_FILE_SIZE >= int(Config.MAX_VIEWER_BYTES), (
        "starlette_routes.MAX_APP_STATIC_FILE_SIZE was not lifted to "
        f"MAX_VIEWER_BYTES={Config.MAX_VIEWER_BYTES}; large viewer files "
        "will 404 from /app/static/"
    )
    # Source-of-truth module also patched — guards against any code path
    # re-importing the constant.
    assert (
        starlette_server_config.MAX_APP_STATIC_FILE_SIZE
        == starlette_routes.MAX_APP_STATIC_FILE_SIZE
    )


def test_lift_never_lowers_cap(monkeypatch):
    """If a user accidentally sets MAX_VIEWER_BYTES below Streamlit's
    default 200 MB, the helper must NOT lower the cap — uses max()."""
    import config
    from streamlit.web.server.starlette import starlette_routes
    from streamlit_static_cap import lift_streamlit_static_cap

    high_water = max(
        starlette_routes.MAX_APP_STATIC_FILE_SIZE,
        200 * 1024 * 1024,  # Streamlit's documented default
    )
    monkeypatch.setattr(config.Config, "MAX_VIEWER_BYTES", 1)

    lift_streamlit_static_cap()

    assert starlette_routes.MAX_APP_STATIC_FILE_SIZE >= high_water


def test_lift_is_idempotent():
    """Calling lift twice must be safe (e.g. if a future contributor
    accidentally wires it into another startup path)."""
    from streamlit.web.server.starlette import starlette_routes
    from streamlit_static_cap import lift_streamlit_static_cap

    lift_streamlit_static_cap()
    first = starlette_routes.MAX_APP_STATIC_FILE_SIZE
    lift_streamlit_static_cap()
    second = starlette_routes.MAX_APP_STATIC_FILE_SIZE

    assert first == second
