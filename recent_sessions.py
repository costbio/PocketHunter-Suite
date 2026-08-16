"""Per-browser 'recent sessions' history (B11.21).

A small client-side history of the sessions a browser has created or
visited, stored in a single cookie (``ph_recent_sessions``) via
``extra_streamlit_components.CookieManager`` — already a dependency. The
list is **never** persisted server-side; it lives only in the visitor's
browser.

Usage:
    # once, near the top of main.py — mounts the cookie component:
    recent_sessions.init()
    # when a session resolves (main.py):
    recent_sessions.record_session(short_code, display_name, edit_secret)
    # on the landing page (landing.py):
    for entry in recent_sessions.list_recent():
        ...

The CookieManager needs one render cycle to read ``document.cookie``;
``init()`` re-mounts it every run and a session-state latch
(``_recent_cm_ready``) defers the first write past the placeholder run
so an existing cookie is never clobbered.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import streamlit as st

_COOKIE = "ph_recent_sessions"
_MAX_RECENT = 10

# Store the edit_secret alongside each entry so the browser keeps editor
# access to its own sessions. The cookie is per-origin and client-side;
# the tradeoff is that anyone with access to this browser profile
# inherits edit rights. Flip to False to keep only view-only links.
INCLUDE_EDIT_SECRET = True


def init() -> None:
    """Mount the CookieManager — call once near the top of ``main.py``.

    Re-instantiated every run (the component must re-render to deliver
    fresh cookie data); the handle is stashed in session_state for the
    other helpers in this module.
    """
    try:
        import extra_streamlit_components as stx
        st.session_state["_recent_cm"] = stx.CookieManager(key="recent_sessions_cm")
    except Exception:
        st.session_state["_recent_cm"] = None


def _cm():
    return st.session_state.get("_recent_cm")


def _parse(raw) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def list_recent() -> list[dict]:
    """Return the browser's recent-sessions list, newest-first.

    Empty on the CookieManager's first (placeholder) render — the
    mount-rerun then re-populates it.
    """
    cm = _cm()
    if cm is None:
        return []
    try:
        return _parse(cm.get(_COOKIE))
    except Exception:
        return []


def record_session(
    short_code: str,
    display_name: str | None = None,
    edit_secret: str | None = None,
) -> None:
    """Append/refresh a session in the browser's recent list.

    Idempotent per browser-session (a ``_recent_done_<code>`` latch) and
    deferred past the CookieManager's placeholder render so an existing
    cookie is never clobbered.
    """
    if not short_code:
        return
    cm = _cm()
    if cm is None:
        return
    # No persistent cookie before the visitor has said yes. Absence of an
    # answer is not consent, so this also covers the runs before the
    # cookie component has delivered anything.
    from cookie_consent import has_consent

    if not has_consent():
        return
    # The CookieManager returns a placeholder on its first run this
    # browser session; writing then would overwrite a real cookie.
    # Latch past that first run (the mount-rerun re-enters here).
    if not st.session_state.get("_recent_cm_ready"):
        st.session_state["_recent_cm_ready"] = True
        return
    done_flag = f"_recent_done_{short_code}"
    if st.session_state.get(done_flag):
        return
    try:
        current = _parse(cm.get(_COOKIE))
        entry: dict = {
            "short_code": str(short_code),
            "display_name": str(display_name or ""),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if INCLUDE_EDIT_SECRET and edit_secret:
            entry["edit_secret"] = str(edit_secret)
        # Drop any prior entry for this code, prepend, cap.
        rest = [e for e in current if e.get("short_code") != entry["short_code"]]
        updated = ([entry] + rest)[:_MAX_RECENT]
        cm.set(
            _COOKIE,
            json.dumps(updated),
            key="recent_sessions_set",
            expires_at=datetime.now(timezone.utc) + timedelta(days=365),
        )
        st.session_state[done_flag] = True
    except Exception:
        # Cookie unavailable / blocked — degrade silently.
        pass
