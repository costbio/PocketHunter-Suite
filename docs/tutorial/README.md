# The documentation pages

Everything editable for the two hosted documentation pages lives in this
directory. They share one template, one build and one stylesheet:

| Page | Source | Served at |
| --- | --- | --- |
| Tutorial — the guided walkthrough | `tutorial.md` | `/app/static/tutorial/index.html` |
| Help — how to read the results | `help.md` | `/app/static/help/index.html` |

```
tutorial.md              the walkthrough — stage by stage, with screenshots
help.md                  the reference — what the numbers mean, licence, privacy
template.html            page shell: theme CSS, masthead, TOC slot
img/                     screenshots (tutorial only)
build.py                 compiles both pages; PAGES at the top is the list
capture_screenshots.py   drives the live service with Playwright to re-take img/
requirements-build.txt   build-time dependency (markdown), not a runtime one
```

Which page does a given fact belong on? The tutorial answers "how do I do this";
the help page answers "what does this output mean". A control's range and default
goes in the tutorial's parameter reference. A column's interpretation goes in
help. When in doubt, prefer help for anything a reader needs *after* a job
finishes — NAR requires the help page specifically to carry result interpretation.

Related files outside this directory that cannot move:

- `tests/test_tutorial_build.py`, `tests/test_help_build.py` — CI runs
  `pytest tests/`, so a test moved out of that directory silently stops running.
- `static/tutorial/`, `static/help/` — the build output. Streamlit serves
  `static/` under `/app/static/` (`enableStaticServing` in
  `.streamlit/config.toml`), which is how the pages reach a browser at all.
  Both are unignored in `.gitignore`; `static/*/` would otherwise swallow them.
- `static/fonts/` — the self-hosted webfont both pages link. No page may fetch
  anything from a third-party host; `tests/test_no_third_party_assets.py`
  enforces that by scanning for external subresources.

## Editing

```bash
$EDITOR docs/tutorial/help.md        # or tutorial.md
python docs/tutorial/build.py        # builds BOTH pages
python -m pytest tests/test_tutorial_build.py tests/test_help_build.py -q
```

**Never edit `static/tutorial/index.html` or `static/help/index.html`.** They are
generated, and the next build overwrites them. Each page has a staleness test that
re-renders the source and compares byte-for-byte, so a forgotten rebuild is caught
rather than shipped.

## The demo session link

The help page links a live, view-only session as its "sample output that performs
interactively in the same way as real output" — a NAR requirement that screenshots
do not satisfy. That session is **pinned** (`sessions.pinned`), which exempts it
from all three cleanup sweeps; see `db/sessions.pinned_job_legacy_ids`.

If you ever replace it, pin the new one *before* publishing the link, and never
publish the `?edit=` half. `test_the_demo_link_is_view_only` fails the build if an
edit token appears in a published URL.

Both the source and the built page are committed. That is deliberate: it makes the
rendered result reviewable in a diff, and it means a deploy is a file copy rather
than a build step on the server.

## Four things that will bite you

1. **Do not rename `Upload your files`, `Find pockets`, `Cluster` or `Dock`.** The
   intro block's SVG schematic links to their generated slugs.
   `test_every_schematic_stage_links_to_a_real_section` fails if you do.
2. **Write `Mol\*`, never `Mol*`.** An unescaped asterisk opens a markdown emphasis
   span, which renders the product name as "Mol" and italicises the wrong clauses.
   `test_no_stray_asterisks_in_rendered_prose` guards this.
3. **Keep the `{: #upload }` style markers.** They pin heading slugs that other
   parts of the page link to.
4. **Never write `{{ TOC }}`, `{{ CONTENT }}` or `{{ BUILT_AT }}` literally**, in
   `template.html` or in prose. `build.py` substitutes those tokens everywhere,
   including inside comments — this has already pasted a rendered table of contents
   into a CSS comment once.

## Prose quality

The page was written to read as human-written, and that was checked rather than
assumed. If you rewrite more than a sentence or two:

```bash
~/.venvs/ph-tutorial/bin/lmscan --file docs/tutorial/tutorial.md --mixed
```

Read the `Per-Paragraph Analysis` table at the end. No paragraph should exceed
50 %. Do **not** add `--format json` — it silently drops that table, so the check
would pass while measuring nothing.

When a paragraph scores high, the fix is a concrete fact replacing generic filler —
a real path, a real range, a real default read off the deployed service — not a
synonym swap. That is also what keeps the page correct, which is why the two pull
in the same direction.

## Screenshots

`capture_screenshots.py` drives the **live** service and creates a real session and
real worker jobs on a shared, quota-limited box. Use the bundled `examples/tem1`
dataset and nothing bigger. Streamlit replaces DOM nodes on every rerun, so
selectors go stale mid-interaction; wait on content appearing (or a loading
placeholder disappearing) rather than on a fixed sleep or on `networkidle`, which
never fires because Streamlit holds a WebSocket open.

Two selector traps, both of which have already broken a capture. Every results
panel renders a `Next: <stage> →` primary button alongside the stage's own submit
button, so filtering primary buttons by stage name matches two elements and
Playwright refuses the click — `stage_submit_button()` excludes the wizard button
for you. And anchoring a crop on a node matched by exact `textContent` is fragile,
because Streamlit's re-nesting can leave you holding a bare heading whose box stops
above the content you meant to show; prefer a structural anchor such as
`[data-testid="stColumn"]`.

`resume <session-url>` redoes Cluster and Dock against a session that already has a
Find Pockets run, which is the polite way to recapture the later stages. Those two
stages must stay in one browser connection: the staged-pocket bucket and the
ligand uploader live in per-connection Streamlit session state, not in the
database.

The Mol\* viewer does not mount in headless Chromium even with WebGL available, so
the results-viewer panel has no screenshot. Capture that one by hand if it is
wanted.

## Facts in the page

Every number, widget label and limit on the page was read off the deployed host
rather than this checkout, which has drifted from it. If you add a claim, check it
against what is actually running — and prefer naming a function over citing a line
number, since line numbers rot on the next change and readers of a hosted service
have no checkout to look in.
