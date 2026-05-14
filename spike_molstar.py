"""B1 spike page — measures Mol* + Streamlit CCv2 viability.

Visit ``/?spike=molstar`` to render this page. Captures:

* Bundle download time (Mol* UMD ~5 MB from jsDelivr)
* Time-to-ready (Mol* viewer instantiated and visible)
* Time-to-structure (PDB rendered)
* Click roundtrip latency (JS → Python via setTriggerValue)

Findings get written to ``docs/molstar_spike_findings.md`` in the
follow-up commit.
"""
from __future__ import annotations

import streamlit as st

from components.molstar_viewer import molstar_viewer


# Small public structure: retinoic acid receptor (1.5 kB PDB).
DEFAULT_PDB = "https://files.rcsb.org/download/1CBS.pdb"


def render_spike_page() -> None:
    st.markdown(
        """
<div class="bh" style="margin-top: 4px;">
    <div class="bh-row">
        <span class="bh-title">B1 SPIKE / MOL* + CCv2</span>
        <span class="bh-version">[Phase B]</span>
    </div>
    <div class="bh-rule"></div>
    <div class="bh-stages">
        Probe — does Mol* embed cleanly inside a Streamlit Components v2 component?
    </div>
</div>
""",
        unsafe_allow_html=True,
    )

    structure_url = st.text_input(
        "Structure URL",
        value=DEFAULT_PDB,
        help="Any publicly fetch-able PDB. Default: retinoic acid receptor (small).",
    )

    result = molstar_viewer(
        structure_url,
        structure_format="pdb",
        key="spike_viewer",
        height=620,
    )

    st.markdown("### Measurements")
    c1, c2, c3 = st.columns(3)
    c1.metric("Mol* ready (ms)", result.ready_ms if result.ready_ms is not None else "—")
    c2.metric("Structure loaded (ms)", result.loaded_ms if result.loaded_ms is not None else "—")
    c3.metric("Last click (epoch ms)", result.clicked if result.clicked else "—")

    if result.load_error:
        st.error(f"Mol* failed to load the structure: {result.load_error}")

    with st.expander("Notes for findings doc"):
        st.markdown(
            """
What this spike actually proves:

1. **Mol* embeds.** Viewer renders the structure interactively.
2. **CCv2 wires up.** Click events bubble back to Python via
   ``setTriggerValue``; ready/load timings persist via ``setStateValue``.
3. **No build step needed yet.** Mol* loaded from jsDelivr UMD at
   ~5 MB (one-time). Cached by the browser for subsequent visits.

Decisions this informs:

* If load+ready < 3 s → **green** — proceed with inline CDN approach.
* If 3-8 s → **yellow** — graduate to packaged + bundled in a later B
  commit; the inline scaffold can stay as a fallback.
* If > 8 s or doesn't load → **red** — pivot. Either bundle Mol* in
  Phase B from day one, or evaluate NGL.js as the viewer library.
"""
        )
