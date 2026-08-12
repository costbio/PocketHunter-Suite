# The tutorial page

Everything editable for `https://pockethunter.bio-cloud.site/app/static/tutorial/index.html`
lives in this directory.

```
tutorial.md              the prose — this is what you edit
template.html            page shell: theme CSS, masthead, TOC slot
img/                     screenshots
build.py                 compiles tutorial.md + template.html into the served page
capture_screenshots.py   drives the live service with Playwright to re-take img/
requirements-build.txt   build-time dependency (markdown), not a runtime one
```

Two related files sit outside this directory and cannot move:

- `tests/test_tutorial_build.py` — CI runs `pytest tests/`, so a test moved out of
  that directory silently stops running.
- `static/tutorial/` — the build output. Streamlit serves `static/` under
  `/app/static/` (`enableStaticServing` in `.streamlit/config.toml`), which is how
  the page reaches a browser at all.

## Editing

```bash
$EDITOR docs/tutorial/tutorial.md
python docs/tutorial/build.py
python -m pytest tests/test_tutorial_build.py -q
```

**Never edit `static/tutorial/index.html`.** It is generated, and the next build
overwrites it. `test_committed_page_is_a_current_render_of_its_sources` fails if
the committed page and its sources disagree, so a forgotten rebuild is caught
rather than shipped.

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
real worker jobs on a shared, quota-limited box. Use the bundled `examples/trypsin`
dataset. Streamlit replaces DOM nodes on every rerun, so selectors go stale
mid-interaction; wait on content appearing (or a loading placeholder disappearing)
rather than on a fixed sleep or on `networkidle`, which never fires because
Streamlit holds a WebSocket open.

The Mol\* viewer does not mount in headless Chromium even with WebGL available, so
the results-viewer panel has no screenshot. Capture that one by hand if it is
wanted.

## Facts in the page

Every number, widget label and limit on the page was read off the deployed host
rather than this checkout, which has drifted from it. If you add a claim, check it
against what is actually running — and prefer naming a function over citing a line
number, since line numbers rot on the next change and readers of a hosted service
have no checkout to look in.
