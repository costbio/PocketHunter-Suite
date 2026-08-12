"""Inject app-level CSS into Streamlit's bundled index.html so it
applies from page-load zero — before React mounts and before our
WebSocket-delivered <style> blocks arrive.

Pattern mirrors ``streamlit_static_cap.py``: monkey-patch Streamlit's
internals at import time. Re-runs are idempotent — we skip the write
when our marker is already present in the file.

Use case: hide ``stHeader`` from the first paint frame so the brutalist
masthead is the first thing visible after a full HTTP navigation
(masthead's ``<a href>`` links — NEW SESSION, the logo, etc.). Without
this, the browser flashes a ~60 px gray emotion-styled header bar for
~100–500 ms before our ``st.markdown`` ``<style>`` arrives over the
WebSocket and hides it. The flash is short but visually jarring.
"""
from __future__ import annotations


_MARKER = "<!-- pockethunter-shell-css -->"
# Bumped on each meaningful change to the rules below so a redeploy
# re-injects an updated block (otherwise the marker-check would skip
# the write and the old rules would persist in site-packages).
_MARKER_VERSION = "v3"
_EARLY_CSS = """
<link rel="icon" type="image/svg+xml" href="/app/static/favicon.svg">
<style>
  /* Hide Streamlit's default top toolbar — emotion paints a ~60 px
     gray strip when React first mounts, before our WebSocket-delivered
     <style> arrives. */
  header[data-testid="stHeader"] { display: none !important; }
  /* Match main.py's block-container top-padding override so the body
     doesn't shift as our deferred CSS lands. */
  .stMainBlockContainer, .main .block-container {
      padding-top: 0.6rem !important;
  }
  /* Collapse the recent-sessions CookieManager's visible layout slot.
     CookieManager is a streamlit-components-v1 iframe that we mount
     at the top of main.py for browser-side cookie reads; the iframe
     itself is empty but Streamlit reserves a ~36 px tall block-
     container for it, painting a gray strip above the masthead until
     post-mount CSS catches up. visibility:hidden + height:0 keeps the
     iframe in the DOM (so it still loads + runs JS) while taking zero
     layout space. */
  .stElementContainer.st-key-recent_sessions_cm {
      visibility: hidden !important;
      height: 0 !important;
      min-height: 0 !important;
      margin: 0 !important;
      padding: 0 !important;
      overflow: hidden !important;
  }
</style>
"""


def inject_shell_css() -> None:
    try:
        import os
        import re
        import streamlit
        index_path = os.path.join(
            os.path.dirname(streamlit.__file__), "static", "index.html",
        )
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        versioned_marker = f"{_MARKER}{_MARKER_VERSION}"
        if versioned_marker in html:
            return  # current version already injected
        # Strip any older marker + the <style>...</style> that follows
        # it (matches up to and including the first closing </style>).
        # Re-injection idempotency: on _MARKER_VERSION bumps we replace,
        # not append.
        html = re.sub(
            re.escape(_MARKER) + r".*?</style>",
            "",
            html,
            count=1,
            flags=re.DOTALL,
        )
        if "</head>" not in html:
            return
        injection = f"{versioned_marker}{_EARLY_CSS}"
        new_html = html.replace("</head>", f"{injection}</head>", 1)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(new_html)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "Could not inject early shell CSS (%s) — header may flash "
            "during full-page navigations.", e,
        )
