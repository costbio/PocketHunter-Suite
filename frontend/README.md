# Mol* bridge

A Vite-built IIFE bundle around [Mol*](https://molstar.org/) that
re-exposes the selection + representation APIs Mol*'s default
viewer-bundle UMD hides. The Streamlit-side
`components/molstar_viewer.py` component loads the built artefact
from `/app/static/js/molstar-bridge.js`.

## Build

```
cd frontend
npm install
npm run build      # writes ../static/js/molstar-bridge.js
```

Commit the rebuilt `../static/js/molstar-bridge.js` together with
your `src/` changes. Contributors who don't edit the bridge don't
need Node installed — the bundle is already in the repo.

## Why

The Mol* npm package ships a `viewer.js` UMD that exposes only
`Viewer`, `ViewerAutoPreset`, and version helpers on `window.molstar`.
Everything we need for programmatic residue selection
(`MolScriptBuilder`, `StructureSelection`, `StructureElement`,
`Color`, `StateTransforms`, `PluginCommands`) is bundled but private.
This bridge imports those internals at build time and wraps them in
a small `MolViewer` class that the Streamlit component drives.

See `src/index.ts` for the full API.
