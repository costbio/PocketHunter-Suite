# Mol* + Streamlit CCv2 spike (B1) — findings

**Verdict: yellow-leaning-green. Proceed to B2.** The inline + CDN approach
works for everything we can prove in a headless test environment; the only
remaining unknown (interactive WebGL rendering) needs a real browser, which
is the user's actual deployment target.

## What we built

* `components/molstar_viewer.py` — inline CCv2 component. Mol* is loaded
  from jsDelivr CDN at runtime (no Node toolchain, no bundling step).
* `spike_molstar.py` — hidden spike page at `/?spike=molstar` rendering the
  viewer against a small reference structure (RCSB `1CBS`, retinoic acid
  receptor).
* `main.py` picks up the `spike=molstar` query param and renders the spike
  page instead of the normal app flow.

## What's proven

| Question | Answer |
|---|---|
| Does `st.components.v2.component(...)` exist in our Streamlit 1.52.2? | **Yes.** Note: `isolate_styles` lives on the mount callable, not on `component()`. |
| Does CCv2's bidi loop wire up? | **Yes.** `setStateValue` / `setTriggerValue` calls reach Python; `data=` round-trips to the JS default export. |
| Can we load Mol* from a public CDN at runtime? | **Yes.** jsDelivr serves Mol* 4.7.0 — CSS (76 KB) and the UMD viewer bundle (5.0 MB raw / **1.39 MB gzipped**, ~28% compression ratio). Cache-immutable for 1 year. |
| Does the script-tag injection pattern work for the UMD bundle? | **Yes.** `script.onload` fires; `window.molstar.Viewer.create(...)` is reachable. |

## What's NOT proven yet

| Question | Why | Plan |
|---|---|---|
| Does Mol* render an interactive structure end-to-end? | Headless Chromium fails WebGL initialization (`Could not create a WebGL rendering context`). SwiftShader flags + `--ignore-gpu-blocklist` didn't fix it on WSL. | Verify manually in a real browser when the user can poke at `/?spike=molstar`. Headed Playwright via xvfb is an option for CI later. |
| Click-event roundtrip latency. | Same as above — viewer didn't render so we couldn't click it. | Re-measure in B5 (annotation API) against a real browser. |
| Mol*'s phone-home to `localhost:9000`. | The default Mol* config requests `http://localhost:9000/v2/list_entries/...` for ligand chemical-component data. Logged a `net::ERR_CONNECTION_REFUSED`. | Configure Mol* with `pdbProvider` / `emdbProvider` options to disable or proxy. Address in B5 (annotation API config). |

## Bundle / load numbers

```
Mol* JS  (jsDelivr 4.7.0)
  raw bytes:       4,960,455  (4.73 MiB)
  gzipped bytes:   1,388,720  (1.32 MiB)
  cache-control:   public, max-age=31536000, immutable

Mol* CSS (jsDelivr 4.7.0)
  raw bytes:          75,662  (74 KiB)
  gzipped bytes:    (smaller, not measured — text)
  cache-control:   public, max-age=31536000, immutable
```

First-visit wire transfer for the viewer assets: **~1.4 MB**. Every
subsequent visit hits the browser cache.

## Decisions this informs

* **No Node toolchain in Phase B (yet).** Inline + CDN is sufficient for the
  forseeable Phase B work. We graduate to a packaged component (with bundled
  Mol*) only if (a) jsDelivr availability becomes a concern in production or
  (b) we need ESM imports that the UMD bundle doesn't expose.
* **B2 trajectory format stays at BCIF.** Mol* accepts BCIF natively
  (`loadStructureFromUrl(url, "bcif")` shape). The viewer's frame slider works
  on the multi-model assembly without our involvement.
* **isolate_styles = False** for the viewer component. Mol*'s CSS is loaded
  into `document.head`; the viewer lives in the light DOM. Other Streamlit
  components can keep using the shadow-DOM default.
* **Cache-busting in B5.** When we wire `set_annotations`, we'll need to
  detect "structure changed" vs "annotations changed" so we don't reload the
  bundle. The B1 component uses `root.dataset.lastPdbUrl` for this — extend
  the pattern in B5.

## Known follow-ups (file in B5)

1. Disable or configure Mol*'s `localhost:9000` phone-home for chemical
   component dictionary (`Viewer.create(root, {pdbProvider: "rcsb"})` or
   similar — check Mol* 4.7 config docs).
2. Wire the annotation API: `pockets`, `clusters`, `ligand_pose`, `focus`.
3. Plumb the `clicked` trigger into the right-pane panels so clicking a
   residue selects it in the active panel.
4. Test viewer rendering against a real browser via headed Playwright or
   manual smoke (a screenshot script in `/tmp/` that hits `/?spike=molstar`
   in headed mode).

## Tested against

* Streamlit 1.52.2
* Mol* 4.7.0 (UMD, jsDelivr)
* Chromium (Playwright bundled) — headless mode
* Linux / WSL2
