"""Landing page rendered when no session is loaded.

Two affordances:

1. **Start new analysis** — creates a fresh ``Session`` row, rewrites the
   query string to ``/?s=<short>&edit=<secret>``, reruns. The user lands
   on the normal app, owning the session.

2. **Open existing** — accepts a full URL or a bare short_code, validates
   that the session exists, redirects.

There's also a tiny "expired session" rendering when a user visits
``/?s=<short>`` whose row has been soft-expired. The DB row survives for
audit but the volume + results are gone — we tell them so.
"""
from __future__ import annotations

import re
from typing import Optional

import streamlit as st

from db.sessions import create_session
from session_routes import (
    ResolvedSession,
    build_session_url,
    navigate_to_session,
)


def render_landing() -> None:
    """Draw the "no session loaded" landing page."""
    _render_header()

    col_start, col_open = st.columns([1, 1], gap="large")

    with col_start:
        st.markdown("### Start a new analysis")
        st.caption(
            "Creates a fresh, shareable workspace. You'll get a URL you can "
            "bookmark or share with collaborators. No sign-up required."
        )
        if st.button("Start new analysis", type="primary", use_container_width=True,
                     key="landing_start_new"):
            try:
                row = create_session()
            except Exception as e:
                st.error(f"Couldn't create session: {type(e).__name__}: {e}")
                st.stop()
            navigate_to_session(row.short_code, edit_secret=row.edit_secret)

    with col_open:
        st.markdown("### Open an existing analysis")
        st.caption(
            "Paste a session URL or short code below. View-only by default; "
            "edit access requires the original URL with its `?edit=…` token."
        )
        raw = st.text_input(
            "Session URL or short code",
            value="",
            placeholder="https://… or just the code (e.g. `xY7-aB12cd`)",
            key="landing_open_input",
        )
        if st.button("Open", use_container_width=True, key="landing_open_btn"):
            short = _extract_short_code(raw)
            secret = _extract_edit_secret(raw)
            if not short:
                st.error("Couldn't read a session code from that input.")
            else:
                navigate_to_session(short, edit_secret=secret)

    _render_recent_sessions()


def _render_recent_sessions() -> None:
    """List the sessions this browser has created/visited (B11.21).

    Backed by ``recent_sessions`` (a client-side cookie) — empty on the
    CookieManager's first render, populated after its mount-rerun.
    """
    import recent_sessions

    entries = recent_sessions.list_recent()
    if not entries:
        return
    st.markdown("### Your recent sessions")
    st.caption(
        "Sessions you've opened in this browser — stored only here, "
        "never on the server."
    )
    for e in entries:
        short = e.get("short_code")
        if not short:
            continue
        name = e.get("display_name") or f"session {short}"
        created = (e.get("created") or "")[:10]
        label = f"{name} · `{short}`" + (f" · {created}" if created else "")
        if st.button(label, key=f"recent_{short}", use_container_width=True):
            navigate_to_session(short, edit_secret=e.get("edit_secret"))


def render_session_not_found(short_code: Optional[str]) -> None:
    """``/?s=<bogus>`` — short_code didn't resolve."""
    _render_header()
    if short_code:
        st.error(f"No session found for `{short_code}`.")
    else:
        st.error("Session not found.")
    st.info(
        "Double-check the URL, or click below to start a new analysis. "
        "Sessions that haven't been touched in a while are automatically "
        "expired; their data is gone but the URL still resolves to a "
        "placeholder so you know it existed."
    )
    if st.button("Start new analysis", type="primary"):
        row = create_session()
        navigate_to_session(row.short_code, edit_secret=row.edit_secret)


def render_session_expired(resolved: ResolvedSession) -> None:
    """``/?s=<short>`` for a session in soft-expiry state."""
    _render_header()
    name = resolved.session.display_name if resolved.session else None
    label = f"`{name}`" if name else f"`{resolved.short_code}`"
    st.warning(f"Session {label} has expired.")
    st.caption(
        "Its trajectory, computed results, and uploads are gone (storage "
        "is cleaned up after inactivity). The session is preserved here "
        "for reference only — start a new analysis to continue working."
    )
    if st.button("Start new analysis", type="primary"):
        row = create_session()
        navigate_to_session(row.short_code, edit_secret=row.edit_secret)


def render_session_chip(resolved: ResolvedSession) -> None:
    """Compact chip rendered at the top of every session-loaded page.

    Shows the share URL + an editor/viewer indicator. Called from main.py
    once the session has resolved.
    """
    if resolved.session is None:
        return
    sess = resolved.session
    full_url = build_session_url(sess.short_code, edit_secret=sess.edit_secret if resolved.is_editor else None)
    role = "Editor" if resolved.is_editor else "Viewer"
    name = sess.display_name or f"session {sess.short_code}"

    with st.container():
        col_name, col_url, col_role = st.columns([3, 5, 1])
        col_name.caption(f"📁 **{name}**")
        if resolved.is_editor:
            # B11.21: st.code gives a native copy button so editors can
            # re-share / re-find their ?edit= URL without hunting for it.
            with col_url:
                st.code(full_url, language=None)
        else:
            col_url.caption(f"🔗 `{full_url}`")
        col_role.caption(f"{'✏️' if resolved.is_editor else '👁'} {role}")
    st.caption(
        "**Editor** links (with `?edit=…`) can run jobs and edit the "
        "session; plain links are **read-only viewers**. Anyone holding "
        "the editor URL has editor access."
    )


# ── helpers ──────────────────────────────────────────────────────────────


_URL_RE = re.compile(r"[?&]s=([A-Za-z0-9_-]+)")
_EDIT_RE = re.compile(r"[?&]edit=([A-Za-z0-9_-]+)")
_SHORT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _extract_short_code(raw: str) -> Optional[str]:
    """Accept either a full URL (`https://…/?s=<short>&edit=<secret>`) or a
    bare short code. Return the short code or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = _URL_RE.search(raw)
    if m:
        return m.group(1)
    if _SHORT_RE.match(raw):
        return raw
    return None


def _extract_edit_secret(raw: str) -> Optional[str]:
    """Pull the `edit=` token from a full URL, if present. Bare short codes
    return None (no editor access — view-only)."""
    raw = (raw or "").strip()
    m = _EDIT_RE.search(raw)
    if m:
        return m.group(1)
    return None


def _render_header() -> None:
    """Brutalist landing header — same .bh* template the rest of the app uses."""
    st.markdown(
        """
<div class="bh" style="margin-top: 4px;">
    <div class="bh-row">
        <span class="bh-title">POCKETHUNTER/SUITE</span>
        <span class="bh-version">[v.2.0]</span>
    </div>
    <div class="bh-rule"></div>
    <div class="bh-stages">
        MD-driven pocket discovery, clustering, and docking
    </div>
</div>
""",
        unsafe_allow_html=True,
    )
