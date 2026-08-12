#!/usr/bin/env python3
"""Capture tutorial screenshots from the live service.

Runs against https://pockethunter.bio-cloud.site so the images match what
a reader actually sees. This creates a real session and real worker jobs
on a shared, quota-limited service — use the small trypsin dataset only.
Do not run examples/tem1; it is a byte-identical duplicate of trypsin and
would waste a second run for nothing.

Two things this script learned the hard way, worth keeping in mind before
you re-run it:

1. The first navigation in this environment has been observed pulling the
   Streamlit JS bundle (2-3 MB) over a link as slow as ~37 KB/s, so
   `page.goto(..., wait_until="load")` can time out well past 30s even
   though the server answers instantly. A persistent browser profile
   (PROFILE_DIR below) keeps that bundle cached across runs; navigation
   waits use `wait_until="commit"` plus an explicit selector wait instead
   of "load" or "networkidle" (Streamlit keeps a websocket open, so
   "networkidle" never fires).

2. Streamlit reruns replace DOM nodes, and this app additionally keeps
   the docking "staged pockets" bucket and the uploaded-ligand widget
   state in ephemeral per-connection session state — not in the
   database-backed Session/Job rows. That state is lost if you navigate
   away and back (a fresh page load is a fresh websocket connection).
   So Cluster -> "Add representatives to docking" -> Dock -> upload ->
   submit has to happen as one unbroken sequence of interactions on a
   single `page`, not as separate script invocations the way the Find
   Pockets capture can be (its job and results live in the database and
   survive a fresh navigation).

Usage:
    ~/.venvs/ph-tutorial/bin/python docs/tutorial/capture_screenshots.py landing
    ~/.venvs/ph-tutorial/bin/python docs/tutorial/capture_screenshots.py full-run

`full-run` drives the whole worked example in one go: starts a session
from the bundled trypsin example, waits for Find Pockets, runs Cluster,
stages all cluster representatives for docking, uploads a small embedded
ligand (benzamidine — trypsin's classic inhibitor; nothing in the repo
ships a ligand fixture), runs Dock, and screenshots each stage's results
panel. It prints the session URL and the Find Pockets job's own
id-embedded submission time and "Updated" completion time, which is the
one honest source for how long the worked example actually took — do not
substitute this script's own wall-clock elapsed time for that; most of it
is Playwright/selector overhead, not pipeline runtime.
"""
from __future__ import annotations

import pathlib
import sys
import time

from playwright.sync_api import Page, sync_playwright

BASE = "https://pockethunter.bio-cloud.site"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "docs" / "tutorial" / "img"

# A persistent Chromium profile so the multi-MB JS bundle is cached across
# runs instead of re-fetched over a slow link every time.
PROFILE_DIR = REPO_ROOT / ".tutorial_capture_profile"  # gitignored; local only

VIEWPORT = {"width": 1440, "height": 1400}
NAV_TIMEOUT_MS = 150_000

# A minimal 3D SDF for benzamidine (trypsin's classic small-molecule
# inhibitor), embedded so this script has no external file dependency.
# Generated once with RDKit (ETKDG embed + MMFF optimize) and frozen here.
BENZAMIDINE_SDF = """\
benzamidine
     RDKit          3D

 18 18  0  0  0  0  0  0  0  0999 V2000
    1.8559   -1.4942    0.9026 N   0  0  0  0  0  0  0  0  0  0  0  0
    1.3245   -0.9392   -0.1648 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.8385   -1.1214   -1.3616 N   0  0  0  0  0  0  0  0  0  0  0  0
    0.1331   -0.0947   -0.0166 C   0  0  0  0  0  0  0  0  0  0  0  0
   -1.0208   -0.3493   -0.7745 C   0  0  0  0  0  0  0  0  0  0  0  0
   -2.1531    0.4543   -0.6315 C   0  0  0  0  0  0  0  0  0  0  0  0
   -2.1438    1.5192    0.2666 C   0  0  0  0  0  0  0  0  0  0  0  0
   -1.0051    1.7835    1.0242 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.1285    0.9810    0.8854 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.4193   -1.3585    1.8095 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.6756   -2.0836    0.8619 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.4203   -0.6599   -2.1638 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.6581   -1.6897   -1.5240 H   0  0  0  0  0  0  0  0  0  0  0  0
   -1.0610   -1.1843   -1.4717 H   0  0  0  0  0  0  0  0  0  0  0  0
   -3.0487    0.2510   -1.2165 H   0  0  0  0  0  0  0  0  0  0  0  0
   -3.0276    2.1456    0.3765 H   0  0  0  0  0  0  0  0  0  0  0  0
   -1.0038    2.6207    1.7204 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.0099    1.2198    1.4779 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  2  0
  2  4  1  0
  4  5  2  0
  5  6  1  0
  6  7  2  0
  7  8  1  0
  8  9  2  0
  9  4  1  0
  1 10  1  0
  1 11  1  0
  3 12  1  0
  3 13  1  0
  5 14  1  0
  6 15  1  0
  7 16  1  0
  8 17  1  0
  9 18  1  0
M  CHG  1   3   1
M  END
$$$$
"""


def new_page(p):
    context = p.chromium.launch_persistent_context(
        str(PROFILE_DIR), viewport=VIEWPORT, headless=True
    )
    page = context.pages[0] if context.pages else context.new_page()
    return context, page


def goto_app(page: Page, url: str) -> None:
    page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
    page.wait_for_selector("text=POCKETHUNTER/SUITE", timeout=NAV_TIMEOUT_MS)


def crop_results_panel(page: Page, path: pathlib.Path, max_height: int = 800) -> None:
    """Screenshot just the right-hand results column (stats + tabs + table),
    excluding the header and the persistent Mol* viewer column — the viewer
    did not reliably mount in headless Chromium during this capture (see
    task-7-report.md), so panels are cropped to what actually rendered
    rather than shipping a screenshot of a permanent "loading" placeholder.
    """
    box = page.evaluate(
        """
() => {
  const cols = [...document.querySelectorAll('[data-testid="stColumn"]')]
    .filter(c => c.getBoundingClientRect().width > 500);
  cols.sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);
  const c = cols[cols.length - 1];
  const r = c.getBoundingClientRect();
  return {x: r.x, y: r.y, width: r.width, height: r.height};
}
"""
    )
    pad = 8
    page.screenshot(
        path=path,
        clip={
            "x": box["x"] - pad,
            "y": box["y"] - pad,
            "width": box["width"] + 2 * pad,
            "height": min(box["height"] + 2 * pad, max_height),
        },
        timeout=60_000,
    )


def cmd_landing() -> int:
    with sync_playwright() as p:
        context, page = new_page(p)
        goto_app(page, BASE)
        page.wait_for_timeout(2000)
        box = page.evaluate(
            """
() => {
  const el = [...document.querySelectorAll('*')]
    .find(e => e.textContent && e.textContent.trim() === 'Open an existing analysis');
  const r = el.getBoundingClientRect();
  return {y: r.y, height: r.height};
}
"""
        )
        path = OUT / "01-landing.png"
        page.screenshot(
            path=path,
            clip={"x": 0, "y": 0, "width": 1440, "height": box["y"] + box["height"] + 10},
            timeout=60_000,
        )
        print("captured landing ->", path)
        context.close()
    return 0


def cmd_full_run() -> int:
    with sync_playwright() as p:
        context, page = new_page(p)
        goto_app(page, BASE)

        # --- Start a session from the bundled trypsin example. This queues
        # the find-pockets job immediately. ---
        page.get_by_role("button", name="Try with example trajectory").click()
        page.wait_for_url(lambda u: "edit=" in u, timeout=NAV_TIMEOUT_MS)
        session_url = page.url
        print("session started:", session_url)

        # --- Find Pockets: poll until the job's own status settles, then
        # crop the results panel. ---
        page.wait_for_timeout(3000)
        t0 = time.time()
        while time.time() - t0 < 20 * 60:
            if page.locator("canvas").count() > 0:
                break
            time.sleep(5)
            page.reload(wait_until="commit", timeout=NAV_TIMEOUT_MS)
            page.wait_for_selector("text=POCKETHUNTER/SUITE", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(2000)
        crop_results_panel(page, OUT / "02-find-pockets.png")
        print("captured find pockets ->", OUT / "02-find-pockets.png")

        # Print the job's own timestamps — the honest source for "time to
        # first pockets" (do not use this script's wall-clock instead).
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(500)
        jobs_expander = page.locator('[data-testid="stExpander"]').first
        jobs_expander.click()
        page.wait_for_timeout(1500)
        print("Jobs panel:", jobs_expander.inner_text())
        jobs_expander.click()  # collapse again

        # --- Cluster: run with defaults, then open Representatives (the
        # Heatmap tab did not reliably paint a canvas in this environment;
        # Representatives is the same information as a table and rendered
        # reliably). ---
        page.get_by_text("Cluster", exact=True).first.click()
        page.wait_for_timeout(2000)
        page.locator('[data-testid="stBaseButton-primary"]', has_text="Cluster").click()
        t0 = time.time()
        while time.time() - t0 < 3 * 60:
            if page.get_by_text("Heatmap", exact=True).count() > 0:
                break
            time.sleep(5)
        page.wait_for_timeout(2000)
        page.get_by_text("Representatives", exact=True).click()
        page.wait_for_timeout(6000)
        crop_results_panel(page, OUT / "03-cluster.png", max_height=620)
        print("captured cluster ->", OUT / "03-cluster.png")

        # --- Dock: stage all cluster representatives, upload the embedded
        # ligand, run, and wait for the *complete* state (the panel shows
        # partial results while still running, which is easy to mistake
        # for done). ---
        add_btn = page.get_by_role("button", name="Add all")
        add_btn.click()
        page.wait_for_timeout(2500)
        page.get_by_text("Dock", exact=True).first.click()
        page.wait_for_timeout(3000)

        ligand_path = pathlib.Path("/tmp/pockethunter_tutorial_benzamidine.sdf")
        ligand_path.write_text(BENZAMIDINE_SDF)
        finput = None
        for _ in range(10):
            if page.locator('input[type="file"]').count() > 0:
                finput = page.locator('input[type="file"]').first
                break
            page.wait_for_timeout(3000)
        if finput is None:
            print("ERROR: ligand file input never mounted; aborting Dock capture", file=sys.stderr)
        else:
            finput.set_input_files(str(ligand_path))
            dock_btn = page.locator('[data-testid="stBaseButton-primary"]', has_text="Dock")
            t0 = time.time()
            while time.time() - t0 < 60:
                if not dock_btn.first.is_disabled():
                    break
                time.sleep(3)
            dock_btn.first.click()
            print("submitted docking job")
            t0 = time.time()
            while time.time() - t0 < 10 * 60:
                if page.get_by_text("New docking run", exact=True).count() > 0:
                    break
                time.sleep(6)
            page.wait_for_timeout(2000)
            crop_results_panel(page, OUT / "04-dock.png", max_height=620)
            print("captured dock ->", OUT / "04-dock.png")

        context.close()
    return 0


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    cmd = sys.argv[1]
    if cmd == "landing":
        raise SystemExit(cmd_landing())
    elif cmd == "full-run":
        raise SystemExit(cmd_full_run())
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        raise SystemExit(1)
