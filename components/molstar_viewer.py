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
// Load our bundled Mol* bridge once. Subsequent renders reuse the
// already-loaded ``window.molstarBridge`` global. CSS is loaded once
// into document.head; JS is loaded once into the page.
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

// One MolViewer instance per host element across re-renders.
const VIEWERS = new WeakMap();

async function applyAnnotations(viewer, annotations, root, setTriggerValue) {
  annotations = annotations || {};
  const fingerprint = JSON.stringify({
    pockets: annotations.pockets || [],
    clusters: annotations.clusters || [],
    ligand_pose: annotations.ligand_pose || null,
    focus: annotations.focus || null,
  });
  if (root.dataset.lastAnnotations === fingerprint) return;
  root.dataset.lastAnnotations = fingerprint;

  // Pockets: clear + re-apply.
  await viewer.clearPocketSurfaces();
  for (const p of (annotations.pockets || [])) {
    await viewer.showPocketSurface(p);
  }

  // Clusters: clear + re-apply.
  await viewer.clearClusterOverpaints();
  for (const c of (annotations.clusters || [])) {
    await viewer.showClusterOverpaint(c);
  }

  // Ligand pose: update or clear.
  if (annotations.ligand_pose && annotations.ligand_pose.sdf) {
    await viewer.loadLigandPose(
      annotations.ligand_pose.sdf,
      annotations.ligand_pose.color || "#ffa500"
    );
  } else {
    await viewer.clearLigandPose();
  }

  // Focus.
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

export default async function (component) {
  const { data, parentElement, setStateValue, setTriggerValue } = component;
  const root = parentElement.querySelector("#molstar-root");
  if (!root) return;

  const t0 = performance.now();
  await loadBridge();

  let viewer = VIEWERS.get(root);
  if (!viewer) {
    viewer = await window.molstarBridge.MolViewer.create(root, {
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
    VIEWERS.set(root, viewer);

    // Surface any viewer click for compat with the old API.
    root.addEventListener("click", () => {
      setTriggerValue("clicked", Date.now());
    });

    // Residue-level click round-trip.
    viewer.onResidueClick((residueId) => {
      setTriggerValue("residue_clicked", residueId);
    });

    setStateValue("ready_ms", Math.round(performance.now() - t0));
  }

  // Structure load — only re-load if structure_url changed.
  if (data?.structure_url && data.structure_url !== root.dataset.lastStructureUrl) {
    const tStart = performance.now();
    root.dataset.lastStructureUrl = data.structure_url;
    delete root.dataset.lastAnnotations;
    delete root.dataset.lastModelIdx;
    try {
      await viewer.loadStructure(
        data.structure_url,
        data.structure_format || "pdb"
      );
      setStateValue("loaded_ms", Math.round(performance.now() - tStart));
    } catch (err) {
      setStateValue("load_error", String(err));
    }
  }

  // Apply annotations (idempotent — fingerprint check).
  if (root.dataset.lastStructureUrl) {
    try {
      await applyAnnotations(viewer, data?.annotations, root, setTriggerValue);
      setStateValue("annotations_applied", root.dataset.lastAnnotations || "");
    } catch (err) {
      console.warn("applyAnnotations failed:", err);
    }
  }

  // Frame navigation.
  if (root.dataset.lastStructureUrl && typeof data?.current_model === "number") {
    const targetIdx = Math.max(0, (data.current_model | 0) - 1);
    if (root.dataset.lastModelIdx !== String(targetIdx)) {
      try {
        await viewer.setCurrentModel(targetIdx);
        root.dataset.lastModelIdx = String(targetIdx);
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
