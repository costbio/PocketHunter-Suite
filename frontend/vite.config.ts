import { defineConfig } from "vite";

// Build an IIFE bundle that attaches `window.molstarBridge`. The
// runtime side (components/molstar_viewer.py) loads this single file
// from /app/static/js/molstar-bridge.js — no module loader needed.
export default defineConfig({
  // Mol*'s code references ``process.env.NODE_ENV`` (and a few other
  // Node-only globals) for dev/prod branching. The browser has no
  // ``process`` global, so we stub it at bundle time. Vite's
  // ``define`` substitutes the literal strings at compile time.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
    "process.env": "{}",
    process: "{}",
  },
  build: {
    target: "es2020",
    lib: {
      entry: "src/index.ts",
      name: "molstarBridge",
      formats: ["iife"],
      fileName: () => "molstar-bridge.iife.js",
    },
    rollupOptions: {
      output: {
        // Inline CSS / assets directly into the bundle so we don't
        // generate extra files.
        inlineDynamicImports: true,
      },
    },
    // ~5 MB bundle is fine for a one-time load + browser cache.
    chunkSizeWarningLimit: 8000,
    sourcemap: false,
  },
});
