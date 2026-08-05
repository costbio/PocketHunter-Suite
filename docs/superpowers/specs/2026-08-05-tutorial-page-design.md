# Tutorial page — design

Date: 2026-08-05
Branch: `feat/tutorial-page` (based on `redesign`, the branch production runs)

## Goal

Ship a full documentation page for PocketHunter Suite, reachable from the
`TUTORIAL` button in the masthead, that takes a new user from "I have an MD
trajectory" to "I have docked poses I can download".

The reference point is `https://grinn.bio-cloud.site/tutorial#welcome` — a
scrollable, sectioned, anchor-addressable documentation page. We match its
*form*, not its implementation: gRINN is a Dash app, this suite is Streamlit.

## Non-goals

- **No driver.js guided tour.** gRINN's `/assets/tutorial.js` drives an
  interactive overlay tour, but it depends on a stable DOM with stable
  element ids. Streamlit rebuilds its DOM on every rerun and exposes no
  durable hooks beyond `st-key-*` container classes, so the same technique
  would be fragile and expensive to maintain here. Revisit separately if
  wanted.
- **No new runtime dependency.** Nothing is added to `requirements.txt` and
  nothing new runs inside the containers.
- **No clean `/tutorial` URL in this change.** See "Future work".

## Background: what the reference page actually does

Fetching gRINN's page shows two independent mechanisms, often conflated:

| Asset | Role |
| --- | --- |
| `/tutorial` route | A Dash-rendered documentation page. |
| `/assets/doc-anchors.js` (2.2 KB) | Walks headings inside `.doc-content-card`, slugifies `textContent` into `id`s, re-runs on a debounced `MutationObserver`, and smooth-scrolls on `hashchange`. |
| `/assets/tutorial.js` (18 KB) | A separate driver.js tour for `/`, `/queue`, `/monitor`, configured by per-page JSON. Not part of `/tutorial`. |

`doc-anchors.js` exists only because Dash renders content client-side, so
heading ids cannot be known until React has painted. Generating the page
statically removes that entire problem: ids are in the HTML, anchors work on
first paint, and there is no observer to keep alive.

## Architecture

Markdown source compiled to a self-contained static page, served by
Streamlit's built-in static file server.

```
docs/tutorial/tutorial.md      ──┐
docs/tutorial/template.html    ──┤── scripts/build_tutorial.py ──> static/tutorial/index.html
docs/tutorial/img/*.png        ──┘                                 static/tutorial/img/*.png
```

`.streamlit/config.toml` already sets `enableStaticServing = true`, so
`static/` is published under `/app/static/`. The page's production URL is:

```
https://pockethunter.bio-cloud.site/app/static/tutorial/index.html
```

`docker-compose.yml` bind-mounts the repository into the streamlit container
(`- .:/app`), so a deployed file is served immediately — no image rebuild, no
container restart.

### Build script

`scripts/build_tutorial.py` uses `python-markdown` with the `toc`, `tables`,
`fenced_code` and `attr_list` extensions. The `toc` extension both assigns
heading ids and produces the table-of-contents HTML, so slug generation and
navigation stay derived from one source and cannot drift apart. The script
substitutes the rendered body, the TOC, and a build timestamp into
`template.html`.

`python-markdown` is a **build-time** dependency only. It runs on the author's
machine, never in a container, and is recorded in
`docs/tutorial/requirements-build.txt` rather than `requirements.txt`.

### Page structure

- Masthead strip matching the app: `POCKETHUNTER/SUITE — TUTORIAL`, version
  chip, link back to `/`.
- Sticky left table of contents, generated.
- Content column, max-width capped for line length.
- `scroll-margin-top` on headings so anchor jumps do not hide a heading under
  the sticky masthead.
- Styling matches the app's brutalist theme from `.streamlit/config.toml`:
  JetBrains Mono, `#000` on `#fff`, `#d4ff00` accent, zero border radius, hard
  borders, no shadows. Theme values are declared once as CSS custom properties
  in `template.html`.

### Intro block ("Figure 1" pattern)

The page opens with a scope block rather than a marketing hero, following the
convention of the tools this audience already reads:

1. One-sentence statement of what the tool does.
2. An inline SVG pipeline schematic — `UPLOAD → FIND POCKETS → CLUSTER → DOCK`
   — where **each stage is an `<a>` linking to that section's anchor**, so the
   schematic doubles as navigation.
3. A `FOR YOU IF` / `NOT FOR YOU IF` box, three concrete bullets each. The
   negative criteria are load-bearing: they let a reader rule the tool out in
   seconds, which is more useful and more credible than persuasion.
4. The worked example named up front with its measured runtime, and the list of
   concrete outputs the reader ends up with.

Inline SVG rather than a raster image: a few KB, sharp at any scale, inherits
the theme's CSS custom properties, and stays accurate as the UI evolves. The
schematic describes *what the pipeline is* and ages slowly; screenshots
describe *what the UI looks like* and age quickly, so screenshots belong in the
step sections, not the intro.

## Content outline

Written in English.

1. **Welcome** — what it does, who it is for, the intro block above.
2. **Before you start** — `topology.pdb` + `trajectory.xtc`, accepted formats
   (`.pdb` or `.gro` topology), size limits, the bundled `examples/trypsin`
   and `examples/tem1` datasets.
3. **Sessions** — the `?s=` URL, what persists, expiry, how to return to work.
4. **Step 1 · Find pockets** — p2rank, the `Min probability` slider
   (0.0–1.0, step 0.05), confidence filtering, reading the pocket table.
5. **Step 2 · Cluster** — method selection, hierarchy depth, choosing
   representatives.
6. **Step 3 · Dock** — ligand upload, `Number of poses` (1–50), `pH` (4.0–10.0,
   default 7.4), exhaustiveness, what SMINA is scoring.
7. **Reading the results** — the Mol\* viewer, frame slider, downloads.
8. **Parameter reference** — one table, every user-facing parameter with range,
   default, and effect.
9. **Limits and quotas** — per-session disk quota, per-pool concurrency, per-IP
   session creation caps, CAPTCHA.
10. **Troubleshooting / FAQ** — the failure modes the app actually surfaces.
11. **Citing** — PocketHunter, SMINA, p2rank, Mol\*.

### Source grounding

Every parameter name, range, default and limit is read from the **deployed**
installation on the host reachable as `ssh pockethunter`, not from memory or
from the GitHub default branch (which is `main` and is *not* what production
runs):

- `panels/find_pockets.py`, `panels/cluster.py`, `panels/docking.py` — exact
  widget labels and ranges.
- `settings.py`, `config.py`, `.env.example` — limits and quotas.
- `tasks.py` — pipeline stage order and failure paths.
- `analysis_app.py`, `landing.py` — session lifecycle and navigation.

Supplemented by `costbio/PocketHunter` (the detection algorithm) and
`costbio/PocketHunter-Suite` (README, `docs/deployment.md`, `CLAUDE.md`).

A claim that cannot be traced to one of those sources does not go in the page.

## Prose quality gate

The page must not read as machine-generated. Three MCP servers are installed
into the project's `.mcp.json` (not the user's global config):

| Server | Mode | Use |
| --- | --- | --- |
| `lmscan-mcp` | Offline, ~0.1 s, per-paragraph | Primary loop. Run `scan_mixed_text` on every draft section. |
| `dechecker-mcp` | Scrapes dechecker.ai via Chromium, ~1000 char cap | Final spot-check, 2–3 representative sections. |
| `zerogpt-mcp` | Scrapes zerogpt.com via Chromium, ~10–15 s | Final spot-check, same sections. |

Loop: draft a section → `scan_mixed_text` → rewrite paragraphs that score high
→ rescan.

Threshold: no paragraph above 0.5 on lmscan, and no section returning an
"AI-generated" verdict.

Two caveats shape how the results are read. lmscan's own documentation notes
that academic and technical writing produces 5–15 % false positives, so a
nonzero score is expected and chasing zero is not the objective. And the thing
that actually moves the score is concrete, checkable detail — real file paths,
real ranges, real runtimes taken from the deployed system — which is the same
discipline that makes the page correct. The detectors verify that discipline;
they do not substitute for it.

## Masthead button

`landing.py` currently renders `st.button("TUTORIAL", key="nav_tutorial")`,
which opens `_tutorial_dialog()` — a placeholder reading "Tutorial coming
soon."

Change: replace it with `st.link_button` pointing at the tutorial URL, opening
in a new tab, and delete `_tutorial_dialog()`.

`st.link_button` renders an `<a>`, but `main.py`'s masthead CSS targets
`.st-key-nav_tutorial button`. The selectors are widened to `button, a` so the
brutalist button styling still applies. `tests/test_masthead.py` gets a case
asserting the tutorial URL is present and the placeholder dialog is gone.

Verified precondition: `landing.py` and `main.py` on the deployed host are
byte-identical to `origin/redesign`, so this patch applies cleanly there.

## Screenshots

Captured with Playwright against the live site using `examples/trypsin`,
driven from the author's machine, so the images match the deployed UI exactly.
Stored under `docs/tutorial/img/`, copied to `static/tutorial/img/` by the
build.

This creates a real session and real worker jobs on production. The trypsin
dataset is small and the run is short, but it is not free: it consumes pool
capacity and leaves session data behind. If Playwright capture proves
unreliable, the fallback is manual capture by the maintainer.

## Preview and deployment

**Nothing reaches production without explicit approval from the maintainer.**
This constraint is structural, not procedural: because `static/` is bind-mounted
into the running container, writing a file there *is* deploying it. Therefore
no part of the authoring workflow touches the deployed host.

Authoring and preview happen entirely in the local clone at
`~/workspace/ph-suite-tutorial`:

```bash
python scripts/build_tutorial.py
python -m http.server 8080 --directory static/tutorial
```

The build output is a self-contained static page, so local preview is a
faithful rendering of what production will serve — no Streamlit needed.

On approval, deployment is additive and does not disturb the deployed tree:

1. `rsync` `static/tutorial/` to the host. This adds a new directory and
   modifies nothing that exists.
2. Apply the `landing.py` + `main.py` masthead patch.

Deployment deliberately does **not** use `git pull` on the host. The deployed
checkout is on `redesign` at a commit that is not on GitHub, is three commits
behind `origin/redesign`, and carries uncommitted local modifications to
`tasks.py`, `docker-entrypoint.sh`, `streamlit.Dockerfile` and
`streamlit_shell_css.py`. Pulling would entangle this change with resolving
that drift. The drift is a real issue worth fixing, but it is a separate piece
of work and this change is designed not to depend on it.

## Testing

- **Anchor integrity** — every `href="#…"` in the generated TOC resolves to an
  `id` in the document, and every heading has an id. A broken anchor fails
  silently in a browser, so this is the check most worth automating.
- **Build determinism** — building twice from unchanged sources produces
  identical output apart from the timestamp.
- **Masthead** — `tests/test_masthead.py` covers the button change.
- **Manual** — load the built page, click every schematic stage and TOC entry,
  confirm each lands on the right section; check narrow-viewport layout.

## Risks

- **Screenshot staleness.** Screenshots drift as the UI changes. Mitigated by
  keeping structural explanation in the SVG schematic and using screenshots
  only where a visual is genuinely needed.
- **Detector flakiness.** `dechecker-mcp` and `zerogpt-mcp` scrape live
  websites and break when those sites change markup. They are used only as a
  final spot-check, so a failure delays nothing; `lmscan` is offline and
  carries the loop.
- **Production side effects from capture.** Bounded by using the small trypsin
  dataset.

## Future work

A clean `https://pockethunter.bio-cloud.site/tutorial` URL, symmetric with
gRINN. TLS termination and reverse proxying happen on the droplet at
`142.93.128.72` (`headscale.bio-cloud.site`), not on the application host — the
app host reaches the internet through `autossh-pockethunter.service`, and its
`Caddyfile.example` is a template that is not in use there. Adding the clean URL
means a rewrite rule on the droplet from `/tutorial` to the static path, which
keeps the content in this repository as the single source. It requires
administrative access to the droplet, which is not yet confirmed, and it is
purely additive, so it is deferred rather than blocking.
