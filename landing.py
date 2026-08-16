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

import html
import json
import re
from typing import Optional

import streamlit as st

from captcha import build_verifier_from_settings, turnstile_widget
from client_ip import client_ip
from config import Config
from db import audit
from db.sessions import create_session
from rate_limiter import RateLimitExceeded, check_session_create_rate_limit
from session_routes import (
    ResolvedSession,
    build_session_url,
    navigate_to_session,
)

# Relative URL for the static tutorial page (Streamlit's static-file route
# serves it at this path — no host/scheme coupling).
TUTORIAL_URL = "/app/static/tutorial/index.html"
HELP_URL = "/app/static/help/index.html"

# Repository + licence. Shown on the landing page because the NAR Web
# Server Issue requires a standard licence to be visible there, not
# merely present in the source tree.
REPO_URL = "https://github.com/costbio/PocketHunter-Suite"
CORE_REPO_URL = "https://github.com/costbio/PocketHunter"
LICENSE_URL = f"{REPO_URL}/blob/main/LICENSE"
LICENSE_NAME = "MIT"

# The bundled sample data, published on the static route so it has a
# stable URL that a paper or a help page can cite. These are copies of
# examples/tem1/ — tests/test_sample_data_published.py asserts the two
# stay byte-identical, since a drifted copy would document a format the
# demo does not actually run.
EXAMPLE_TOPOLOGY_URL = "/app/static/example/topology.pdb"
EXAMPLE_TRAJECTORY_URL = "/app/static/example/trajectory.xtc"


def _render_captcha_if_enabled(*, key: str) -> tuple[bool, str | None]:
    """Render the Turnstile widget and return ``(ok_to_proceed, token_for_verify)``.

    - When ``TURNSTILE_ENABLED=false`` (dev): returns ``(True, None)``
      immediately; no widget rendered.
    - When enabled but the user hasn't solved the challenge yet: returns
      ``(False, None)`` so the caller knows to wait for the user.
    - When the user has solved it: returns ``(True, token)`` so the
      caller can call ``TurnstileVerifier.verify(token, client_ip())``.
    """
    if not Config.TURNSTILE_ENABLED:
        return True, None
    if not Config.TURNSTILE_SITE_KEY:
        # Misconfiguration: enabled but no key. Surface visibly to the
        # operator (NOT a silent fail-open).
        st.error("CAPTCHA is enabled but TURNSTILE_SITE_KEY is unset.")
        return False, None
    token = turnstile_widget(Config.TURNSTILE_SITE_KEY, key=key)
    return (token is not None, token)


def _example_data_available() -> bool:
    """Whether the landing-page demo button should render.

    True iff ``EXAMPLE_TRAJECTORY_DIR`` is set to a real directory
    that holds ``trajectory.xtc`` and a topology file (``.pdb`` or
    ``.gro``). Empty string / missing dir / missing files → return
    False; the button is hidden entirely.
    """
    from pathlib import Path

    raw = (Config.EXAMPLE_TRAJECTORY_DIR or "").strip()
    if not raw:
        return False
    d = Path(raw)
    has_xtc = (d / "trajectory.xtc").is_file()
    has_top = (d / "topology.pdb").is_file() or (d / "topology.gro").is_file()
    return has_xtc and has_top


def _create_example_session() -> "object | None":
    """B3.1: one-click demo — create a session and auto-dispatch a find_pockets
    job against the bundled trypsin trajectory under ``examples/trypsin/``.

    The user lands directly in the running state of the Find Pockets
    panel; no upload required. Honours the per-IP rate limit + CAPTCHA
    same as a manual session-create.
    """
    import shutil
    from pathlib import Path

    from config import Config
    from db.jobs import create_for_legacy
    from panels._shared import new_job_id
    from tasks import run_find_pockets_task
    import streamlit as st

    row = _create_session_with_rate_limit(captcha_widget_key="landing_example_captcha")
    if row is None:
        return None

    # Stage the example trajectory (configurable via EXAMPLE_TRAJECTORY_DIR;
    # default ./examples/trypsin) into the new session's uploads dir, then
    # dispatch a find_pockets job against it.
    example_dir = Path(Config.EXAMPLE_TRAJECTORY_DIR)
    src_xtc = example_dir / "trajectory.xtc"
    # Accept either PDB or GRO topology — the example dir may ship either.
    src_top = example_dir / "topology.pdb"
    if not src_top.exists():
        src_top = example_dir / "topology.gro"
    if not src_xtc.exists() or not src_top.exists():
        st.error(
            f"Example trajectory missing at `{example_dir}`. Set "
            f"`EXAMPLE_TRAJECTORY_DIR` in .env to a directory containing "
            f"`trajectory.xtc` + `topology.pdb` (or `.gro`), or unset "
            f"it to hide the button. Falling back to the blank session."
        )
        return row

    job_id = new_job_id("find_pockets")
    upload_dir = Path(Config.UPLOAD_DIR) / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    xtc_dst = upload_dir / "trajectory_example_demo.xtc"
    top_dst = upload_dir / f"topology_example_demo{src_top.suffix}"
    try:
        shutil.copy2(src_xtc, xtc_dst)
        shutil.copy2(src_top, top_dst)
    except OSError as e:
        st.error(f"Couldn't stage example files: {e}. Falling back to a blank session.")
        return row

    # Phase-D bugfix: call create_for_legacy DIRECTLY with the freshly
    # created session's id. `session_routes.register_session_job` reads
    # `st.session_state["current_session"]`, which on the landing page
    # hasn't been set yet — that no-op left the Job row missing, so the
    # find_pockets panel later fell through to its empty settings form
    # (perceived by the user as "back to the new session screen").
    try:
        create_for_legacy(row.id, "find_pockets", job_id)
    except Exception as e:
        # DB hiccup shouldn't block the user; log but proceed.
        import logging
        logging.getLogger(__name__).warning(
            "example-session Job row insert failed (%s); the running "
            "panel may show an empty form when the task completes.", e,
        )

    task = run_find_pockets_task.delay(
        job_id=job_id,
        xtc_file_path=str(xtc_dst),
        topology_file_path=str(top_dst),
        # Stride 1 on purpose, unlike the panel's default of 10. The bundled
        # example is a 98-frame ensemble, so taking every frame costs little
        # and lets a newcomer see the whole trajectory go through the
        # pipeline. The panel keeps 10 because a real trajectory is orders
        # of magnitude longer and a user picks their own stride there.
        stride=1,
    )
    # Mark the session so the find_pockets panel renders the running
    # state on first load (just like a normal manual submit).
    st.session_state.find_pockets_job_id = job_id
    st.session_state.find_pockets_task_id = task.id
    st.session_state.find_pockets_status = "running"
    return row


def _create_session_with_rate_limit(*, captcha_widget_key: str = "landing_captcha") -> "object | None":
    """Wrap create_session with the per-IP daily cap (C4) + Turnstile CAPTCHA (Phase D).

    Returns the new session row on success, ``None`` if the call was
    refused (an ``st.error`` is rendered in that case so the caller can
    just early-return without further UX work).
    """
    try:
        check_session_create_rate_limit(client_ip())
    except RateLimitExceeded as e:
        st.error(str(e))
        return None

    captcha_ok, captcha_token = _render_captcha_if_enabled(key=captcha_widget_key)
    if not captcha_ok:
        st.info("Please complete the CAPTCHA above to start a new analysis.")
        return None
    if captcha_token is not None:
        verifier = build_verifier_from_settings()
        if not verifier.verify(captcha_token, client_ip()):
            st.error("CAPTCHA verification failed — please try again.")
            return None

    try:
        row = create_session()
    except Exception as e:
        st.error(f"Couldn't create session: {type(e).__name__}: {e}")
        return None
    # B3.4: forensic record. Defensive — failure is logged, not raised.
    audit.record(row.id, client_ip(), "session_create")
    return row


def render_landing() -> None:
    """Draw the "no session loaded" landing page."""
    render_masthead_fragment(resolved=None)

    col_start, col_open = st.columns([1, 1], gap="large")

    with col_start:
        st.markdown("### Start a new analysis")
        st.caption(
            "Creates a fresh, shareable workspace. You'll get a URL you can "
            "bookmark or share with collaborators. No sign-up required."
        )
        if st.button("Start new analysis", type="primary", use_container_width=True,
                     key="landing_start_new"):
            row = _create_session_with_rate_limit()
            if row is None:
                st.stop()
            navigate_to_session(row.short_code, edit_secret=row.edit_secret)

        # B3.1: a zero-click demo so new users see the pipeline work
        # without having to upload anything. Path is configurable via
        # EXAMPLE_TRAJECTORY_DIR in .env; empty / missing → button is
        # hidden (operators opt out by leaving the knob unset).
        if _example_data_available():
            st.caption(
                "_New here? Try the **example demo** — small XTC + topology, "
                "auto-extracts pockets in ~1 min on the fast pool._"
            )
            if st.button("Try with example trajectory", use_container_width=True,
                         key="landing_example_btn"):
                row = _create_example_session()
                if row is None:
                    st.stop()
                navigate_to_session(row.short_code, edit_secret=row.edit_secret)
            # The same two files the button runs, downloadable, so anyone
            # can check what the uploaders expect before preparing their
            # own. Served straight off the static route rather than via
            # st.download_button, which would have to hold 600 KB in
            # memory on every landing-page render.
            st.markdown(
                f'<div class="bh-footer">Inspect the sample data: '
                f'<a href="{EXAMPLE_TOPOLOGY_URL}" download>topology.pdb</a> · '
                f'<a href="{EXAMPLE_TRAJECTORY_URL}" download>trajectory.xtc</a>'
                f'</div>',
                unsafe_allow_html=True,
            )

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
    render_landing_footer()


def render_landing_footer() -> None:
    """Licence, source and contact, shown on the landing page.

    Kept a plain function rather than inlined so the licence line stays
    unit-testable: the NAR Web Server Issue requires a standard licence
    to be *visible on the landing page*, and a requirement that is only
    satisfied by a string buried in a layout is one a refactor can drop
    without anyone noticing.
    """
    st.divider()
    st.markdown(
        f'<div class="bh-footer">'
        f'<span>PocketHunter Suite is free and open-source software, '
        f'released under the <a href="{LICENSE_URL}" target="_blank" '
        f'rel="noopener noreferrer">{LICENSE_NAME} licence</a>. '
        f'Free for academic and commercial use alike.</span>'
        f'<span>Source: '
        f'<a href="{REPO_URL}" target="_blank" rel="noopener noreferrer">suite</a>'
        f' · '
        f'<a href="{CORE_REPO_URL}" target="_blank" rel="noopener noreferrer">pipeline</a>'
        f' · <a href="{TUTORIAL_URL}">tutorial</a>'
        f' · <a href="{HELP_URL}">help</a></span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_recent_sessions() -> None:
    """List the sessions this browser has created/visited (B11.21).

    Backed by ``recent_sessions`` (a client-side cookie) — empty on the
    CookieManager's first render, populated after its mount-rerun. The
    list lives inside an ``st.expander`` (collapsed by default) so a
    long history doesn't crowd the landing-page primary actions; the
    label shows the count so users with one or two sessions know there's
    something to open.
    """
    import recent_sessions

    entries = [e for e in recent_sessions.list_recent() if e.get("short_code")]
    if not entries:
        return
    with st.expander(f"Your recent sessions ({len(entries)})", expanded=False):
        st.caption(
            "Sessions you've opened in this browser — stored only here, "
            "never on the server."
        )
        for e in entries:
            short = e["short_code"]
            name = e.get("display_name") or f"session {short}"
            created = (e.get("created") or "")[:10]
            label = f"{name} · `{short}`" + (f" · {created}" if created else "")
            if st.button(label, key=f"recent_{short}", use_container_width=True):
                navigate_to_session(short, edit_secret=e.get("edit_secret"))


def render_session_not_found(short_code: Optional[str]) -> None:
    """``/?s=<bogus>`` — short_code didn't resolve."""
    render_masthead_fragment(resolved=None)
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
        row = _create_session_with_rate_limit()
        if row is None:
            st.stop()
        navigate_to_session(row.short_code, edit_secret=row.edit_secret)


def render_session_expired(resolved: ResolvedSession) -> None:
    """``/?s=<short>`` for a session in soft-expiry state."""
    # Don't show the session-row chip for an expired session — its data
    # is gone, and the "share me" affordance is misleading. Hide.
    render_masthead_fragment(resolved=None)
    name = resolved.session.display_name if resolved.session else None
    label = f"`{name}`" if name else f"`{resolved.short_code}`"
    st.warning(f"Session {label} has expired.")
    st.caption(
        "Its trajectory, computed results, and uploads are gone (storage "
        "is cleaned up after inactivity). The session is preserved here "
        "for reference only — start a new analysis to continue working."
    )
    if st.button("Start new analysis", type="primary"):
        row = _create_session_with_rate_limit()
        if row is None:
            st.stop()
        navigate_to_session(row.short_code, edit_secret=row.edit_secret)


# The animated pixel-art logo. Inlined directly into the masthead HTML —
# no separate asset file, no build step. A 4 px cell grid fills the
# magnifier's raster scan region (11×10 = 110 cells covering x ∈ [12, 52],
# y ∈ [12, 48]) so the magnifier always passes over coloured structure.
#
# Pastel patchwork palette — the brand's pale yellow as the protein
# body, with three pastel patches (pink / mint / lavender) reading as
# different residue regions on the surface. No central pocket — too
# heavy at this scale; the magnifier ring alone carries the "finding"
# concept. All hexes are soft enough to sit comfortably under the
# acid-yellow brand accent without competing for attention.
#
#   #f0f0f0  off-white      — outside corners; rounds the silhouette so
#                              the 11×10 square reads as a globular protein.
#   #f5ffa3  pale yellow    — protein body. Same colour as the primary-
#                              button background.
#   #ffd6e0  pale pink      — warm patch, upper-centre.
#   #c8e8d0  pale mint      — cool patch, upper-left.
#   #d8d0f0  pale lavender  — cool patch, mid-right.
#
# Each colour region is contiguous and packed against its neighbours.
# White appears only at the corners. The magnifier raster-scans every
# 8 seconds and passes over each patch in turn.
#
# The magnifier is anchored at SVG origin (0, 0); CSS @keyframes drive
# its position via `transform: translate(...)`. Anchoring at origin
# means the keyframe coordinates correspond directly to viewport
# positions — no manual offsets to keep in sync between SVG + CSS.
#
# `prefers-reduced-motion` freezes the magnifier at its starting position.
_LOGO_SVG = """\
<svg class="bh-logo" viewBox="0 0 64 64" width="64" height="64"
     aria-label="PocketHunter — magnifier scanning a protein surface" role="img">
  <title>PocketHunter</title>
  <g class="surface">
    <!-- Rounded silhouette: 3 white cells in each corner (2 on the
         edge row + 1 inset) so the 11×10 square reads as globular. -->
    <g fill="#f0f0f0">
      <rect x="12" y="12" width="4" height="4"/><rect x="16" y="12" width="4" height="4"/>
      <rect x="48" y="12" width="4" height="4"/><rect x="52" y="12" width="4" height="4"/>
      <rect x="12" y="16" width="4" height="4"/><rect x="52" y="16" width="4" height="4"/>
      <rect x="12" y="44" width="4" height="4"/><rect x="52" y="44" width="4" height="4"/>
      <rect x="12" y="48" width="4" height="4"/><rect x="16" y="48" width="4" height="4"/>
      <rect x="48" y="48" width="4" height="4"/><rect x="52" y="48" width="4" height="4"/>
    </g>
    <!-- Pale yellow protein body — base colour wherever a patch
         isn't present. -->
    <g fill="#f5ffa3">
      <rect x="20" y="12" width="4" height="4"/><rect x="24" y="12" width="4" height="4"/>
      <rect x="40" y="12" width="4" height="4"/><rect x="44" y="12" width="4" height="4"/>
      <rect x="16" y="16" width="4" height="4"/><rect x="20" y="16" width="4" height="4"/>
      <rect x="44" y="16" width="4" height="4"/><rect x="48" y="16" width="4" height="4"/>
      <rect x="12" y="20" width="4" height="4"/><rect x="16" y="20" width="4" height="4"/>
      <rect x="40" y="20" width="4" height="4"/><rect x="44" y="20" width="4" height="4"/>
      <rect x="48" y="20" width="4" height="4"/><rect x="52" y="20" width="4" height="4"/>
      <rect x="24" y="24" width="4" height="4"/><rect x="28" y="24" width="4" height="4"/>
      <rect x="32" y="24" width="4" height="4"/><rect x="36" y="24" width="4" height="4"/>
      <rect x="40" y="24" width="4" height="4"/>
      <rect x="20" y="28" width="4" height="4"/><rect x="24" y="28" width="4" height="4"/>
      <rect x="28" y="28" width="4" height="4"/><rect x="32" y="28" width="4" height="4"/>
      <rect x="36" y="28" width="4" height="4"/><rect x="40" y="28" width="4" height="4"/>
      <rect x="16" y="32" width="4" height="4"/><rect x="20" y="32" width="4" height="4"/>
      <rect x="24" y="32" width="4" height="4"/><rect x="28" y="32" width="4" height="4"/>
      <rect x="32" y="32" width="4" height="4"/><rect x="36" y="32" width="4" height="4"/>
      <rect x="40" y="32" width="4" height="4"/><rect x="52" y="32" width="4" height="4"/>
      <rect x="12" y="36" width="4" height="4"/><rect x="16" y="36" width="4" height="4"/>
      <rect x="20" y="36" width="4" height="4"/><rect x="24" y="36" width="4" height="4"/>
      <rect x="28" y="36" width="4" height="4"/><rect x="32" y="36" width="4" height="4"/>
      <rect x="36" y="36" width="4" height="4"/><rect x="40" y="36" width="4" height="4"/>
      <rect x="52" y="36" width="4" height="4"/>
      <rect x="12" y="40" width="4" height="4"/><rect x="16" y="40" width="4" height="4"/>
      <rect x="20" y="40" width="4" height="4"/><rect x="24" y="40" width="4" height="4"/>
      <rect x="28" y="40" width="4" height="4"/><rect x="32" y="40" width="4" height="4"/>
      <rect x="36" y="40" width="4" height="4"/><rect x="40" y="40" width="4" height="4"/>
      <rect x="48" y="40" width="4" height="4"/><rect x="52" y="40" width="4" height="4"/>
      <rect x="16" y="44" width="4" height="4"/><rect x="20" y="44" width="4" height="4"/>
      <rect x="24" y="44" width="4" height="4"/><rect x="28" y="44" width="4" height="4"/>
      <rect x="32" y="44" width="4" height="4"/><rect x="36" y="44" width="4" height="4"/>
      <rect x="40" y="44" width="4" height="4"/><rect x="44" y="44" width="4" height="4"/>
      <rect x="48" y="44" width="4" height="4"/>
      <rect x="20" y="48" width="4" height="4"/><rect x="24" y="48" width="4" height="4"/>
      <rect x="28" y="48" width="4" height="4"/><rect x="32" y="48" width="4" height="4"/>
      <rect x="36" y="48" width="4" height="4"/><rect x="40" y="48" width="4" height="4"/>
      <rect x="44" y="48" width="4" height="4"/>
    </g>
    <!-- Pale pink patch — warm, upper-centre. -->
    <g fill="#ffd6e0">
      <rect x="28" y="12" width="4" height="4"/><rect x="32" y="12" width="4" height="4"/>
      <rect x="36" y="12" width="4" height="4"/>
      <rect x="24" y="16" width="4" height="4"/><rect x="28" y="16" width="4" height="4"/>
      <rect x="32" y="16" width="4" height="4"/><rect x="36" y="16" width="4" height="4"/>
      <rect x="40" y="16" width="4" height="4"/>
      <rect x="28" y="20" width="4" height="4"/><rect x="32" y="20" width="4" height="4"/>
      <rect x="36" y="20" width="4" height="4"/>
    </g>
    <!-- Pale mint patch — cool, upper-left. -->
    <g fill="#c8e8d0">
      <rect x="20" y="20" width="4" height="4"/><rect x="24" y="20" width="4" height="4"/>
      <rect x="12" y="24" width="4" height="4"/><rect x="16" y="24" width="4" height="4"/>
      <rect x="20" y="24" width="4" height="4"/>
      <rect x="12" y="28" width="4" height="4"/><rect x="16" y="28" width="4" height="4"/>
      <rect x="12" y="32" width="4" height="4"/>
    </g>
    <!-- Pale lavender patch — cool, mid-right. -->
    <g fill="#d8d0f0">
      <rect x="44" y="24" width="4" height="4"/><rect x="48" y="24" width="4" height="4"/>
      <rect x="52" y="24" width="4" height="4"/>
      <rect x="44" y="28" width="4" height="4"/><rect x="48" y="28" width="4" height="4"/>
      <rect x="52" y="28" width="4" height="4"/>
      <rect x="44" y="32" width="4" height="4"/><rect x="48" y="32" width="4" height="4"/>
      <rect x="44" y="36" width="4" height="4"/><rect x="48" y="36" width="4" height="4"/>
      <rect x="44" y="40" width="4" height="4"/>
    </g>
  </g>
  <g class="magnifier">
    <circle cx="0" cy="0" r="5" fill="none" stroke="#000" stroke-width="2"/>
    <line x1="3.5" y1="3.5" x2="8" y2="8" stroke="#000" stroke-width="2"
          stroke-linecap="square"/>
  </g>
</svg>"""


def handle_new_session_request() -> None:
    """One-click 'create a new session' flow, invoked by ``?new=1``.

    Dev path (CAPTCHA disabled): create the session, swap the URL
    in-place via ``st.query_params`` (which sends a page-info-changed
    ForwardMsg WITHOUT triggering a script rerun), then render the
    full analysis page in the same iteration. The user sees one clean
    render instead of the two-iteration flow (masthead-only flash →
    analysis page) that the previous ``navigate_to_session`` +
    ``st.rerun`` approach produced.

    Prod path (CAPTCHA enabled): the create call returns ``None`` while
    the Turnstile widget waits for the user to solve the challenge.
    Render the masthead + a hint; Streamlit re-runs the script on solve
    and we land back here — at which point the create succeeds and the
    fast-path takes over.

    Called from ``main.py`` before the usual session-resolve dispatch
    so that ``?new=1`` short-circuits whatever else was in the URL.
    """
    row = _create_session_with_rate_limit(
        captcha_widget_key="new_session_captcha",
    )
    if row is None:
        # Rate-limited or CAPTCHA pending — render the masthead +
        # widget so the user can solve / read the error. Caller will
        # st.stop() so the landing page doesn't render below.
        render_masthead_fragment(resolved=None)
        st.caption(
            "Creating a new analysis session… complete the verification "
            "above if shown, or wait if rate-limited."
        )
        return

    # ── Single-iteration fast-path ───────────────────────────────────
    # st.query_params.__setitem__ enqueues a `page_info_changed`
    # ForwardMsg (URL bar update) but does NOT trigger a rerun —
    # contrast with the explicit st.rerun() in
    # session_routes.navigate_to_session. So we can mutate the URL in-
    # place AND keep rendering the page in this same script run.
    st.query_params["s"] = row.short_code
    st.query_params["edit"] = row.edit_secret
    if "new" in st.query_params:
        del st.query_params["new"]

    # The NEW SESSION click doesn't disconnect Streamlit's WebSocket;
    # session_state carries over from whatever session the user was just
    # on. Clear the keys that would force render_analysis_app into an
    # extra render iteration (e.g. active_stage="Dock" left over from
    # the previous session → mismatch → reset → flicker). Stage-locked
    # state is the only thing that needs explicit reset; viewer / cluster
    # / docking session-scoped keys are keyed by short_code so they
    # don't collide.
    for k in (
        "active_stage",
        "pending_active_stage",
        "docking_view_job_id",
        "cluster_view_job_id",
        "find_pockets_view_job_id",
        "docking_task_id", "docking_job_id", "docking_status",
        "docking_running_bucket", "docking_running_results_dir",
        "cluster_task_id", "cluster_job_id", "cluster_status",
        "find_pockets_task_id", "find_pockets_job_id", "find_pockets_status",
    ):
        st.session_state.pop(k, None)

    # Reproduce main.py's post-resolve setup so the analysis page sees
    # the same state it would on a normal session-URL load.
    resolved = ResolvedSession(
        session=row,
        is_editor=True,
        is_expired=False,
        short_code=row.short_code,
    )
    from session_routes import set_session_in_state
    set_session_in_state(resolved)
    try:
        from db.sessions import touch_last_active
        touch_last_active(row)
    except Exception:
        pass
    # NOTE: deliberately NOT calling recent_sessions.record_session here.
    # The CookieManager's cookie write triggers a Streamlit rerun (the
    # component sends the cookie state back over the WebSocket, which
    # Streamlit treats as a widget change). On NEXT page load — when the
    # user revisits this session URL via bookmark / browser history /
    # the recent-sessions expander — main.py's normal post-resolve flow
    # calls record_session at that point, and the user is on a stable
    # page where the extra rerun isn't visually disruptive. For THIS
    # one-shot create-and-render path, skipping it avoids the rerun-
    # flicker (body content briefly clears between renders).
    from analysis_app import render_analysis_app
    render_analysis_app(resolved)


def _build_pool_load_chips_html() -> str:
    """Inner HTML for the nav-row pool-load chips (between NEW SESSION
    and costbio). Re-computed every time ``render_masthead`` fires —
    that's the whole masthead's 15 s fragment cadence.

    Fail-open: any DB hiccup logs + returns an empty string so the rest
    of the masthead still renders.
    """
    try:
        from pool_load import compute_pool_load, format_wait_seconds
        fast = compute_pool_load("fast")
        dock = compute_pool_load("docking")
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not compute pool load — chip area will be empty.",
            exc_info=True,
        )
        return ""

    def _chip(load) -> str:
        total = max(load.workers_total, 1)
        filled = min(load.workers_busy, total)
        bar = "█" * filled + "░" * (total - filled)
        full = load.workers_busy >= total
        bar_cls = "bh-load-bar bh-load-warn" if full else "bh-load-bar"
        # Three states with distinct copy: idle / busy-no-queue / busy+queued.
        if load.workers_busy == 0 and load.queued == 0:
            meta_text = "idle"
        elif load.queued == 0:
            meta_text = f"{load.workers_busy}/{load.workers_total} busy"
        else:
            wait = format_wait_seconds(load.est_wait_seconds)
            wait_part = f" · {wait}" if wait else ""
            meta_text = (
                f"{load.workers_busy}/{load.workers_total} busy · "
                f"{load.queued} queued{wait_part}"
            )
        meta_cls = "bh-load-meta bh-load-warn" if full else "bh-load-meta"
        label = "FAST" if load.pool == "fast" else "DOCK"
        # Tooltip copy lives next to the HTML it annotates so future
        # pool renames stay in one diff.
        tooltips = {
            "fast": "Find Pockets and Cluster jobs run on this pool.",
            "docking": "Smina docking jobs run on this pool.",
        }
        tip = html.escape(tooltips.get(load.pool, ""), quote=True)
        return (
            '<span class="bh-load-pool">'
            f'<span class="bh-load-label" data-tooltip="{tip}">{label}</span>'
            f'<span class="{bar_cls}">{bar}</span>'
            f'<span class="{meta_cls}">{meta_text}</span>'
            '</span>'
        )

    return (
        '<span class="bh-load-chips">'
        f'{_chip(fast)}'
        '<span class="bh-load-sep">·</span>'
        f'{_chip(dock)}'
        '</span>'
    )


@st.dialog("Help")
def _help_dialog() -> None:
    st.markdown(
        "**Help is on the way.**\n\n"
        "A help centre with FAQs, troubleshooting, and the PocketHunter "
        "method paper is being prepared. In the meantime, questions go "
        "to **costbio@gtubeng** via the link at the bottom of the nav row."
    )


def render_masthead(resolved: Optional[ResolvedSession] = None) -> None:
    """Unified brutalist masthead.

    Top row: ``[logo] POCKETHUNTER/SUITE | <session-inline-if-any> | [v.2.0]``
    Rule + nav row (always rendered):
        ``⌂ New session   FAST … · DOCK …   costbio@gtubeng``

    Session metadata is hoisted into the top row so the entire 'where am
    I' message lives on one line. Stages are intentionally absent —
    ``analysis_app``'s ``st.segmented_control`` immediately below the
    masthead already serves as the stage indicator + selector.

    Plain (non-fragment) function so the masthead HTML stays unit-
    testable from bare-mode pytest. The polling shell that refreshes the
    pool-load chips is ``render_masthead_fragment`` — call sites in the
    live app use that wrapper instead of this one.
    """
    # Session info, hoisted inline into the top row. When no session is
    # loaded (landing / not-found / expired) the slot is reused for a
    # one-sentence product description so first-time visitors know what
    # they're looking at.
    session_inline_html = ""
    is_session_loaded = resolved is not None and resolved.session is not None
    if not is_session_loaded:
        session_inline_html = (
            '<span class="bh-intro">'
            'Pocket detection, clustering, and ensemble docking for '
            'molecular-dynamics trajectories.'
            '</span>'
        )
    if is_session_loaded:
        sess = resolved.session
        is_editor = bool(resolved.is_editor)
        full_url = build_session_url(
            sess.short_code,
            edit_secret=sess.edit_secret if is_editor else None,
        )
        # HTML-escape for the visible <code> + JS-escape (via json.dumps)
        # for the clipboard payload. The URL itself is server-generated
        # from a fixed alphabet (short_code + edit_secret are URL-safe
        # base64), but defence-in-depth against future display_name leaks.
        url_html = html.escape(full_url, quote=True)
        url_js = json.dumps(full_url)
        display_name = sess.display_name or f"session {sess.short_code}"
        name_html = html.escape(display_name, quote=True)
        role_icon, role_label = ("✏️", "Editor") if is_editor else ("👁", "Viewer")
        # Copy buttons rendered via st.components.v1.html below the
        # markdown — onclick is stripped by st.markdown.
        # Copy buttons use data-copy-url spans (survives markdown
        # sanitizer). Click handlers are injected via Dockerfile script.
        _view_url = build_session_url(sess.short_code, edit_secret=None)
        _copy_btns = (
            '<span class="bh-copy-btn" data-copy-url="' + html.escape(_view_url, quote=True) + '">view-only link</span>'
        )
        if is_editor:
            _edit_url = build_session_url(sess.short_code,
                                          edit_secret=sess.edit_secret)
            _copy_btns += (
                ' <span class="bh-copy-btn" data-copy-url="' + html.escape(_edit_url, quote=True) + '">editor link</span>'
            )
        session_inline_html = (
            '<span class="bh-session-inline">'
            f'<span class="bh-session-name">📁 <strong>{name_html}</strong></span>'
            '<span class="bh-session-url">'
            f'<code class="bh-url">{url_html}</code>'
            f'{_copy_btns}'
            '</span>'
            f'<span class="bh-role">{role_icon} {role_label}</span>'
            '</span>'
        )

    # Top row + rule line as ONE markdown blob. Static content; no
    # widgets needed. The nav row (with the 3 buttons + chips +
    # costbio) is rendered below as a Streamlit columns row inside the
    # same container, so the visual frame stays continuous.
    #
    # The brand-mark is wrapped in <a href="/"> so a click on either
    # the logo or the wordmark returns to the landing page.
    top_row_html = (
        '<div class="bh-row">'
        f'<a class="bh-mark" href="/" target="_self" title="PocketHunter Suite — home">{_LOGO_SVG}'
        '<span class="bh-title">POCKETHUNTER/SUITE</span>'
        '</a>'
        f'{session_inline_html}'
        '<span class="bh-right">'
        '<span class="bh-version">[v.2.0]</span>'
        '</span>'
        '</div>'
        '<div class="bh-rule"></div>'
    )

    # st.container with key="ph_masthead" lets CSS draw the brutalist
    # border around BOTH the top-row markdown AND the nav-row columns
    # (via .st-key-ph_masthead). The .bh class is no longer the outer
    # frame — it just styles the top-row contents.
    with st.container(key="ph_masthead"):
        st.markdown(top_row_html, unsafe_allow_html=True)

        # Nav row: 3 buttons + chips + costbio link, in 5 columns.
        # Ratio sized so the buttons take roughly their text-width,
        # chips fill the middle, costbio pins right.
        nav_cols = st.columns([2, 2, 2, 7, 2], gap="small",
                              vertical_alignment="center")
        with nav_cols[0]:
            if st.button(
                "⌂ NEW SESSION",
                key="nav_new_session",
                use_container_width=True,
            ):
                # Inline (not on_click) — callbacks inside fragments
                # don't reliably escape to a page-level rerun with
                # st.rerun(scope="app"). The inline branch runs in the
                # fragment's body, sets the flag, then escapes.
                st.session_state["_pending_new_session"] = True
                st.rerun(scope="app")
        with nav_cols[1]:
            st.link_button("TUTORIAL", TUTORIAL_URL, key="nav_tutorial",
                           use_container_width=True)
        with nav_cols[2]:
            if st.button("HELP", key="nav_help",
                         use_container_width=True):
                _help_dialog()
        with nav_cols[3]:
            st.markdown(
                f'<div class="bh-load-chips-row">{_build_pool_load_chips_html()}</div>',
                unsafe_allow_html=True,
            )
        with nav_cols[4]:
            st.markdown(
                '<div class="bh-costbio-row">'
                '<a class="bh-group-link" href="https://costbio.github.io" '
                'target="_blank" rel="noopener noreferrer">costbio@gtubeng</a>'
                '</div>',
                unsafe_allow_html=True,
            )


@st.fragment(run_every="15s")
def render_masthead_fragment(resolved: Optional[ResolvedSession] = None) -> None:
    """Polling wrapper around ``render_masthead``.

    The masthead's pool-load chips (built inside ``render_masthead`` via
    ``_build_pool_load_chips_html``) need a heartbeat so they refresh
    even when nothing else on the page is rerunning. Wrapping the bare
    function in a ``@st.fragment(run_every="15s")`` keeps the polling
    concern out of the testable surface.

    Every 15 s the wrapper re-runs and the entire ``.bh`` HTML blob is
    rebuilt. Cost is one ``st.markdown`` call — cheap. Identity-stable
    structure means the visible diff is just the chip text/colour
    on the load chips.
    """
    render_masthead(resolved=resolved)


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


