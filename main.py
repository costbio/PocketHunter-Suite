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


# Raise Streamlit's hardcoded 200 MB cap on /app/static/ so multi-frame
# viewer.pdb files (which can be 300+ MB for 1 k+ frames) actually serve
# instead of silently 404'ing. See streamlit_static_cap.py for details.
from streamlit_static_cap import lift_streamlit_static_cap
lift_streamlit_static_cap()

# Inject early CSS into Streamlit's bundled index.html so stHeader is
# hidden from the first paint frame. Without this, full-page navs (NEW
# SESSION, the logo click) flash a gray header bar for ~100-500 ms
# before our st.markdown <style> arrives over the WebSocket.
from streamlit_shell_css import inject_shell_css
inject_shell_css()

# Serve large /app/static/ files gzipped so the compressed payload fits
# in Chrome's per-resource disk cache slot (~64 MB). Without this, big
# viewer.pdb files hit ERR_CACHE_WRITE_FAILURE → Mol* "Failed to download
# data" → blank viewport. See streamlit_gzip_static.py for details.
from streamlit_gzip_static import install_gzip_static
install_gzip_static()


from analysis_app import render_analysis_app
from landing import (
    handle_new_session_request,
    render_landing,
    render_session_expired,
    render_session_not_found,
)
from session_routes import resolve_session_from_query, set_session_in_state
import recent_sessions


st.set_page_config(
    page_title="PocketHunter Suite",
        layout="wide",
    initial_sidebar_state="collapsed",
    # These were placeholder your-repo URLs that 404'd for anyone who
    # clicked them. Get Help points at the help page rather than the
    # repository — someone reaching for help wants to read how to use the
    # thing, not browse source.
    menu_items={
        'Get Help': '/app/static/help/index.html',
        'Report a bug': "https://github.com/costbio/PocketHunter-Suite/issues",
        'About': (
            "# PocketHunter Suite\n\n"
            "Pocket detection, clustering and ensemble docking for "
            "molecular-dynamics trajectories. Free and open source under "
            "the MIT licence — https://github.com/costbio/PocketHunter-Suite"
        ),
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

    /* Brutalist outer frame — moved from .bh to the Streamlit container
       (key="ph_masthead") so it wraps BOTH the top-row markdown AND the
       nav-row st.columns. .bh becomes a pass-through inside.
       Streamlit injects extra padding on container children — kill it
       so the top-row HTML and the columns row sit flush. */
    .st-key-ph_masthead {
        border: 2px solid #000;
        padding: 10px 18px;
        margin: 4px 0 14px 0;
        background: #fff;
    }
    .st-key-ph_masthead [data-testid="stVerticalBlock"] {
        gap: 0;          /* collapse the default 1rem vertical gap
                            between the top-row markdown and the nav-row */
    }
    .st-key-ph_masthead [data-testid="stMarkdown"] p { margin: 0; }
    /* Streamlit columns inside the container also carry extra padding
       on their direct child; zero it out so the nav row sits flush
       against the .bh-rule above it. */
    .st-key-ph_masthead [data-testid="stHorizontalBlock"] {
        gap: 0.5rem;
        align-items: center;
    }
    /* Compact buttons inside the masthead — match the visual weight of
       the prior .bh-newsession link (small, brutalist, no rounding).

       TUTORIAL and HELP are st.link_buttons, which Streamlit renders as
       <a href> and not <button> at all, so their `a` lines are the ones
       actually styling them; NEW SESSION is an st.button and matches
       `button`. The unused `button` lines for the two links are kept
       because Streamlit has changed this element before. Delete an `a`
       line while tidying and that link silently reverts to Streamlit's
       default anchor — no test catches it, because the masthead tests
       assert on the Python-side capture rather than on this CSS. */
    .st-key-nav_new_session button,
    .st-key-nav_tutorial button,
    .st-key-nav_tutorial a,
    .st-key-nav_help button,
    .st-key-nav_help a {
        font-family: 'JetBrains Mono', ui-monospace, monospace !important;
        font-size: 0.85rem !important;
        font-weight: 600 !important;
        padding: 4px 10px !important;
        min-height: 0 !important;
        background: #f0f0f0 !important;
        color: #555 !important;
        border: 2px solid #000 !important;
        text-transform: uppercase;
        text-decoration: none !important;
    }
    .st-key-nav_new_session button:hover:not(:disabled),
    .st-key-nav_tutorial button:hover:not(:disabled),
    .st-key-nav_tutorial a:hover,
    .st-key-nav_help button:hover:not(:disabled),
    .st-key-nav_help a:hover {
        background: #e0e0e0 !important;
        color: #000 !important;
    }
    /* Right-align the chips + costbio inside their columns */
    .bh-load-chips-row {
        display: flex;
        justify-content: flex-start;
        align-items: center;
    }
    .bh-costbio-row {
        display: flex;
        justify-content: flex-end;
        align-items: center;
    }

    .bh {
        border: none;       /* outer frame moved to .st-key-ph_masthead */
        padding: 0;
        margin: 0;
        background: transparent;
        font-family: 'JetBrains Mono', ui-monospace, monospace;
    }
    .bh-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 16px;
    }
    .bh-title {
        /* Bumped from 1.15rem in masthead-v2 to match the 64×64 logo
           height. line-height: 1 keeps the row's vertical rhythm tight
           — the logo (not the descender) sets the row height. */
        font-size: 1.5rem;
        font-weight: 800;
        line-height: 1;
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
    /* Brand mark — animated SVG logo sits inline-left of the wordmark.
       align-items: center keeps the magnifier's optical midpoint level
       with the wordmark's centerline. gap bumped to 14px for the
       larger 64×64 logo.
       Note: bh-mark is an <a href="/"> (logo + wordmark both link to
       home). Overrides below kill the default anchor styling (text
       underline + Streamlit's injected link colour) so the brand
       still reads as the brand, not a link. The cursor + opacity-on-
       hover give a subtle 'this is clickable' affordance. */
    .bh-mark {
        display: inline-flex;
        align-items: center;
        gap: 14px;
        flex-shrink: 0;
        text-decoration: none !important;
        color: inherit !important;
        cursor: pointer;
    }
    .bh-mark:hover { opacity: 0.75; }
    .bh-logo { display: block; flex-shrink: 0; }
    /* Note: the .tint disc was dropped in masthead-v3 when the surface
       gained electrostatic colours (blue/red cells). The mix-blend-mode
       multiply tint turned blue cells olive-green and red cells dark-red,
       obscuring the colour information. The lens ring alone now provides
       the visual focal point. */
    .bh-logo .magnifier {
        /* Raster zigzag: magnifier scans top row L→R, drops, scans R→L,
           drops, etc. — 4 rows, ending at bottom-left then a quick snap
           back to start. The magnifier is anchored at SVG origin
           (cx=cy=0) so these translate values are absolute viewport
           positions. Sweeps are slower than drops (gives the 'scan'
           rhythm) and the final snap-back is shorter than a full row. */
        animation: bh-raster 8s linear infinite;
        transform-origin: 0 0;
    }
    @keyframes bh-raster {
        0%   { transform: translate(12px, 12px); }
        20%  { transform: translate(52px, 12px); }
        25%  { transform: translate(52px, 24px); }
        45%  { transform: translate(12px, 24px); }
        50%  { transform: translate(12px, 36px); }
        70%  { transform: translate(52px, 36px); }
        75%  { transform: translate(52px, 48px); }
        95%  { transform: translate(12px, 48px); }
        100% { transform: translate(12px, 12px); }
    }
    @media (prefers-reduced-motion: reduce) {
        .bh-logo .magnifier {
            animation: none;
            /* Static fallback: pin magnifier at the start of the raster
               path so it sits over the top-left of the surface. Without
               this it would render at SVG origin (top-left corner). */
            transform: translate(12px, 12px);
        }
    }

    /* Top row — taller now to accommodate the 64×64 logo. */
    .bh-row {
        align-items: center;
        min-height: 64px;
    }

    /* Session info, hoisted INLINE into the top row (was bh-session-row).
       Sits between the brand-mark and the right-cluster (version pill);
       flex: 1 stretches to fill, URL ellipsizes when narrow. */
    .bh-session-inline {
        display: inline-flex;
        align-items: center;
        gap: 14px;
        margin-left: 24px;
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        font-size: 0.8rem;
        min-width: 0;
        flex: 1;
    }
    .bh-session-name { color: #000; white-space: nowrap; flex-shrink: 0; }

    /* Intro tagline — fills the session-info slot in the brand row
       when no session is loaded. Quiet grey, monospaced, lets the
       reader scan the page without the brand competing. flex:1 makes
       it absorb the same horizontal space the session-info chip
       occupies, balancing the row. */
    .bh-intro {
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        font-size: 0.85rem;
        color: #555;
        margin-left: 24px;
        flex: 1;
        min-width: 0;
        line-height: 1.3;
    }
    .bh-session-url {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        min-width: 0;
        flex: 1;
    }
    .bh-url {
        font-family: inherit;
        font-size: 0.8rem;
        padding: 2px 6px;
        background: #f0f0f0;
        border: 1px solid #ccc;
        user-select: all;          /* triple-click selects the whole URL */
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 100%;
        min-width: 0;
        color: #000;
    }
    .bh-copy, .bh-copy-btn {
        font-family: inherit;
        font-size: 0.7rem;
        padding: 2px 8px;
        background: #f0f0f0;
        border: 2px solid #000;
        color: #000;
        cursor: pointer;
        white-space: nowrap;
        text-transform: uppercase;
        flex-shrink: 0;
    }
    .bh-copy:hover, .bh-copy-btn:hover { background: #d4ff00; }
    .bh-role { white-space: nowrap; color: #000; flex-shrink: 0; }

    /* Bottom nav row — hosts ⌂ New session and future Help / Tutorial
       buttons. Only renders when a session is loaded (see render_masthead). */
    .bh-nav-row {
        display: flex;
        align-items: center;
        gap: 16px;
        margin-top: 8px;
        min-height: 32px;        /* fits the 0.9rem text + padding */
    }

    /* Right cluster — just the version pill now (New session moved
       down into bh-nav-row). margin-left: auto pushes it to the far
       right of the flex row. */
    .bh-right { display: flex; align-items: center; gap: 10px; margin-left: auto; flex-shrink: 0; }
    .bh-newsession {
        /* Bumped from 0.7rem to 0.9rem in masthead-v5 — matches the
           font size of body buttons/labels in the panels below so the
           nav row reads as part of the same UI register, not a fine-
           print metadata strip. */
        font-size: 0.9rem;
        font-weight: 600;
        /* ``!important`` overrides Streamlit's theme linkColor (#000)
           injection, which would otherwise paint <a> text black. */
        color: #555 !important;
        background: #f0f0f0;
        padding: 4px 10px;
        border: 2px solid #000;
        text-decoration: none !important;
        text-transform: uppercase;
        white-space: nowrap;
    }
    .bh-newsession:hover {
        background: #e0e0e0;
        color: #000 !important;
    }

    /* Research-group affiliation link in the nav row — plain underlined
       text (not a button) per the brutalist 'links look like links' rule.
       margin-left: auto pushes it to the right edge of bh-nav-row
       regardless of whether ⌂ New session sits to its left. The
       !important on color mirrors .bh-newsession — beats Streamlit's
       theme linkColor injection. */
    .bh-group-link {
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        /* Same 0.9rem as .bh-newsession so the two nav-row elements
           share a baseline visual weight. */
        font-size: 0.9rem;
        font-weight: 600;
        color: #555 !important;
        text-decoration: underline;
        text-underline-offset: 2px;
        margin-left: auto;
        white-space: nowrap;
    }
    .bh-group-link:hover {
        color: #000 !important;
        text-decoration-thickness: 2px;
    }

    /* Landing-page footer carrying the licence, source links and the
       cookie notice. Muted and small — it is a legal/provenance strip,
       not navigation — but deliberately not hidden: the NAR Web Server
       Issue requires the licence to be visible on the landing page. */
    .bh-footer {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem 1.5rem;
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        font-size: 0.75rem;
        line-height: 1.6;
        color: #666;
    }
    .bh-footer a {
        color: #444 !important;
        text-decoration: underline;
        text-underline-offset: 2px;
    }
    .bh-footer a:hover {
        color: #000 !important;
        text-decoration-thickness: 2px;
    }

    /* Cookie consent copy. Slightly larger than .bh-footer — it is a
       question the visitor has to actually read and answer, not fine
       print to be skimmed past. */
    .bh-consent {
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        font-size: 0.8rem;
        line-height: 1.65;
        color: #333;
        margin-bottom: 0.5rem;
    }
    .bh-consent code {
        font-size: 0.75rem;
        background: #f4f4f4;
        padding: 0 0.25em;
    }

    /* Inline pool-load chips inside the nav row — between NEW SESSION
       and the costbio link. Smaller font (0.75rem vs the nav row's
       0.9rem) so the busiest copy ("FAST ██████ 6/6 busy · 2 queued ·
       ~30s · DOCK …") still fits on one line at 1400 px viewport. */
    .bh-load-chips {
        display: inline-flex;
        align-items: center;
        gap: 16px;
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        font-size: 0.75rem;
        margin-left: 24px;        /* breathing room from ⌂ NEW SESSION */
        flex: 0 1 auto;            /* shrink before wrapping */
        min-width: 0;
    }
    .bh-load-pool {
        display: inline-flex;
        gap: 8px;
        align-items: baseline;
        white-space: nowrap;
    }
    .bh-load-label {
        font-weight: 700;
        color: #000;
        min-width: 36px;
        position: relative;        /* anchor for ::after tooltip */
        cursor: help;
        border-bottom: 1px dotted #888;  /* visual cue that it's hoverable */
    }
    .bh-load-label::after {
        content: attr(data-tooltip);
        position: absolute;
        bottom: calc(100% + 6px);
        left: 0;
        background: #d4ff00;       /* brand acid-yellow */
        color: #000;
        border: 2px solid #000;
        padding: 6px 10px;
        font-family: 'JetBrains Mono', ui-monospace, monospace;
        font-size: 0.7rem;
        font-weight: 500;
        letter-spacing: normal;     /* override .bh-load-bar's -1px */
        white-space: nowrap;
        opacity: 0;
        pointer-events: none;
        transition: opacity 120ms;
        z-index: 100;
    }
    .bh-load-label:hover::after { opacity: 1; }
    .bh-load-bar {
        display: inline-block;
        color: #000;
        letter-spacing: -1px;      /* tighten the ▆▆░░ glyph spacing */
    }
    .bh-load-meta { color: #555; }
    .bh-load-warn { color: #cc0000 !important; }
    .bh-load-sep { color: #999; font-weight: 700; }
    /* nav row already uses flex; let it wrap on narrow viewports so
       chips drop below NEW SESSION / costbio instead of being
       truncated, and give wrapped rows visible vertical breathing
       room. */
    .bh-nav-row { flex-wrap: wrap; gap: 8px 16px; }

    /* B11.23: pale "Riso" palette. Secondary stays achromatic; chromatic
       fills are reserved for meaning (yellow = go, blush = danger).
       The acid-yellow #d4ff00 is held back to the hover state — brand
       reserve. ``!important`` beats Streamlit's theme CSS, same as the
       rules above. data-testid / st-key- are Streamlit internals —
       re-verify after a Streamlit upgrade. */
    button[data-testid^="stBaseButton-primary"] {
        background: #f5ffa3 !important;
        color: #000 !important;
        border: 2px solid #000 !important;
    }
    button[data-testid^="stBaseButton-primary"]:hover:not(:disabled) {
        background: #d4ff00 !important;
        color: #000 !important;
    }
    button[data-testid^="stBaseButton-secondary"] {
        background: #f0f0f0 !important;
        color: #000 !important;
        border: 2px solid #000 !important;
    }
    button[data-testid^="stBaseButton-secondary"]:hover:not(:disabled) {
        background: #e0e0e0 !important;
    }
    /* Destructive actions — pale blush + red ink, targeted by widget
       key. These are default-type buttons, so this block must come
       AFTER the secondary rule to win on source order. */
    .st-key-docking_bucket_remove button,
    .st-key-docking_bucket_clear button,
    .st-key-dock_strip_clear button,
    .st-key-panel_cluster_clear_sel button {
        background: #ffe0e0 !important;
        color: #cc0000 !important;
    }
    .st-key-docking_bucket_remove button:hover:not(:disabled),
    .st-key-docking_bucket_clear button:hover:not(:disabled),
    .st-key-dock_strip_clear button:hover:not(:disabled),
    .st-key-panel_cluster_clear_sel button:hover:not(:disabled) {
        background: #ffcccc !important;
        color: #a30000 !important;
    }
    /* Disabled — near-white, softly inert regardless of role. */
    button[data-testid^="stBaseButton-primary"]:disabled,
    button[data-testid^="stBaseButton-secondary"]:disabled {
        background: #f8f8f8 !important;
        color: #bbb !important;
        border-color: #ccc !important;
    }
</style>
""", unsafe_allow_html=True)


_resolved = resolve_session_from_query()
set_session_in_state(_resolved)

# B11.21: mount the per-browser recent-sessions cookie component once,
# before the page dispatch — both the landing page (lists recent
# sessions) and the analysis page (records the current one) use it.
recent_sessions.init()

# Ask before writing the one persistent cookie this service sets. Must
# come after recent_sessions.init() (it reuses that CookieManager) and
# before any code path that could call record_session.
import cookie_consent  # noqa: E402 — must follow the CookieManager mount

cookie_consent.render_banner()

# Mol* embedding spike entry-point. Unlinked from the main nav; used for
# diagnostic checks when the viewer misbehaves.
if st.query_params.get("spike") == "molstar":
    from spike_molstar import render_spike_page
    render_spike_page()
    st.stop()

# `⌂ New session` in the masthead nav links to `/?new=1` — intercept
# here so the user gets a freshly created session in one click instead
# of landing on the landing page and having to click "Start new analysis"
# again. CAPTCHA-enabled deployments will render the Turnstile widget
# in handle_new_session_request and the create completes on solve.
#
# Two entry points feed this:
#   1. ?new=1 in the URL — direct link, bookmark, or copy-paste.
#   2. `_pending_new_session` session-state flag — set by the masthead
#      NEW SESSION button's on_click handler (it's inside a fragment
#      so it can't navigate the page itself; instead it sets the flag
#      + scope="app" reruns).
if (
    st.query_params.get("new") == "1"
    or st.session_state.pop("_pending_new_session", False)
):
    handle_new_session_request()
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

# Render the analysis app — single page, no nav strip. The unified
# masthead (brand + session chip in one strip) is rendered inside
# render_analysis_app via _render_header → landing.render_masthead.
render_analysis_app(_resolved)
