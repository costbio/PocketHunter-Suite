"""Mol* viewer Streamlit Components v2 component.

v2 Phase B B10 — switched from the jsDelivr ``viewer-bundle`` UMD (which
only exposes ``Viewer`` and hides the rest of Mol*) to our own
Vite-built bridge at ``/app/static/js/molstar-bridge.js``. The bridge
wraps Mol* and exposes a focused API on ``window.molstarBridge`` for
pocket surfaces, cluster overpaints, ligand pose loading, camera
focus, and residue-click round-trips. See ``frontend/`` for the
bridge source.

Python entry point::

    from components.molstar_viewer import molstar_viewer

    molstar_viewer(
        structure_url="/app/static/<short>/viewer.pdb",
        structure_format="pdb",
        annotations={
            "pockets":   [{"residues": ["A_125"], "color": "#FF5733", "label": "Pocket 1"}],
            "clusters":  [{"cluster_id": 0, "residues": [...], "color": "#33FF57"}],
            "ligand_pose": {"sdf": "<v2000>", "color": "#FFA500", "label": "lig"} or None,
            "focus":     {"type": "pocket", "target": 0} or None,
        },
        current_model=3,
        on_residue_clicked_change=lambda: print(...),
        key="viewer_x",
    )

State / triggers:
    * ``ready_ms``         — JS init to viewer-ready (ms).
    * ``loaded_ms``        — load call to structure rendered (ms).
    * ``load_error``       — load error message, if any.
    * ``annotations_applied`` — JSON checksum of last-applied annotations.
    * ``clicked``          — opaque ms timestamp, fires on any click.
    * ``residue_clicked``  — ``"A_125"`` or ``null`` on empty-area clicks.
"""
from __future__ import annotations

from collections.abc import Callable

import streamlit as st


_HTML = '<div id="molstar-root" style="width: 100%; height: 440px; position: relative; background: #000;"></div>'

_JS = r"""
// =============================================================
// Mol* viewer JS bridge — persistent across Streamlit re-mounts
// =============================================================
//
// Streamlit destroys the component's parentElement on every fragment
// re-run. To preserve the Mol* WebGL context + parsed structure
// (~358 MB for the trypsin demo) across these re-mounts, we use a
// *floating-canvas* pattern:
//
//   - Mol*'s entire DOM tree lives in a stable host appended directly
//     to document.body — Streamlit never touches it.
//   - The Streamlit-managed #molstar-root div becomes a layout
//     placeholder; we mirror its bounding box into the floating host's
//     absolute-position styles via getBoundingClientRect().
//   - A persistent loading overlay (separate document.body host) sits
//     on top during initial load + error states.
//   - State is keyed by structure_url so navigating to a different
//     session evicts the previous viewer's memory.

const BRIDGE_JS = "/app/static/js/molstar-bridge.js";
const BRIDGE_CSS = "/app/static/js/molstar-bridge.css";

if (!document.querySelector(`link[data-mol-bridge-css="1"]`)) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = BRIDGE_CSS;
  link.dataset.molBridgeCss = "1";
  document.head.appendChild(link);
}

let _bridgePromise = null;
function loadBridge() {
  if (window.molstarBridge) return Promise.resolve(window.molstarBridge);
  if (_bridgePromise) return _bridgePromise;
  _bridgePromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = BRIDGE_JS;
    script.onload = () => {
      if (window.molstarBridge) resolve(window.molstarBridge);
      else reject(new Error("bridge loaded but window.molstarBridge undefined"));
    };
    script.onerror = (e) => reject(e);
    document.head.appendChild(script);
  });
  return _bridgePromise;
}

// =============================================================
// Document-level singletons (attached to window so they survive
// any re-import of this module across Streamlit script re-runs).
// =============================================================
const W = window;
if (!W.__MOL_CACHE) W.__MOL_CACHE = new Map();           // url → { viewer, host }
if (!W.__OVERLAY_STATES) W.__OVERLAY_STATES = new Map(); // url → 'loading'|'loaded'|'error'

// One-time CSS injection for the loading-overlay dot animation.
// Guarded by the same data-attribute pattern as the bridge CSS link.
if (!document.querySelector('style[data-molstar-overlay-css="1"]')) {
  const css = document.createElement("style");
  css.dataset.molstarOverlayCss = "1";
  css.textContent = `
    [data-molstar-overlay-host="1"] {
      box-sizing: border-box;
      font-family: 'JetBrains Mono', ui-monospace, monospace;
    }
    .mol-dots {
      display: inline-block;
      margin-left: 4px;
      min-width: 1.4em;
      text-align: left;
      letter-spacing: 0.1em;
    }
    .mol-dots > span {
      opacity: 0.2;
      animation: mol-dot-pulse 1.4s infinite ease-in-out;
    }
    .mol-dots > span:nth-child(2) { animation-delay: 0.2s; }
    .mol-dots > span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes mol-dot-pulse {
      0%, 80%, 100% { opacity: 0.2; }
      40%           { opacity: 1; }
    }
    @media (prefers-reduced-motion: reduce) {
      .mol-dots > span { animation: none; opacity: 0.6; }
    }
  `;
  document.head.appendChild(css);
}

// Get or create the persistent Mol* host for a given structure_url.
// Evicts any other cached entries (we keep at most one viewer at a
// time — navigating to a different session releases ~4 GB of parsed
// structure memory).
function getOrCreateFloatingHost(url) {
  let info = W.__MOL_CACHE.get(url);
  if (info) return info;
  for (const [k, v] of W.__MOL_CACHE) {
    try { v.viewer && v.viewer.dispose && v.viewer.dispose(); } catch (_) {}
    try { v.host && v.host.remove(); } catch (_) {}
    W.__MOL_CACHE.delete(k);
    W.__OVERLAY_STATES.delete(k);
  }
  const host = document.createElement("div");
  host.dataset.molstarFloatingHost = "1";
  Object.assign(host.style, {
    position: "absolute",
    top: "0", left: "0", width: "0", height: "0",
    background: "#000",
    zIndex: "100",
    pointerEvents: "auto",
    overflow: "hidden",
  });
  document.body.appendChild(host);
  info = { host: host, viewer: null };
  W.__MOL_CACHE.set(url, info);
  return info;
}

// Get or create the persistent overlay host.
function getOrCreateOverlayHost() {
  let host = document.querySelector('[data-molstar-overlay-host="1"]');
  if (host) return host;
  host = document.createElement("div");
  host.dataset.molstarOverlayHost = "1";
  Object.assign(host.style, {
    position: "absolute",
    top: "0", left: "0", width: "0", height: "0",
    display: "none",
    alignItems: "center",
    justifyContent: "center",
    background: "rgba(255, 255, 255, 0.97)",
    color: "#000",
    fontSize: "0.95rem",
    fontWeight: "600",
    letterSpacing: "0.05em",
    textTransform: "uppercase",
    border: "2px solid #000",
    pointerEvents: "none",
    transition: "opacity 0.18s ease-out",
    opacity: "1",
    zIndex: "101",
  });
  host.innerHTML = `LOADING MOLECULAR STRUCTURE<span class="mol-dots"><span>.</span><span>.</span><span>.</span></span>`;
  document.body.appendChild(host);
  return host;
}

function showOverlay(host) {
  host.style.display = "flex";
  host.style.opacity = "1";
}

function hideOverlay(host) {
  if (host.style.display === "none") return;
  host.style.opacity = "0";
  setTimeout(() => {
    // Re-check before hiding — a fresh load may have shown it again.
    if (host.style.opacity === "0") host.style.display = "none";
  }, 220);
}

function setOverlayError(host, msg) {
  host.innerHTML = "";
  host.textContent = msg;
  host.style.color = "#cc0000";
  host.style.display = "flex";
  host.style.opacity = "1";
}

// Mirror a placeholder element's bounding box into a host's absolute-
// position styles. Returns false if the placeholder is gone OR not
// laid out yet (rect 0×0). In the 0×0 case we *don't* mutate display
// — the placeholder's ResizeObserver will fire realign once layout
// arrives. The earlier "hide on 0×0" behaviour caused a chicken-and-
// egg deadlock where showOverlay set display:flex, alignTo immediately
// hid it because the placeholder hadn't been laid out, and the
// realign loop refused to re-align a hidden overlay.
function alignTo(host, placeholder) {
  if (!placeholder || !placeholder.isConnected) {
    host.style.display = "none";
    return false;
  }
  const r = placeholder.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) {
    return false;  // leave display alone; ResizeObserver will retry
  }
  host.style.top = `${window.scrollY + r.top}px`;
  host.style.left = `${window.scrollX + r.left}px`;
  host.style.width = `${r.width}px`;
  host.style.height = `${r.height}px`;
  return true;
}

// Window-level scroll + resize listeners set up ONCE. They re-align
// both the floating canvas and the visible overlay to the placeholder
// on every layout shift. The placeholder reference is stashed on
// ``W.__molstarPlaceholder`` by each script run — we can't rely on
// ``document.querySelector("#molstar-root")`` because Streamlit's
// component v2 mounts the template in a context that isn't always
// reachable from the document-root selector chain. The script-set
// reference is the authoritative pointer.
if (!W.__molstarAlignmentSetup) {
  W.__molstarAlignmentSetup = true;
  let rafPending = false;
  const realign = () => {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      const placeholder = W.__molstarPlaceholder;
      const floating = document.querySelector('[data-molstar-floating-host="1"]');
      const overlay = document.querySelector('[data-molstar-overlay-host="1"]');
      // ``isConnected`` is the reliable "still in the DOM" check across
      // light + shadow trees. The script-set reference itself going
      // null means no session is loaded.
      const inDOM = placeholder && placeholder.isConnected;
      if (inDOM) {
        if (floating) {
          floating.style.display = "block";
          alignTo(floating, placeholder);
        }
        // Always re-align the overlay so the size stays right even
        // when it's hidden between cycles. Visibility is a separate
        // concern handled by showOverlay/hideOverlay.
        if (overlay) {
          alignTo(overlay, placeholder);
        }
      } else {
        // Placeholder is null OR stale-detached. The lifecycle
        // MutationObserver schedules the actual host removal at
        // 300 ms; hiding on the next realign tick gives the immediate
        // visual clearing (no waiting). The brief transient detach
        // during a normal same-page rerun gets re-shown on the next
        // realign tick when the new placeholder lands — which the
        // first-mount realign cascade (RAF + 50 ms + 250 ms timeouts
        // at viewer-mount time) already triggers.
        if (floating) floating.style.display = "none";
        if (overlay) overlay.style.display = "none";
      }
    });
  };
  window.addEventListener("resize", realign);
  // Streamlit hosts the scrollbar on an inner container
  // ([data-testid="stMain"]) and puts overflow:hidden on body, so
  // window itself never scrolls — the window-level listener below is
  // a no-op in this layout. Catch the real scroll via a capture-phase
  // listener on document: scroll events don't bubble, so capture is
  // the only way to observe scroll on any descendant without
  // enumerating containers. The window listener stays for layouts
  // that DO scroll the window (defence against future Streamlit
  // refactors).
  window.addEventListener("scroll", realign, { passive: true });
  document.addEventListener("scroll", realign, { capture: true, passive: true });
  W.__molstarRealign = realign;
}

async function applyAnnotations(viewer, annotations, host, setTriggerValue) {
  annotations = annotations || {};
  const fingerprint = JSON.stringify({
    pockets: annotations.pockets || [],
    clusters: annotations.clusters || [],
    ligand_pose: annotations.ligand_pose || null,
    focus: annotations.focus || null,
  });
  if (host.dataset.lastAnnotations === fingerprint) return;
  host.dataset.lastAnnotations = fingerprint;

  await viewer.clearPocketSurfaces();
  for (const p of (annotations.pockets || [])) {
    await viewer.showPocketSurface(p);
  }

  await viewer.clearClusterOverpaints();
  for (const c of (annotations.clusters || [])) {
    await viewer.showClusterOverpaint(c);
  }

  if (annotations.ligand_pose && annotations.ligand_pose.sdf) {
    await viewer.loadLigandPose(
      annotations.ligand_pose.sdf,
      annotations.ligand_pose.color || "#ffa500"
    );
  } else {
    await viewer.clearLigandPose();
  }

  if (annotations.focus) {
    const f = annotations.focus;
    if (f.type === "residues" && Array.isArray(f.target)) {
      await viewer.focusOnResidues(f.target);
    } else if (f.type === "pocket" && Array.isArray(annotations.pockets)) {
      const pocket = annotations.pockets[f.target | 0];
      if (pocket) await viewer.focusOnResidues(pocket.residues);
    } else if (f.type === "all") {
      await viewer.resetCamera();
    }
  }
}

// Lifecycle observer — auto-tears-down the floating canvas when the
// placeholder has been gone for >300 ms with no replacement. Installed
// the first time any viewer mounts in this tab; runs for the rest of
// the tab's lifetime. Doesn't depend on any Python-side cleanup call,
// so it survives missed call sites / component-caching quirks / rerun
// races. 300 ms absorbs the brief detach/remount during a normal
// same-page rerun.
function installLifecycleObserver() {
  if (W.__molstarLifecycleObserver) return;
  let pendingTeardown = null;
  const teardown = () => {
    pendingTeardown = null;
    const ph = W.__molstarPlaceholder;
    if (ph && ph.isConnected) return;     // placeholder came back
    W.__molstarPlaceholder = null;
    if (W.__MOL_CACHE) {
      for (const [, v] of W.__MOL_CACHE) {
        try { v.viewer && v.viewer.dispose && v.viewer.dispose(); } catch (_) {}
        try { v.host && v.host.remove(); } catch (_) {}
      }
      W.__MOL_CACHE.clear();
    }
    if (W.__OVERLAY_STATES) W.__OVERLAY_STATES.clear();
    document.querySelectorAll('[data-molstar-floating-host="1"]').forEach(h => h.remove());
    document.querySelectorAll('[data-molstar-overlay-host="1"]').forEach(h => h.remove());
  };
  const check = () => {
    const ph = W.__molstarPlaceholder;
    if (ph && ph.isConnected) {
      if (pendingTeardown) { clearTimeout(pendingTeardown); pendingTeardown = null; }
    } else if (!pendingTeardown) {
      pendingTeardown = setTimeout(teardown, 300);
    }
  };
  const observer = new MutationObserver(check);
  observer.observe(document.body, { childList: true, subtree: true });
  W.__molstarLifecycleObserver = observer;
}

export default async function (component) {
  const { data, parentElement, setStateValue, setTriggerValue } = component;
  const placeholder = parentElement.querySelector("#molstar-root");
  if (!placeholder || !data || !data.structure_url) return;
  const url = data.structure_url;

  // First-mount setup: install the global lifecycle observer that
  // tears down the floating canvas when the placeholder goes away
  // without replacement (e.g., navigate to a session with no viewer).
  installLifecycleObserver();

  // Stash for the realign loop — it can't reliably find the placeholder
  // via document.querySelector because Streamlit's component v2 mount
  // context isn't always document-root-reachable. The script-set ref
  // is updated on every mount, so realign always points at the live
  // placeholder.
  W.__molstarPlaceholder = placeholder;
  // At first-mount the placeholder has been inserted but Streamlit
  // hasn't run layout yet (rect = 0×0). Schedule realign across a few
  // frames so we catch the moment it gets real dimensions; subsequent
  // layout shifts are handled by the ResizeObserver below.
  if (W.__molstarRealign) {
    W.__molstarRealign();
    requestAnimationFrame(() => W.__molstarRealign && W.__molstarRealign());
    setTimeout(() => W.__molstarRealign && W.__molstarRealign(), 50);
    setTimeout(() => W.__molstarRealign && W.__molstarRealign(), 250);
  }

  // Per-placeholder ResizeObserver so layout shifts within the
  // Streamlit column (panel toggles, sidebar collapse, etc.) keep
  // both floating hosts aligned.
  if (!placeholder.dataset.molstarObserverInstalled) {
    placeholder.dataset.molstarObserverInstalled = "1";
    try {
      new ResizeObserver(() => W.__molstarRealign && W.__molstarRealign())
        .observe(placeholder);
    } catch (_) {}
  }

  const overlay = getOrCreateOverlayHost();
  const cachedState = W.__OVERLAY_STATES.get(url);

  // If this URL is already fully loaded in the cache, drop the
  // overlay immediately (no flash) — the canvas under it is already
  // showing the rendered structure. If it's still loading or errored,
  // keep the overlay visible.
  if (cachedState === "loaded") {
    hideOverlay(overlay);
  } else if (cachedState === "error") {
    setOverlayError(overlay, "Failed to load structure — see console.");
    alignTo(overlay, placeholder);
  } else {
    showOverlay(overlay);
    alignTo(overlay, placeholder);
  }

  const t0 = performance.now();
  await loadBridge();

  // Look up the floating host + viewer for this URL. First time → create.
  const info = getOrCreateFloatingHost(url);
  alignTo(info.host, placeholder);

  // Concurrent re-mounts race through this function. Without a shared
  // promise, each one would call MolViewer.create + loadStructure on
  // the same host (multiple parallel 358 MB downloads + duplicate Mol*
  // instances on one canvas). Stash the in-flight creation on `info`
  // so subsequent mounts await the same promise.
  if (!info.viewer && !info.creating) {
    W.__OVERLAY_STATES.set(url, "loading");
    showOverlay(overlay);
    alignTo(overlay, placeholder);
    info.creating = (async () => {
      const v = await window.molstarBridge.MolViewer.create(info.host, {
        layoutIsExpanded: false,
        layoutShowControls: false,
        layoutShowRemoteState: false,
        layoutShowSequence: true,
        layoutShowLog: false,
        layoutShowLeftPanel: false,
        viewportShowExpand: true,
        viewportShowControls: true,
        viewportShowSettings: false,
        viewportShowSelectionMode: true,
        viewportShowAnimation: true,
      });
      info.host.addEventListener("click", () => {
        setTriggerValue("clicked", Date.now());
      });
      v.onResidueClick((residueId) => {
        setTriggerValue("residue_clicked", residueId);
      });
      setStateValue("ready_ms", Math.round(performance.now() - t0));

      const tLoad = performance.now();
      await v.loadStructure(url, data.structure_format || "pdb");
      setStateValue("loaded_ms", Math.round(performance.now() - tLoad));
      return v;
    })();
  }
  if (info.creating && !info.viewer) {
    try {
      info.viewer = await info.creating;
      W.__OVERLAY_STATES.set(url, "loaded");
      hideOverlay(overlay);
    } catch (err) {
      info.creating = null;  // allow retry on next mount
      W.__OVERLAY_STATES.set(url, "error");
      setStateValue("load_error", String(err));
      setOverlayError(overlay, "Failed to load structure — see console.");
      return;
    }
  }
  const viewer = info.viewer;
  if (!viewer) return;

  // Apply annotations (idempotent — fingerprint stored on the
  // persistent floating host, so re-mounts skip the re-apply cycle).
  try {
    await applyAnnotations(viewer, data.annotations, info.host, setTriggerValue);
    setStateValue("annotations_applied", info.host.dataset.lastAnnotations || "");
  } catch (err) {
    console.warn("applyAnnotations failed:", err);
  }

  // Frame navigation (also fingerprinted on the persistent host).
  if (typeof data.current_model === "number") {
    const targetIdx = Math.max(0, (data.current_model | 0) - 1);
    if (info.host.dataset.lastModelIdx !== String(targetIdx)) {
      try {
        await viewer.setCurrentModel(targetIdx);
        info.host.dataset.lastModelIdx = String(targetIdx);
      } catch (frameErr) {
        console.warn("frame switch failed:", frameErr);
      }
    }
  }
}
"""


_MOLSTAR = st.components.v2.component(
    "molstar_viewer",
    html=_HTML,
    js=_JS,
)


# Persistent-viewer cleanup is handled inside the molstar_viewer
# component itself — see ``installLifecycleObserver`` in _JS above.
# A MutationObserver watches document.body and tears down the floating
# canvas whenever the #molstar-root placeholder is gone for >300 ms
# without a replacement (e.g., navigate from a viewer-bearing session
# to landing / a viewer-less new session). No Python-side cleanup
# call is needed; the observer handles it automatically.


def molstar_viewer(
    structure_url: str | None = None,
    *,
    structure_format: str = "pdb",
    annotations: dict | None = None,
    current_model: int | None = None,
    key: str | None = None,
    height: int = 460,
    on_clicked_change: Callable[[], None] | None = None,
    on_ready_ms_change: Callable[[], None] | None = None,
    on_loaded_ms_change: Callable[[], None] | None = None,
    on_load_error_change: Callable[[], None] | None = None,
    on_annotations_applied_change: Callable[[], None] | None = None,
    on_residue_clicked_change: Callable[[], None] | None = None,
):
    """Render a Mol* viewer driven by the custom bridge.

    Args mirror the pre-B10 API; the JS underneath now goes through
    ``window.molstarBridge.MolViewer`` instead of Mol*'s minimal UMD.
    """
    return _MOLSTAR(
        data={
            "structure_url": structure_url,
            "structure_format": structure_format,
            "annotations": annotations or {},
            "current_model": current_model,
            "height": height,
        },
        key=key,
        height=height,
        isolate_styles=False,
        on_clicked_change=on_clicked_change or (lambda: None),
        on_ready_ms_change=on_ready_ms_change or (lambda: None),
        on_loaded_ms_change=on_loaded_ms_change or (lambda: None),
        on_load_error_change=on_load_error_change or (lambda: None),
        on_annotations_applied_change=on_annotations_applied_change or (lambda: None),
        on_residue_clicked_change=on_residue_clicked_change or (lambda: None),
    )
