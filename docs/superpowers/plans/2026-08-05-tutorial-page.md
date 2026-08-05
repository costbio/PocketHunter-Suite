# Tutorial Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a full English documentation page for PocketHunter Suite, built from markdown into a self-contained static page and reachable from the masthead `TUTORIAL` button.

**Architecture:** `docs/tutorial/tutorial.md` plus `docs/tutorial/template.html` are compiled by `scripts/build_tutorial.py` into `static/tutorial/index.html`. Streamlit already serves `static/` under `/app/static/` (`enableStaticServing = true`), and `docker-compose.yml` bind-mounts the repository into the container, so the built page needs no rebuild or restart. Heading ids and the table of contents both come from python-markdown's `toc` extension, so navigation cannot drift from the content.

**Tech Stack:** Python 3, `markdown` 3.8 (build-time only, already installed system-wide), `pytest` 8.4.2, Streamlit 1.58.0, Playwright (authoring-time only, in a throwaway venv).

## Global Constraints

- **Language:** the tutorial page is written in English. Commit messages and code comments in English.
- **Nothing is deployed without the maintainer's explicit approval.** Because `static/` is bind-mounted into the running container, writing there *is* deploying. No task before Task 10 touches the host `ssh pockethunter` in any way other than reading.
- **Nothing is pushed to GitHub** in this plan. All commits stay local on `feat/tutorial-page`.
- **No new runtime dependency.** `requirements.txt`, `worker-requirements.txt`, and every Dockerfile stay untouched.
- **Ground every factual claim in the deployed host**, read over `ssh pockethunter`, not in a local checkout and not from memory.
- **Only `main` of `costbio/PocketHunter` is deployed** (host bundled core is at `7287d23`). The seventeen commits on `feature/discrimination` — pharmacophore Layer 1/Layer 2 scoring, the `discriminate` subcommand, rdkit and prody — are **not** running and must not appear anywhere in the page.
- **Do not claim optimal clustering parameters.** In the deployed `PocketHunter/pockethunter.py`, DBSCAN fits on `df[cols_2cluster]` with `metric='hamming'` (line 333) while the selecting silhouette score is computed on the whole dataframe with default euclidean (line 339).
- **Neither repository has a LICENSE file.** The Citing section states this rather than implying terms.
- **Docking scores may be explained without qualification.** The `GetPartialCharge()` loop in `step4_docking.py` is a valid Open Babel idiom; charges verified present in the written PDBQT.
- **Prose gate:** no paragraph above 0.5 on lmscan, no section returning an "AI-generated" verdict.
- **Tutorial URL (relative, no env coupling):** `/app/static/tutorial/index.html`
- **Theme values, copied from `.streamlit/config.toml`:** background `#ffffff`, text `#000000`, secondary background `#f4f4f4`, accent `#d4ff00`, borders `2px solid #000000`, radius `0`, font `JetBrains Mono`, no shadows.

---

## File Structure

| Path | Responsibility |
| --- | --- |
| `scripts/build_tutorial.py` | Create. Pure render function plus a CLI. Knows markdown and templating; knows nothing about content. |
| `docs/tutorial/template.html` | Create. The page shell: theme CSS, masthead strip, TOC slot, content slot. |
| `docs/tutorial/tutorial.md` | Create. The prose. Single source of truth for content. |
| `docs/tutorial/img/` | Create. Screenshots, source-of-truth copies. |
| `docs/tutorial/requirements-build.txt` | Create. Build-time dependency pin, deliberately separate from `requirements.txt`. |
| `tests/test_tutorial_build.py` | Create. Anchor integrity and determinism. |
| `static/tutorial/index.html`, `static/tutorial/img/` | Build output, committed. |
| `.gitignore:31` | Modify. `static/*/` currently excludes the output; add a `!static/tutorial/` exception beside the existing `!static/js/`. |
| `landing.py:600-607, 735-738` | Modify. Replace the placeholder dialog with a link button. |
| `main.py:107-125` | Modify. Widen masthead button selectors to cover `<a>`. |
| `tests/test_masthead.py` | Modify. Cover the link button. |

---

## Task 1: Build pipeline

**Files:**
- Create: `scripts/build_tutorial.py`
- Create: `docs/tutorial/template.html`
- Create: `docs/tutorial/tutorial.md`
- Create: `docs/tutorial/requirements-build.txt`
- Create: `tests/test_tutorial_build.py`
- Modify: `.gitignore:31-34`

**Interfaces:**
- Consumes: nothing.
- Produces: `render(md_text: str, template_text: str, *, built_at: str) -> str` returning complete HTML; `main(argv: list[str] | None = None) -> int` writing `static/tutorial/index.html` and copying `docs/tutorial/img/` to `static/tutorial/img/`. Template placeholders are the literal strings `{{ TOC }}`, `{{ CONTENT }}`, `{{ BUILT_AT }}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tutorial_build.py`:

```python
"""Tests for scripts/build_tutorial.py.

A broken in-page anchor fails silently in a browser — the page simply
does not move — so anchor integrity is the property most worth pinning
down here.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_builder():
    path = REPO / "scripts" / "build_tutorial.py"
    spec = importlib.util.spec_from_file_location("build_tutorial", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TEMPLATE = """<html><body>
<nav>{{ TOC }}</nav>
<main>{{ CONTENT }}</main>
<footer>{{ BUILT_AT }}</footer>
</body></html>"""

MARKDOWN = """# Tutorial

## Welcome

Text.

## Before You Start

More text.
"""


class TestRender:
    def test_headings_get_ids(self):
        html = _load_builder().render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        assert 'id="welcome"' in html
        assert 'id="before-you-start"' in html

    def test_toc_is_substituted(self):
        html = _load_builder().render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        assert "{{ TOC }}" not in html
        assert "{{ CONTENT }}" not in html
        assert "{{ BUILT_AT }}" not in html
        assert 'href="#welcome"' in html

    def test_every_anchor_resolves_to_an_id(self):
        html = _load_builder().render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        ids = set(re.findall(r'id="([^"]+)"', html))
        hrefs = re.findall(r'href="#([^"]+)"', html)
        assert hrefs, "no in-page anchors found at all"
        assert [h for h in hrefs if h not in ids] == []

    def test_render_is_deterministic(self):
        builder = _load_builder()
        first = builder.render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        second = builder.render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        assert first == second


class TestShippedPage:
    """The committed page must satisfy the same invariant as a synthetic one."""

    def test_built_page_anchors_all_resolve(self):
        built = REPO / "static" / "tutorial" / "index.html"
        if not built.exists():
            import pytest
            pytest.skip("page not built yet")
        html = built.read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', html))
        hrefs = re.findall(r'href="#([^"]+)"', html)
        assert [h for h in hrefs if h not in ids] == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/workspace/ph-suite-tutorial && python -m pytest tests/test_tutorial_build.py -v`
Expected: FAIL — `FileNotFoundError` / `spec_from_file_location` returns nothing, because `scripts/build_tutorial.py` does not exist.

- [ ] **Step 3: Write the build script**

Create `scripts/build_tutorial.py`:

```python
#!/usr/bin/env python3
"""Compile docs/tutorial/tutorial.md into static/tutorial/index.html.

Streamlit serves ./static/ under /app/static/ (enableStaticServing in
.streamlit/config.toml), and docker-compose bind-mounts the repository
into the container, so the built page is served with no rebuild.

Heading ids and the table of contents both come from python-markdown's
`toc` extension. Deriving both from one pass is deliberate: a hand-kept
table of contents drifts, and a dead in-page anchor produces no error —
the browser just does not scroll — so the drift stays invisible.

`markdown` is a build-time dependency only. It runs here, on an author's
machine, and is never imported by the application or installed into any
container image.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import shutil
import sys

import markdown

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC_MD = REPO / "docs" / "tutorial" / "tutorial.md"
TEMPLATE = REPO / "docs" / "tutorial" / "template.html"
SRC_IMG = REPO / "docs" / "tutorial" / "img"
OUT_DIR = REPO / "static" / "tutorial"

EXTENSIONS = ["toc", "tables", "fenced_code", "attr_list", "sane_lists"]
EXTENSION_CONFIGS = {"toc": {"permalink": False, "toc_depth": "2-3"}}


def render(md_text: str, template_text: str, *, built_at: str) -> str:
    """Render markdown into the template. Pure: no filesystem access."""
    md = markdown.Markdown(extensions=EXTENSIONS,
                           extension_configs=EXTENSION_CONFIGS)
    content = md.convert(md_text)
    return (
        template_text
        .replace("{{ TOC }}", md.toc)
        .replace("{{ CONTENT }}", content)
        .replace("{{ BUILT_AT }}", built_at)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--built-at",
        default=_dt.date.today().isoformat(),
        help="Build date stamped into the page. Pin it to make builds "
             "byte-reproducible.",
    )
    args = parser.parse_args(argv)

    html = render(
        SRC_MD.read_text(encoding="utf-8"),
        TEMPLATE.read_text(encoding="utf-8"),
        built_at=args.built_at,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")

    out_img = OUT_DIR / "img"
    if out_img.exists():
        shutil.rmtree(out_img)
    if SRC_IMG.exists():
        shutil.copytree(SRC_IMG, out_img)

    print(f"wrote {OUT_DIR / 'index.html'} ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write the template**

Create `docs/tutorial/template.html`. Theme values are declared once as custom properties so a future theme change is a single edit:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tutorial — PocketHunter Suite</title>
<link rel="icon" href="/app/static/favicon.svg">
<style>
:root {
  --bg: #ffffff;
  --fg: #000000;
  --bg2: #f4f4f4;
  --accent: #d4ff00;
  --muted: #666666;
  --border: 2px solid #000000;
  --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  --masthead-h: 56px;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font-family: var(--mono); font-size: 14px; line-height: 1.65;
}
a { color: var(--fg); text-decoration: underline; }
a:hover { background: var(--accent); }

.masthead {
  position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; height: var(--masthead-h); padding: 0 18px;
  background: var(--bg); border-bottom: var(--border);
}
.masthead .brand { font-weight: 800; letter-spacing: 1px; text-decoration: none; }
.masthead .version { color: var(--muted); font-size: 0.85rem; }

.layout {
  display: grid; grid-template-columns: 260px minmax(0, 1fr);
  gap: 32px; max-width: 1200px; margin: 0 auto; padding: 24px 18px 80px;
}
.toc {
  position: sticky; align-self: start;
  top: calc(var(--masthead-h) + 24px);
  max-height: calc(100vh - var(--masthead-h) - 48px); overflow-y: auto;
  border: var(--border); background: var(--bg2); padding: 12px 14px;
}
.toc ul { list-style: none; margin: 0; padding-left: 12px; }
.toc > ul { padding-left: 0; }
.toc a { text-decoration: none; display: block; padding: 3px 0; }
.toc a:hover { background: var(--accent); }

.content { min-width: 0; }
.content h1 { font-size: 28px; font-weight: 800; }
.content h2 {
  font-size: 22px; font-weight: 700;
  margin-top: 48px; padding-top: 10px; border-top: var(--border);
}
.content h3 { font-size: 18px; font-weight: 700; margin-top: 32px; }
.content h1, .content h2, .content h3 {
  scroll-margin-top: calc(var(--masthead-h) + 16px);
}
.content img { max-width: 100%; border: var(--border); display: block; }
.content table { border-collapse: collapse; width: 100%; display: block; overflow-x: auto; }
.content th, .content td { border: 1px solid var(--fg); padding: 6px 10px; text-align: left; }
.content th { background: var(--bg2); font-weight: 700; }
.content pre {
  background: var(--bg2); border: var(--border);
  padding: 12px; overflow-x: auto; font-size: 13px;
}
.content code { background: var(--bg2); padding: 1px 4px; }
.content pre code { background: none; padding: 0; }
.content blockquote {
  margin: 20px 0; padding: 12px 16px;
  border: var(--border); border-left-width: 8px; background: var(--bg2);
}

footer {
  border-top: var(--border); padding: 16px 18px;
  color: var(--muted); font-size: 0.85rem; text-align: center;
}

@media (max-width: 860px) {
  .layout { grid-template-columns: 1fr; }
  .toc { position: static; max-height: none; }
}
</style>
</head>
<body>
<header class="masthead">
  <a class="brand" href="/">POCKETHUNTER/SUITE — TUTORIAL</a>
  <span class="version">[v.2.0]</span>
</header>
<div class="layout">
  <nav class="toc" aria-label="Table of contents">{{ TOC }}</nav>
  <main class="content">{{ CONTENT }}</main>
</div>
<footer>Built {{ BUILT_AT }} · <a href="/">Back to the app</a></footer>
</body>
</html>
```

- [ ] **Step 5: Write a minimal source document**

Create `docs/tutorial/tutorial.md` — placeholder prose is replaced in Tasks 4–6, but the file must render now so the pipeline is testable:

```markdown
# PocketHunter Suite tutorial

## Welcome

Placeholder. Replaced in Task 4.

## Before you start

Placeholder. Replaced in Task 4.
```

Create `docs/tutorial/requirements-build.txt`:

```
# Build-time only. Never installed into a container image and never
# imported by the application — see scripts/build_tutorial.py.
markdown>=3.8,<4
```

- [ ] **Step 6: Add the gitignore exception**

`static/*/` on line 31 excludes per-session runtime artefacts. The built page is neither runtime nor per-session, so it gets an exception beside the existing one for `static/js/`. Change lines 31-34 from:

```
static/*/
# But the bridge bundle (B10) lives here and IS source-tracked.
!static/js/
static/js/*.map
```

to:

```
static/*/
# But the bridge bundle (B10) lives here and IS source-tracked.
!static/js/
static/js/*.map
# So is the built tutorial page — a build artefact of docs/tutorial/,
# committed so the rendered result is reviewable in a diff.
!static/tutorial/
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_tutorial_build.py -v`
Expected: PASS, 4 passed and 1 skipped (`test_built_page_anchors_all_resolve` skips until the page is built).

- [ ] **Step 8: Build and confirm the page appears**

Run: `python scripts/build_tutorial.py --built-at 2026-08-05`
Expected: `wrote .../static/tutorial/index.html (N bytes)`

Run: `python -m pytest tests/test_tutorial_build.py -v`
Expected: PASS, 5 passed, 0 skipped.

Run: `git status --porcelain static/tutorial/`
Expected: `?? static/tutorial/` — confirms the gitignore exception works. An empty result means `static/*/` is still winning and Step 6 needs revisiting.

- [ ] **Step 9: Commit**

```bash
git add scripts/build_tutorial.py docs/tutorial/ tests/test_tutorial_build.py .gitignore static/tutorial/
git commit -m "feat: markdown-to-html build pipeline for the tutorial page"
```

---

## Task 2: Intro block

**Files:**
- Modify: `docs/tutorial/tutorial.md`
- Modify: `docs/tutorial/template.html` (intro block CSS)
- Modify: `tests/test_tutorial_build.py`

**Interfaces:**
- Consumes: `render()` from Task 1.
- Produces: the section ids `find-pockets`, `cluster`, `dock` that the schematic links to; Tasks 5 and 6 must keep those headings spelled so they slugify to exactly these.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tutorial_build.py`:

```python
class TestIntroBlock:
    """The schematic doubles as navigation, so its links are load-bearing."""

    def _built(self):
        built = REPO / "static" / "tutorial" / "index.html"
        if not built.exists():
            import pytest
            pytest.skip("page not built yet")
        return built.read_text(encoding="utf-8")

    def test_schematic_is_inline_svg(self):
        html = self._built()
        assert "<svg" in html
        assert 'class="schematic"' in html

    def test_every_schematic_stage_links_to_a_real_section(self):
        html = self._built()
        svg = html[html.index('class="schematic"'):]
        svg = svg[:svg.index("</svg>")]
        targets = re.findall(r'href="#([^"]+)"', svg)
        assert sorted(targets) == ["cluster", "dock", "find-pockets", "upload"]
        ids = set(re.findall(r'id="([^"]+)"', html))
        assert [t for t in targets if t not in ids] == []

    def test_scope_box_states_both_directions(self):
        html = self._built()
        assert "FOR YOU IF" in html
        assert "NOT FOR YOU IF" in html
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_tutorial_build.py::TestIntroBlock -v`
Expected: FAIL — `assert "<svg" in html`.

- [ ] **Step 3: Add intro-block CSS to the template**

Insert into `docs/tutorial/template.html` before the `footer {` rule:

```css
.schematic { width: 100%; height: auto; margin: 24px 0; border: var(--border); background: var(--bg2); }
.schematic .stage rect { fill: var(--bg); stroke: var(--fg); stroke-width: 2; }
.schematic .stage:hover rect { fill: var(--accent); }
.schematic text { font-family: var(--mono); font-size: 11px; fill: var(--fg); }
.schematic .stage-title { font-weight: 700; font-size: 12px; }
.schematic .arrow { stroke: var(--fg); stroke-width: 2; fill: none; }

.scope { border: var(--border); margin: 24px 0; }
.scope h3 {
  margin: 0; padding: 8px 14px; font-size: 13px; letter-spacing: 1px;
  background: var(--fg); color: var(--bg);
}
.scope h3.negative { background: var(--bg2); color: var(--fg); border-top: var(--border); }
.scope ul { margin: 0; padding: 12px 14px 14px 32px; }
.scope li { margin: 4px 0; }
.facts { border: var(--border); background: var(--bg2); padding: 12px 14px; margin: 24px 0; }
.facts dt { font-weight: 700; }
.facts dd { margin: 0 0 8px 0; }
```

- [ ] **Step 4: Add the intro block to the source document**

Replace the `## Welcome` section in `docs/tutorial/tutorial.md`. The schematic is hand-written SVG so each stage is a real link; `attr_list` and raw HTML pass through markdown untouched. Every stage `href` must match a heading id created in Tasks 4–6:

```markdown
## Welcome

Find transient, druggable pockets across a molecular-dynamics trajectory —
then dock ligands against them.

<svg class="schematic" viewBox="0 0 620 120" role="img"
     aria-label="Pipeline: upload, find pockets, cluster, dock">
  <a class="stage" href="#upload">
    <rect x="10" y="24" width="130" height="72"/>
    <text class="stage-title" x="24" y="50">UPLOAD</text>
    <text x="24" y="68">topology.pdb</text>
    <text x="24" y="84">trajectory.xtc</text>
  </a>
  <path class="arrow" d="M148 60 h20 m-6 -5 l6 5 l-6 5"/>
  <a class="stage" href="#find-pockets">
    <rect x="175" y="24" width="130" height="72"/>
    <text class="stage-title" x="189" y="50">FIND POCKETS</text>
    <text x="189" y="68">p2rank, per frame</text>
  </a>
  <path class="arrow" d="M313 60 h20 m-6 -5 l6 5 l-6 5"/>
  <a class="stage" href="#cluster">
    <rect x="340" y="24" width="130" height="72"/>
    <text class="stage-title" x="354" y="50">CLUSTER</text>
    <text x="354" y="68">group by residues</text>
  </a>
  <path class="arrow" d="M478 60 h20 m-6 -5 l6 5 l-6 5"/>
  <a class="stage" href="#dock">
    <rect x="505" y="24" width="105" height="72"/>
    <text class="stage-title" x="519" y="50">DOCK</text>
    <text x="519" y="68">SMINA</text>
  </a>
</svg>

Each stage above links to its section.

<div class="scope" markdown="1">
### FOR YOU IF

- You have an MD trajectory and want the pockets a single static structure would miss.
- You want those pockets ranked and grouped rather than one hit per frame.
- You want to dock ligands against the pockets you select.

### NOT FOR YOU IF {: .negative }

- You have one static structure — run p2rank directly instead.
- You need covalent docking, or docking into a membrane or nucleic-acid site.
- You need a guaranteed turnaround; this is a shared, quota-limited service.
</div>
```

Placeholder to be replaced in Task 6 once measured: the worked-example facts block. Add it now with the structure it will keep:

```markdown
<dl class="facts">
  <dt>Worked example</dt>
  <dd>MEASURED_EXAMPLE</dd>
  <dt>You end up with</dt>
  <dd>Ranked pockets per frame · cluster representatives · SMINA scores · downloadable poses</dd>
</dl>
```

- [ ] **Step 5: Add the anchor targets the schematic points at**

The schematic links to four ids that must exist. Add or confirm these headings in `docs/tutorial/tutorial.md`; their exact spelling is what produces the slug:

```markdown
## Upload your files

## Find pockets

## Cluster

## Dock
```

Verify the slugs by building and grepping, rather than assuming:

Run: `python scripts/build_tutorial.py --built-at 2026-08-05 && grep -o 'id="[a-z-]*"' static/tutorial/index.html | sort -u`
Expected: the list includes `id="upload"`, `id="find-pockets"`, `id="cluster"`, `id="dock"`. If `## Upload your files` slugifies to `upload-your-files` instead of `upload`, pin it explicitly with `attr_list`: `## Upload your files {: #upload }`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_tutorial_build.py -v`
Expected: PASS, all tests.

- [ ] **Step 7: Commit**

```bash
git add docs/tutorial/ tests/test_tutorial_build.py static/tutorial/
git commit -m "feat: figure-1 intro block with clickable pipeline schematic"
```

---

## Task 3: Authoring toolchain

**Files:**
- Create: `.mcp.json`
- Modify: `.gitignore` (ignore the authoring venv if placed in-tree; it is not)

**Interfaces:**
- Consumes: nothing.
- Produces: MCP tools `scan_text`, `scan_file`, `scan_mixed_text` (lmscan), `check_dechecker`, and the zerogpt checker, used as the prose gate in Tasks 4–6.

- [ ] **Step 1: Create the authoring venv outside the repository**

The repository must not gain a venv; these tools are authoring aids, not project dependencies.

```bash
python3 -m venv ~/.venvs/ph-tutorial
~/.venvs/ph-tutorial/bin/pip install --quiet --upgrade pip
~/.venvs/ph-tutorial/bin/pip install --quiet lmscan-mcp dechecker-mcp zerogpt-mcp
~/.venvs/ph-tutorial/bin/playwright install chromium
```

- [ ] **Step 2: Verify each server starts**

```bash
~/.venvs/ph-tutorial/bin/lmscan-mcp --help
~/.venvs/ph-tutorial/bin/dechecker-mcp --help
~/.venvs/ph-tutorial/bin/zerogpt-mcp --help
```
Expected: each exits without a traceback. If a package fails to install from PyPI, fall back to a source install: `~/.venvs/ph-tutorial/bin/pip install git+https://github.com/bogrum/<name>`.

- [ ] **Step 3: Register the servers for this project only**

Create `.mcp.json` at the repository root. Project scope keeps the maintainer's global configuration untouched:

```json
{
  "mcpServers": {
    "lmscan": {
      "command": "/home/emre/.venvs/ph-tutorial/bin/lmscan-mcp"
    },
    "dechecker": {
      "command": "/home/emre/.venvs/ph-tutorial/bin/dechecker-mcp"
    },
    "zerogpt": {
      "command": "/home/emre/.venvs/ph-tutorial/bin/zerogpt-mcp"
    }
  }
}
```

- [ ] **Step 4: Confirm lmscan answers**

Restart the session so the servers load, then run `scan_text` on two paragraphs of the existing `README.md`. Expected: a score and verdict come back. lmscan is offline, so this must work without network.

If `dechecker` or `zerogpt` fail here, continue anyway — they scrape live websites and are only the final spot-check. Record the failure and rely on lmscan. If **lmscan** fails, stop and report: it carries the loop.

- [ ] **Step 5: Commit**

```bash
git add .mcp.json
git commit -m "chore: register AI-detection MCP servers for tutorial authoring"
```

---

## Task 4: Content — Welcome, Before you start, Sessions

**Files:**
- Modify: `docs/tutorial/tutorial.md`

**Interfaces:**
- Consumes: the intro block from Task 2, the prose gate from Task 3.
- Produces: heading ids `upload` and `sessions`, referenced by later sections.

- [ ] **Step 1: Read the deployed source of truth**

Do not write from memory. Read, over SSH:

```bash
ssh pockethunter 'cd ~/PocketHunter-Suite && sed -n "60,110p" panels/find_pockets.py'
ssh pockethunter 'cd ~/PocketHunter-Suite && grep -n "MAX_UPLOAD_SIZE\|SESSION_TTL\|MAX_FRAMES\|QUOTA" .env.example settings.py config.py'
ssh pockethunter 'cd ~/PocketHunter-Suite && sed -n "1,80p" session_routes.py'
ssh pockethunter 'cd ~/PocketHunter-Suite && ls -la examples/trypsin examples/tem1'
```

Record the exact accepted topology extensions — commit `bff1bb2` on `origin/redesign` is titled "accept .pdb or .gro topology", so confirm against the deployed `panels/find_pockets.py` rather than trusting the section outline.

- [ ] **Step 2: Write the three sections**

Replace the placeholder bodies in `docs/tutorial/tutorial.md`. Keep the `## Welcome` intro block from Task 2 intact and write beneath it. Required content:

- **Welcome** — one paragraph on what the tool does and the problem it addresses (pockets that open and close over a trajectory), then the intro block.
- **Upload your files** (id `upload`) — the two inputs, accepted extensions as verified in Step 1, real size limits with their numbers, and the bundled `examples/trypsin` and `examples/tem1` datasets with their actual file sizes.
- **Sessions** (id `sessions`) — what `?s=` is, the difference between the editor URL (carries `edit=`) and the view-only URL, what persists, when a session expires.

Every number in these sections must come from Step 1's output.

- [ ] **Step 3: Run the prose gate**

Run `scan_mixed_text` (lmscan MCP) on the three sections. Rewrite any paragraph scoring above 0.5 — the reliable fix is replacing generic phrasing with the specific fact it is standing in for, not reaching for synonyms. Rescan until clean.

- [ ] **Step 4: Build and check**

Run: `python scripts/build_tutorial.py --built-at 2026-08-05 && python -m pytest tests/test_tutorial_build.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/tutorial/tutorial.md static/tutorial/
git commit -m "docs: tutorial sections on inputs and sessions"
```

---

## Task 5: Content — Find pockets, Cluster, Dock

**Files:**
- Modify: `docs/tutorial/tutorial.md`

**Interfaces:**
- Consumes: Task 4's sections.
- Produces: heading ids `find-pockets`, `cluster`, `dock` — already linked from the Task 2 schematic, so their spelling must not change.

- [ ] **Step 1: Read the deployed source of truth**

```bash
ssh pockethunter 'cd ~/PocketHunter-Suite && sed -n "340,370p" panels/find_pockets.py'
ssh pockethunter 'cd ~/PocketHunter-Suite && sed -n "390,430p;565,585p" panels/cluster.py'
ssh pockethunter 'cd ~/PocketHunter-Suite && sed -n "310,365p" panels/docking.py'
ssh pockethunter 'cd ~/PocketHunter-Suite && sed -n "290,345p" PocketHunter/pockethunter.py'
ssh pockethunter 'cd ~/PocketHunter-Suite && grep -n "DOCKING_EXHAUSTIVENESS\|DOCKING_MAX_PAIRS" .env.example docker-compose.yml'
```

Known values to confirm rather than assume: `Min probability` slider `0.0`–`1.0` step `0.05`; `Number of poses` `1`–`50` default `10`; `pH (protonation)` `4.0`–`10.0` step `0.1` default `7.4`; `DOCKING_EXHAUSTIVENESS` default `8`; `DOCKING_MAX_PAIRS` default `1000`.

- [ ] **Step 2: Write the three sections**

- **Find pockets** — what p2rank does per frame, what `Min probability` filters, what the confidence multiselect does, how to read the resulting table.
- **Cluster** — that clustering groups pockets by **residue composition** using DBSCAN with a Hamming metric, not by geometric proximity, so a reader does not misread the output. On parameter selection: describe that the app sweeps `eps` and `min_samples` and picks by silhouette score, and state plainly that the resulting choice should be checked against the viewer rather than trusted as optimal. **Do not** claim the parameters are optimal — see Global Constraints.
- **Dock** — ligand upload, `Number of poses`, `pH`, exhaustiveness, and what SMINA's score means. Docking scores may be explained without qualification.

**Forbidden in this task:** any mention of pharmacophore scoring, the `discriminate` subcommand, Layer 1/Layer 2, rdkit, or prody. None of it is deployed.

- [ ] **Step 3: Run the prose gate**

`scan_mixed_text` on all three sections; rewrite anything above 0.5; rescan.

- [ ] **Step 4: Verify the schematic still resolves**

Run: `python scripts/build_tutorial.py --built-at 2026-08-05 && python -m pytest tests/test_tutorial_build.py -v`
Expected: PASS — in particular `test_every_schematic_stage_links_to_a_real_section`, which fails if a heading was reworded.

- [ ] **Step 5: Commit**

```bash
git add docs/tutorial/tutorial.md static/tutorial/
git commit -m "docs: tutorial sections for the three pipeline stages"
```

---

## Task 6: Content — Results, reference, limits, troubleshooting, citing

**Files:**
- Modify: `docs/tutorial/tutorial.md`

**Interfaces:**
- Consumes: Tasks 4 and 5.
- Produces: the finished document; Task 7 adds images into it.

- [ ] **Step 1: Read the deployed source of truth**

```bash
ssh pockethunter 'cd ~/PocketHunter-Suite && grep -n "def \|st.download_button" downloads.py | head -30'
ssh pockethunter 'cd ~/PocketHunter-Suite && grep -nE "RATE_LIMIT|QUOTA|MAX_|TURNSTILE|CAPTCHA" .env.example | head -40'
ssh pockethunter 'cd ~/PocketHunter-Suite && grep -n "class \|def " failure_view.py task_errors.py | head -30'
ssh pockethunter 'cd ~/PocketHunter-Suite && grep -rn "p2rank\|smina\|SMINA" README.md PocketHunter/README.md | head -20'
```

- [ ] **Step 2: Write the sections**

- **Reading the results** — the Mol\* viewer, the frame slider, what each download contains.
- **Parameter reference** — one markdown table: parameter, where it appears, range, default, effect. Values from Task 5 Step 1 and this task's Step 1.
- **Limits and quotas** — per-session disk quota, per-pool concurrency, per-IP session-creation caps, CAPTCHA, worker recycling. Real numbers only.
- **Troubleshooting** — the failure modes the app actually surfaces, taken from `failure_view.py` and `task_errors.py`, not invented ones.
- **Citing** — PocketHunter, SMINA, p2rank, Mol\*. State plainly that neither `costbio/PocketHunter` nor `costbio/PocketHunter-Suite` currently carries a LICENSE file, so reuse terms should be confirmed with the authors.

- [ ] **Step 3: Fill in the measured worked-example facts**

Replace `MEASURED_EXAMPLE` in the Task 2 facts block with the real figures — dataset, frame count, and observed wall-clock time — measured during Task 7's capture run. If Task 7's capture is deferred to the maintainer, measure frame count locally from `examples/trypsin/trajectory.xtc` and state the runtime as observed on the shared service rather than inventing a figure.

- [ ] **Step 4: Run the prose gate, including the web checkers**

`scan_mixed_text` across the whole document; rewrite anything above 0.5.

Then the final spot-check: run `check_dechecker` and the zerogpt checker on two or three representative sections — one narrative (Welcome), one procedural (Dock), one reference-style (Limits). Both tools cap at roughly 1000 characters, so submit an excerpt per section. Expected: no section returns an "AI-generated" verdict. Recall that lmscan's own documentation puts false positives on technical writing at 5–15 %, so a nonzero score is not itself a failure; a verdict is.

- [ ] **Step 5: Build and check**

Run: `python scripts/build_tutorial.py --built-at 2026-08-05 && python -m pytest tests/test_tutorial_build.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/tutorial/tutorial.md static/tutorial/
git commit -m "docs: tutorial reference, limits, troubleshooting and citing"
```

---

## Task 7: Screenshots

**Files:**
- Create: `docs/tutorial/img/*.png`
- Create: `scripts/capture_tutorial_screenshots.py`
- Modify: `docs/tutorial/tutorial.md`

**Interfaces:**
- Consumes: the finished prose from Task 6.
- Produces: image files referenced as `img/<name>.png` in the markdown; the build copies `docs/tutorial/img/` to `static/tutorial/img/`, so relative paths resolve identically in preview and in production.

- [ ] **Step 1: Confirm the capture is authorised**

The maintainer approved attempting Playwright capture, with manual capture as the fallback. Capture runs against the **live** service and creates a real session plus real worker jobs. Use `examples/trypsin` only — it is the smaller dataset. Do not run `examples/tem1`.

- [ ] **Step 2: Write the capture script**

Create `scripts/capture_tutorial_screenshots.py`:

```python
#!/usr/bin/env python3
"""Capture tutorial screenshots from the live service.

Runs against https://pockethunter.bio-cloud.site so the images match what
a reader actually sees. This creates a real session and real worker jobs
on a shared, quota-limited service — use the small trypsin dataset only.

Usage:
    ~/.venvs/ph-tutorial/bin/python scripts/capture_tutorial_screenshots.py
"""
from __future__ import annotations

import pathlib

from playwright.sync_api import sync_playwright

BASE = "https://pockethunter.bio-cloud.site"
OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "tutorial" / "img"
VIEWPORT = {"width": 1440, "height": 900}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)
        page.goto(BASE, wait_until="networkidle")
        page.screenshot(path=OUT / "01-landing.png")
        print("captured landing")
        # Remaining captures are added as the run progresses — each step
        # needs the previous stage's job to finish, so they are driven
        # interactively rather than blind-scripted.
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Capture the landing page**

Run: `~/.venvs/ph-tutorial/bin/python scripts/capture_tutorial_screenshots.py`
Expected: `docs/tutorial/img/01-landing.png` exists and shows the masthead and landing content.

If Streamlit's WebSocket render does not settle under `networkidle`, replace the wait with an explicit selector wait, for example `page.wait_for_selector("text=POCKETHUNTER/SUITE")`.

- [ ] **Step 4: Capture the three stage panels**

Drive a real trypsin session through Find Pockets, Cluster and Dock, screenshotting each panel once its results render. Target four more images: `02-find-pockets.png`, `03-cluster.png`, `04-dock.png`, `05-results-viewer.png`.

If Playwright cannot drive Streamlit reliably — the usual obstacle is that reruns replace nodes mid-interaction — stop and hand this step to the maintainer, who offered to capture manually. Do not spend more than one focused attempt per panel.

- [ ] **Step 5: Reference the images from the prose**

Add each image to its section in `docs/tutorial/tutorial.md`, with alt text that describes what the reader should notice:

```markdown
![The Find Pockets panel after a trypsin run, showing the pocket table ranked by probability](img/02-find-pockets.png)
```

- [ ] **Step 6: Build and check**

Run: `python scripts/build_tutorial.py --built-at 2026-08-05 && python -m pytest tests/test_tutorial_build.py -v`
Expected: PASS. Confirm `static/tutorial/img/` now contains the same files as `docs/tutorial/img/`.

- [ ] **Step 7: Commit**

```bash
git add scripts/capture_tutorial_screenshots.py docs/tutorial/ static/tutorial/
git commit -m "docs: screenshots captured from the live service"
```

---

## Task 8: Masthead link button

**Files:**
- Modify: `landing.py:600-607` (delete `_tutorial_dialog`), `landing.py:735-738` (the button)
- Modify: `main.py:107-125` (CSS selectors)
- Modify: `tests/test_masthead.py`

**Interfaces:**
- Consumes: the tutorial URL `/app/static/tutorial/index.html`.
- Produces: `landing.TUTORIAL_URL`, a module-level constant the tests assert on.

- [ ] **Step 1: Write the failing test**

`tests/test_masthead.py`'s `_render_to_capture` patches `st.button` but not `st.link_button`, so it must capture both. Modify the helper — add a `link_buttons` list, a `fake_link_button`, and the extra patch entry:

```python
    captured = {"markdown": [], "buttons": [], "link_buttons": []}

    def fake_link_button(label, url, *args, key=None, **kwargs):
        captured["link_buttons"].append({"label": label, "url": url,
                                         "key": key, "kwargs": dict(kwargs)})

    ...

    with patch.multiple(
        "landing.st",
        markdown=fake_markdown,
        button=fake_button,
        link_button=fake_link_button,
        container=fake_container,
        columns=fake_columns,
    ):
```

Three existing tests assert `"nav_tutorial" in keys` where `keys` comes from `captured["buttons"]` — `test_no_session_omits_session_inline`, `test_nav_row_with_session_has_all_three_buttons_and_costbio`, and `test_nav_row_on_landing_has_all_three_buttons_too`. In each, drop `nav_tutorial` from the button-key assertions and add a link-button key assertion:

```python
        link_keys = [b["key"] for b in cap["link_buttons"]]
        assert "nav_tutorial" in link_keys
```

Then add a new class:

```python
class TestTutorialLink:
    def test_tutorial_is_a_link_button_to_the_static_page(self):
        cap = _render_to_capture(resolved=None)
        link = next(b for b in cap["link_buttons"] if b["key"] == "nav_tutorial")
        assert link["label"] == "TUTORIAL"
        assert link["url"] == "/app/static/tutorial/index.html"

    def test_placeholder_dialog_is_gone(self):
        import landing
        assert not hasattr(landing, "_tutorial_dialog")

    def test_help_stays_a_dialog_button(self):
        """Only TUTORIAL changes; HELP has no page to point at yet."""
        cap = _render_to_capture(resolved=None)
        assert "nav_help" in [b["key"] for b in cap["buttons"]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_masthead.py -v`
Expected: FAIL — `TestTutorialLink` cannot find a `nav_tutorial` link button, and `test_placeholder_dialog_is_gone` fails because `_tutorial_dialog` still exists.

- [ ] **Step 3: Change landing.py**

Add the constant near the other module-level definitions:

```python
TUTORIAL_URL = "/app/static/tutorial/index.html"
```

Delete the whole `_tutorial_dialog` function (currently `landing.py:600-607`, the `@st.dialog("Tutorial")` block). Leave `_help_dialog` alone.

Replace the button at `landing.py:735-738`:

```python
        with nav_cols[1]:
            if st.button("TUTORIAL", key="nav_tutorial",
                         use_container_width=True):
                _tutorial_dialog()
```

with:

```python
        with nav_cols[1]:
            st.link_button("TUTORIAL", TUTORIAL_URL, key="nav_tutorial",
                           use_container_width=True)
```

Streamlit 1.58's `st.link_button` accepts `key`, so the `.st-key-nav_tutorial` wrapper class the CSS depends on is still emitted.

- [ ] **Step 4: Change main.py**

`st.link_button` renders an `<a>`, not a `<button>`, so the two masthead rules at `main.py:107-125` need to match both. Change:

```css
    .st-key-nav_new_session button,
    .st-key-nav_tutorial button,
    .st-key-nav_help button {
```

to:

```css
    .st-key-nav_new_session button,
    .st-key-nav_tutorial button,
    .st-key-nav_tutorial a,
    .st-key-nav_help button {
```

and the hover rule:

```css
    .st-key-nav_new_session button:hover:not(:disabled),
    .st-key-nav_tutorial button:hover:not(:disabled),
    .st-key-nav_help button:hover:not(:disabled) {
```

to:

```css
    .st-key-nav_new_session button:hover:not(:disabled),
    .st-key-nav_tutorial button:hover:not(:disabled),
    .st-key-nav_tutorial a:hover,
    .st-key-nav_help button:hover:not(:disabled) {
```

Add `text-decoration: none !important;` to the first rule's body — the app's theme sets `linkUnderline = true`, which would otherwise underline the button label.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_masthead.py -v`
Expected: PASS, every test.

Run: `python -m pytest tests/ -q`
Expected: no new failures relative to the branch point. Record any pre-existing failures rather than fixing them here.

- [ ] **Step 6: Commit**

```bash
git add landing.py main.py tests/test_masthead.py
git commit -m "feat: point the masthead TUTORIAL button at the tutorial page"
```

---

## Task 9: Local preview and approval gate

**Files:** none modified.

**Interfaces:**
- Consumes: everything above.
- Produces: the maintainer's go/no-go decision.

- [ ] **Step 1: Build the page from a clean state**

```bash
rm -rf static/tutorial
python scripts/build_tutorial.py
python -m pytest tests/test_tutorial_build.py tests/test_masthead.py -v
```
Expected: PASS.

- [ ] **Step 2: Serve it locally**

```bash
python -m http.server 8080 --directory static/tutorial
```
The built page is self-contained, so this is a faithful preview of what production will serve.

- [ ] **Step 3: Check it by hand**

- Click every table-of-contents entry; each lands on its heading, not under the sticky masthead.
- Click all four schematic stages; each jumps to the right section.
- Load `http://localhost:8080/index.html#welcome` directly; it scrolls on first paint.
- Narrow the window below 860 px; the table of contents stacks above the content and nothing scrolls sideways.
- Every screenshot loads.

- [ ] **Step 4: Confirm nothing has been deployed or pushed**

```bash
git status -sb
git log --oneline origin/redesign..HEAD
ssh pockethunter 'ls ~/PocketHunter-Suite/static/ | grep -c tutorial || echo "0 — nothing deployed"'
```
Expected: commits exist locally and are ahead of `origin/redesign`; the host has no `static/tutorial`.

- [ ] **Step 5: Ask for approval**

Present the preview URL and a summary. **Stop here.** Task 10 does not begin without an explicit yes.

---

## Task 10: Deployment

**Do not start this task without explicit approval from the maintainer.**

**Files:** none in the repository; this task writes to the host.

**Interfaces:**
- Consumes: approval from Task 9.
- Produces: the live page.

- [ ] **Step 1: Copy the built page to the host**

Additive: this creates a new directory and modifies nothing that exists.

```bash
rsync -av --delete static/tutorial/ pockethunter:~/PocketHunter-Suite/static/tutorial/
```

- [ ] **Step 2: Verify the page is served**

```bash
curl -sS -o /dev/null -w "%{http_code} %{size_download}\n" \
  https://pockethunter.bio-cloud.site/app/static/tutorial/index.html
```
Expected: `200` and a non-trivial byte count.

- [ ] **Step 3: Apply the masthead patch to the host**

The host's `landing.py` and `main.py` were verified byte-identical to `origin/redesign` at planning time. Re-verify before patching, because that could have changed:

```bash
for f in landing.py main.py; do
  diff <(git show origin/redesign:$f) <(ssh pockethunter "cat ~/PocketHunter-Suite/$f") \
    && echo "$f unchanged — safe to patch"
done
```

If both are unchanged, copy the two patched files:

```bash
scp landing.py main.py pockethunter:~/PocketHunter-Suite/
```

If either differs, **stop** and report. Do not overwrite host-local edits.

Deliberately not used: `git pull` on the host. Its checkout is on `redesign` at a commit absent from GitHub, three commits behind `origin/redesign`, and carries uncommitted modifications to `tasks.py`, `docker-entrypoint.sh`, `streamlit.Dockerfile` and `streamlit_shell_css.py`. Pulling would entangle this change with that drift.

- [ ] **Step 4: Verify the button in the live app**

Streamlit watches the bind-mounted source and reruns, so no restart is needed. Load `https://pockethunter.bio-cloud.site`, confirm `TUTORIAL` renders with the same brutalist styling as its neighbours and opens the page.

If styling regressed, the cause is almost certainly the CSS selector: inspect the rendered element and confirm it sits inside `.st-key-nav_tutorial` and is an `<a>`.

- [ ] **Step 5: Report**

Report what was deployed, the live URL, and the rollback command:

```bash
ssh pockethunter 'rm -rf ~/PocketHunter-Suite/static/tutorial'
```
plus restoring `landing.py` and `main.py` from `origin/redesign` if the masthead patch needs reverting.

---

## Self-Review

**Spec coverage.** Build pipeline → Task 1. Intro block → Task 2. Prose gate tooling → Task 3. Content outline sections 1–11 → Tasks 4, 5, 6. Accuracy constraints → Global Constraints, enforced in Tasks 5 and 6. Screenshots → Task 7. Masthead button → Task 8. Preview and approval gate → Task 9. Additive deployment → Task 10. Testing → Tasks 1, 2, 8, 9. The clean `/tutorial` URL is future work in the spec and correctly absent here.

**Placeholders.** One intentional token, `MEASURED_EXAMPLE`, introduced in Task 2 Step 4 and resolved in Task 6 Step 3 — it stands for a figure that must be measured rather than guessed, and both ends are named.

**Type consistency.** `render(md_text, template_text, *, built_at)` is defined in Task 1 and called with that signature in Tasks 1 and 2's tests. `main(argv)` matches its CLI use. `TUTORIAL_URL` is defined in Task 8 Step 3 and asserted in Task 8 Step 1. Template placeholders `{{ TOC }}`, `{{ CONTENT }}`, `{{ BUILT_AT }}` are consistent across the script, the template and the tests. Schematic targets `upload`, `find-pockets`, `cluster`, `dock` are asserted in Task 2 and created in Tasks 4 and 5.
