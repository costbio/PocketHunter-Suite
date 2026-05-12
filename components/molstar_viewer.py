"""Mol* viewer Streamlit Components v2 component.

v2 Phase B commit B1 — inline CCv2 component, Mol* loaded from jsDelivr
at runtime. No Node toolchain required for this iteration; we graduate
to a packaged component (with bundled Mol*) only if the spike's bundle
weight or load latency proves unacceptable.

The component exposes one Python entry point::

    from components.molstar_viewer import molstar_viewer

    result = molstar_viewer(
        pdb_url="https://files.rcsb.org/download/1CBS.pdb",
        key="viewer_1",
        on_clicked_change=lambda: print(st.session_state["viewer_1"].clicked),
    )
    st.write("ready_ms:", result.ready_ms)
    st.write("loaded_ms:", result.loaded_ms)

State (persists across reruns):
    * ``ready_ms``  — milliseconds from JS start to Mol* viewer ready.
    * ``loaded_ms`` — milliseconds from load call to structure rendered.

Triggers (fire once per event):
    * ``clicked``   — emitted when the user clicks the viewer surface.

isolate_styles is OFF so Mol*'s own CSS (injected into document.head from
the CDN) can target Mol*'s elements. This means the viewer renders into
the light DOM rather than a shadow root.
"""
from __future__ import annotations

from collections.abc import Callable

import streamlit as st


_HTML = '<div id="molstar-root" style="width: 100%; height: 600px; position: relative; background: #000;"></div>'

_JS = r"""
const MOLSTAR_VERSION = "4.7.0";
const MOLSTAR_JS = `https://cdn.jsdelivr.net/npm/molstar@${MOLSTAR_VERSION}/build/viewer/molstar.js`;
const MOLSTAR_CSS = `https://cdn.jsdelivr.net/npm/molstar@${MOLSTAR_VERSION}/build/viewer/molstar.css`;

// One-time CSS injection — Mol*'s CSS lives in document.head so it can
// target the viewer's elements (which sit in the light DOM since the
// component runs with isolate_styles=False).
if (!document.querySelector(`link[data-mol-css="1"]`)) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = MOLSTAR_CSS;
  link.dataset.molCss = "1";
  document.head.appendChild(link);
}

// One-time Mol* UMD bundle load. The bundle exposes `window.molstar`.
let _molstarLoadingPromise = null;
function loadMolstar() {
  if (window.molstar) return Promise.resolve(window.molstar);
  if (_molstarLoadingPromise) return _molstarLoadingPromise;
  _molstarLoadingPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = MOLSTAR_JS;
    script.onload = () => {
      if (window.molstar) resolve(window.molstar);
      else reject(new Error("Mol* loaded but window.molstar is undefined"));
    };
    script.onerror = (e) => reject(e);
    document.head.appendChild(script);
  });
  return _molstarLoadingPromise;
}

// Cache the viewer instance per host element so re-renders don't reload.
const VIEWERS = new WeakMap();

export default async function (component) {
  const { data, parentElement, setStateValue, setTriggerValue } = component;
  const root = parentElement.querySelector("#molstar-root");
  if (!root) return;

  const t0 = performance.now();
  await loadMolstar();
  const tLoaded = performance.now();

  let viewer = VIEWERS.get(root);
  if (!viewer) {
    viewer = await window.molstar.Viewer.create(root, {
      layoutIsExpanded: false,
      layoutShowControls: true,
      layoutShowRemoteState: false,
      layoutShowSequence: true,
      layoutShowLog: false,
      layoutShowLeftPanel: false,
      viewportShowExpand: true,
      viewportShowControls: true,
      viewportShowSettings: false,
      viewportShowSelectionMode: true,
      viewportShowAnimation: false,
    });
    VIEWERS.set(root, viewer);

    // Single-time click wiring. Mol* canvas events bubble out of root.
    root.addEventListener("click", () => {
      setTriggerValue("clicked", Date.now());
    });

    setStateValue("ready_ms", Math.round(performance.now() - t0));
  }

  // Structure load — only re-load if pdb_url changed.
  if (data?.pdb_url && data.pdb_url !== root.dataset.lastPdbUrl) {
    const tStart = performance.now();
    root.dataset.lastPdbUrl = data.pdb_url;
    try {
      await viewer.loadStructureFromUrl(data.pdb_url, "pdb");
      setStateValue("loaded_ms", Math.round(performance.now() - tStart));
    } catch (err) {
      setStateValue("load_error", String(err));
    }
  }
}
"""


_MOLSTAR = st.components.v2.component(
    "molstar_viewer",
    html=_HTML,
    js=_JS,
)


def molstar_viewer(
    pdb_url: str | None = None,
    *,
    key: str | None = None,
    height: int = 620,
    on_clicked_change: Callable[[], None] | None = None,
    on_ready_ms_change: Callable[[], None] | None = None,
    on_loaded_ms_change: Callable[[], None] | None = None,
):
    """Render a Mol* viewer.

    Args:
        pdb_url: URL to a PDB file. ``None`` renders an empty viewer.
        key: Streamlit widget key (required for state to persist).
        height: Viewer container height in pixels.
        on_clicked_change: Optional callback for click events.
        on_ready_ms_change: Optional callback when ``ready_ms`` is set.
        on_loaded_ms_change: Optional callback when ``loaded_ms`` is set.

    Returns:
        Component result. Attributes:
            ``ready_ms`` — int milliseconds from JS start to viewer ready.
            ``loaded_ms`` — int milliseconds from load call to structure done.
            ``clicked`` — opaque timestamp; fires on each click.
            ``load_error`` — error string if structure load failed.
    """
    # Provide no-op callbacks so the result object always has these
    # attributes (CCv2 omits them when the callback isn't given).
    # isolate_styles=False so Mol*'s CSS (loaded into document.head from
    # jsDelivr) can target the viewer's elements (light DOM).
    return _MOLSTAR(
        data={"pdb_url": pdb_url, "height": height},
        key=key,
        height=height,
        isolate_styles=False,
        on_clicked_change=on_clicked_change or (lambda: None),
        on_ready_ms_change=on_ready_ms_change or (lambda: None),
        on_loaded_ms_change=on_loaded_ms_change or (lambda: None),
    )
