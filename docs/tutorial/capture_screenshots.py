#!/usr/bin/env python3
"""Capture tutorial screenshots from the live service.

Runs against https://pockethunter.bio-cloud.site so the images match what
a reader actually sees. This creates a real session and real worker jobs
on a shared, quota-limited service — the bundled TEM-1 dataset
(examples/tem1, 98 frames of a 263-residue chain) is small enough to be a
polite thing to run there. Nothing else should be.

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
    ~/.venvs/ph-tutorial/bin/python docs/tutorial/capture_screenshots.py resume <session-url>

`resume` re-enters an existing session and redoes Cluster + Dock only,
which is the cheap way to recapture the later stages without asking the
worker pool for another pocket search.

`full-run` drives the whole worked example in one go: starts a session
from the bundled TEM-1 example, waits for Find Pockets, runs Cluster,
stages all cluster representatives for docking, uploads the two embedded
ligands below, runs Dock, and screenshots each stage's results panel. It
prints the session URL and the Find Pockets job's own
id-embedded submission time and "Updated" completion time, which is the
one honest source for how long the worked example actually took — do not
substitute this script's own wall-clock elapsed time for that; most of it
is Playwright/selector overhead, not pipeline runtime.
"""
from __future__ import annotations

import os
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

# Two ligands, embedded so this script has no external file dependency.
# Both were generated once with `obabel -:<SMILES> -osdf --gen3d` and
# frozen here; the input conformer barely matters because smina rebuilds
# the ligand from its rotatable bonds anyway.
#
# They are chosen to sit on opposite sides of the point the tutorial is
# making. CBT is the het code in PDB 1PZO/1PZP — Horn & Shoichet found it
# binding a cryptic pocket ~16 A from TEM-1's catalytic serine, a site
# that only exists once helices 11 and 12 separate. It is exactly the kind
# of hit an ensemble finds and a single crystal structure does not.
# Sulbactam is the orthosteric control: a clinical beta-lactamase
# inhibitor that binds the active site, present in every conformation.
#
# CBT: N,N-bis(4-chlorobenzyl)-1H-tetrazol-5-amine, C15H13Cl2N5, 334.203 Da
CBT_SDF = """\
CBT
 OpenBabel                3D

 35 37  0  0  0  0  0  0  0  0999 V2000
    4.8936    0.6765    3.0095 C   0  0  0  0  0  0  0  0  0  0  0  0
    4.8109    1.3320    4.2389 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.6270    1.9662    4.6054 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5057    1.8974    3.7832 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.5888    1.2417    2.5533 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.8005    0.6730    2.1321 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.9642    0.2090    0.6951 C   0  0  0  0  0  0  0  0  0  0  0  0
    4.4958    1.2899   -0.1495 N   0  0  0  0  0  0  0  0  0  0  0  0
    3.5839    2.3637   -0.5382 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.6643    1.9860   -1.6917 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.5346    2.7878   -1.9314 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.5621    2.3881   -2.8471 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.7251    1.1954   -3.5458 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.8920    0.4486   -3.4036 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.8704    0.8556   -2.4952 C   0  0  0  0  0  0  0  0  0  0  0  0
   -0.5416    0.6198   -4.5567 Cl  0  0  0  0  0  0  0  0  0  0  0  0
    5.8207    1.5739   -0.0920 C   0  0  0  0  0  0  0  0  0  0  0  0
    6.4164    2.7342   -0.4015 N   0  0  0  0  0  0  0  0  0  0  0  0
    7.6797    2.4498   -0.0709 N   0  0  0  0  0  0  0  0  0  0  0  0
    7.9179    1.2214    0.3777 N   0  0  0  0  0  0  0  0  0  0  0  0
    6.7296    0.6347    0.3507 N   0  0  0  0  0  0  0  0  0  0  0  0
    3.5592    2.8746    6.0646 Cl  0  0  0  0  0  0  0  0  0  0  0  0
    5.8345    0.2132    2.7220 H   0  0  0  0  0  0  0  0  0  0  0  0
    5.6849    1.3753    4.8832 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.5787    2.3841    4.0711 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.7190    1.2306    1.9001 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.9888   -0.1284    0.3132 H   0  0  0  0  0  0  0  0  0  0  0  0
    4.6027   -0.6825    0.6586 H   0  0  0  0  0  0  0  0  0  0  0  0
    4.0911    3.2960   -0.8112 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.9389    2.6187    0.3081 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.3911    3.7122   -1.3743 H   0  0  0  0  0  0  0  0  0  0  0  0
   -0.3303    2.9921   -2.9836 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.0344   -0.4671   -3.9706 H   0  0  0  0  0  0  0  0  0  0  0  0
    3.7737    0.2584   -2.3874 H   0  0  0  0  0  0  0  0  0  0  0  0
    8.4164    3.1485   -0.1454 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  1  6  2  0  0  0  0
  1 23  1  0  0  0  0
  2  3  2  0  0  0  0
  2 24  1  0  0  0  0
  3  4  1  0  0  0  0
  3 22  1  0  0  0  0
  4  5  2  0  0  0  0
  4 25  1  0  0  0  0
  5  6  1  0  0  0  0
  5 26  1  0  0  0  0
  6  7  1  0  0  0  0
  7  8  1  0  0  0  0
  7 27  1  0  0  0  0
  7 28  1  0  0  0  0
  8  9  1  0  0  0  0
  8 17  1  0  0  0  0
  9 10  1  0  0  0  0
  9 29  1  0  0  0  0
  9 30  1  0  0  0  0
 10 11  1  0  0  0  0
 10 15  2  0  0  0  0
 11 12  2  0  0  0  0
 11 31  1  0  0  0  0
 12 13  1  0  0  0  0
 12 32  1  0  0  0  0
 13 14  2  0  0  0  0
 13 16  1  0  0  0  0
 14 15  1  0  0  0  0
 14 33  1  0  0  0  0
 15 34  1  0  0  0  0
 17 18  2  0  0  0  0
 17 21  1  0  0  0  0
 18 19  1  0  0  0  0
 19 20  1  0  0  0  0
 19 35  1  0  0  0  0
 20 21  2  0  0  0  0
M  END
$$$$
"""

# Sulbactam: penicillanic acid sulfone, C8H11NO5S, 233.242 Da
SULBACTAM_SDF = """\
sulbactam
 OpenBabel                3D

 26 27  0  0  1  0  0  0  0  0999 V2000
    0.8731    0.0832   -0.0315 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.4225    0.0556    0.0237 C   0  0  0  0  0  0  0  0  0  0  0  0
    3.0680   -0.5388   -1.2657 C   0  0  1  0  0  0  0  0  0  0  0  0
    3.0129    0.5091   -2.2955 N   0  0  0  0  0  0  0  0  0  0  0  0
    3.1501    1.9247   -1.8382 C   0  0  2  0  0  0  0  0  0  0  0  0
    3.0523    1.7582   -0.0335 S   0  0  0  0  0  0  0  0  0  0  0  0
    2.0619    2.6729    0.4932 O   0  0  0  0  0  0  0  0  0  0  0  0
    4.3957    1.7337    0.5002 O   0  0  0  0  0  0  0  0  0  0  0  0
    1.8908    2.2450   -2.6520 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.8206    0.7344   -2.9458 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.0076   -0.0448   -3.3417 O   0  0  0  0  0  0  0  0  0  0  0  0
    4.5299   -1.0215   -1.2010 C   0  0  0  0  0  0  0  0  0  0  0  0
    4.9117   -1.8750   -0.4127 O   0  0  0  0  0  0  0  0  0  0  0  0
    5.3911   -0.5322   -2.1337 O   0  0  0  0  0  0  0  0  0  0  0  0
    2.7673   -0.6438    1.3422 C   0  0  0  0  0  0  0  0  0  0  0  0
    0.4664    0.6727   -0.8520 H   0  0  0  0  0  0  0  0  0  0  0  0
    0.4536    0.5317    0.8798 H   0  0  0  0  0  0  0  0  0  0  0  0
    0.4698   -0.9304   -0.1151 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.4798   -1.4053   -1.5885 H   0  0  0  0  0  0  0  0  0  0  0  0
    4.0798    2.3843   -2.1810 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.0497    2.8485   -3.5513 H   0  0  0  0  0  0  0  0  0  0  0  0
    1.0317    2.6212   -2.0966 H   0  0  0  0  0  0  0  0  0  0  0  0
    4.9979   -0.0220   -2.8681 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.3358   -1.6518    1.3598 H   0  0  0  0  0  0  0  0  0  0  0  0
    2.3470   -0.0979    2.1968 H   0  0  0  0  0  0  0  0  0  0  0  0
    3.8396   -0.7319    1.5311 H   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0  0  0  0
  1 16  1  0  0  0  0
  1 17  1  0  0  0  0
  1 18  1  0  0  0  0
  2  3  1  0  0  0  0
  2  6  1  0  0  0  0
  2 15  1  0  0  0  0
  3  4  1  0  0  0  0
  3 12  1  0  0  0  0
  3 19  1  6  0  0  0
  4  5  1  0  0  0  0
  4 10  1  0  0  0  0
  5  6  1  0  0  0  0
  5  9  1  0  0  0  0
  5 20  1  6  0  0  0
  6  7  2  0  0  0  0
  6  8  2  0  0  0  0
  9 10  1  0  0  0  0
  9 21  1  0  0  0  0
  9 22  1  0  0  0  0
 10 11  2  0  0  0  0
 12 13  2  0  0  0  0
 12 14  1  0  0  0  0
 14 23  1  0  0  0  0
 15 24  1  0  0  0  0
 15 25  1  0  0  0  0
 15 26  1  0  0  0  0
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
        page.wait_for_timeout(2500)
        # Anchor on the two wide landing columns rather than on a heading's
        # text. Streamlit re-nests its DOM on every rerun, so a node matched
        # by exact textContent can silently become a bare <h3> whose box
        # stops above the buttons — which is how an earlier capture cropped
        # "Try with example trajectory" out of the shot it exists to show.
        box = page.evaluate(
            """
() => {
  const cols = [...document.querySelectorAll('[data-testid="stColumn"]')]
    .filter(c => c.getBoundingClientRect().width > 500);
  const bottom = Math.max(...cols.map(c => c.getBoundingClientRect().bottom));
  return {bottom};
}
"""
        )
        path = OUT / "01-landing.png"
        page.screenshot(
            path=path,
            clip={"x": 0, "y": 0, "width": 1440, "height": box["bottom"] + 10},
            timeout=60_000,
        )
        print("captured landing ->", path)
        context.close()
    return 0


def stage_submit_button(page: Page, label: str):
    """The panel's own primary submit button for a stage.

    Every results panel also renders a `Next: <stage> →` primary button to
    walk you forward, so filtering the primary buttons on the stage name
    alone resolves to two elements and Playwright refuses the click under
    strict mode. Excluding the wizard button by its prefix is what makes
    the match unique.
    """
    return (
        page.locator('[data-testid="stBaseButton-primary"]')
        .filter(has_text=label)
        .filter(has_not_text="Next:")
    )


def run_find_pockets(page: Page) -> str:
    """Start a session from the bundled example and capture stage 1."""
    # --- Start a session from the bundled TEM-1 example. This queues
    # the find-pockets job immediately, at stride 1. ---
    page.get_by_role("button", name="Try with example trajectory").click()
    # Poll page.url rather than page.wait_for_url(). Streamlit swaps the
    # session into the query string through the History API, so whether a
    # navigation event fires at all is a race — this exact call has both
    # succeeded and timed out against an unchanged build. Polling the URL
    # observes the state instead of an event, so it cannot lose that race.
    session_url = ""
    t0 = time.time()
    while time.time() - t0 < NAV_TIMEOUT_MS / 1000:
        if "edit=" in page.url:
            session_url = page.url
            break
        time.sleep(1)
    if not session_url:
        raise RuntimeError("session never appeared in the URL after clicking the example button")
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
    page.mouse.wheel(0, -3000)
    page.wait_for_timeout(500)
    return session_url


def run_cluster_and_dock(page: Page) -> None:
    """Capture stages 2 and 3, which must run as one unbroken sequence.

    The staged-pocket bucket and the uploaded-ligand widget live in
    per-connection Streamlit session state, not in the database, so a
    reload between Cluster and Dock loses them. Find Pockets is different
    — its results are database-backed, which is why it can be captured
    (or resumed into) separately.
    """
    # --- Cluster: run with defaults, then open Representatives (the
    # Heatmap tab did not reliably paint a canvas in this environment;
    # Representatives is the same information as a table and rendered
    # reliably). ---
    page.get_by_text("Cluster", exact=True).first.click()
    page.wait_for_timeout(2500)
    stage_submit_button(page, "Cluster").first.click()
    print("submitted cluster job")
    # 778 pockets at stride 1, not the 79 the stride-10 capture had,
    # so the eps/min_samples sweep has a great deal more to chew on.
    t0 = time.time()
    while time.time() - t0 < 15 * 60:
        if page.get_by_text("Heatmap", exact=True).count() > 0:
            break
        time.sleep(5)
    page.wait_for_timeout(2000)
    page.get_by_text("Representatives", exact=True).click()
    page.wait_for_timeout(6000)
    # Heights are tuned to end just past the last row of content. The
    # dataframe reserves space for more rows than these runs produce, so
    # an untuned crop trails a few hundred pixels of empty table.
    crop_results_panel(page, OUT / "03-cluster.png", max_height=445)
    print("captured cluster ->", OUT / "03-cluster.png")

    # --- Dock: stage all cluster representatives, upload both embedded
    # ligands, run, and wait for the *complete* state (the panel shows
    # partial results while still running, which is easy to mistake
    # for done). ---
    add_btn = page.get_by_role("button", name="Add all")
    print("staging:", add_btn.first.inner_text())
    add_btn.first.click()
    page.wait_for_timeout(2500)
    page.get_by_text("Dock", exact=True).first.click()
    page.wait_for_timeout(3000)

    scratch = pathlib.Path(os.environ.get("CLAUDE_JOB_DIR", "/tmp")) / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    ligand_paths = []
    for name, sdf in (("cbt.sdf", CBT_SDF), ("sulbactam.sdf", SULBACTAM_SDF)):
        q = scratch / name
        q.write_text(sdf)
        ligand_paths.append(str(q))
    finput = None
    for _ in range(10):
        if page.locator('input[type="file"]').count() > 0:
            finput = page.locator('input[type="file"]').first
            break
        page.wait_for_timeout(3000)
    if finput is None:
        print("ERROR: ligand file input never mounted; aborting Dock capture", file=sys.stderr)
        return
    finput.set_input_files(ligand_paths)
    dock_btn = stage_submit_button(page, "Dock")
    t0 = time.time()
    while time.time() - t0 < 120:
        if dock_btn.count() > 0 and not dock_btn.first.is_disabled():
            break
        time.sleep(3)
    dock_btn.first.click()
    print("submitted docking job")
    # Two ligands against twelve receptors is 24 pairs, so this
    # waits longer than the single-ligand capture used to.
    t0 = time.time()
    while time.time() - t0 < 25 * 60:
        if page.get_by_text("New docking run", exact=True).count() > 0:
            break
        time.sleep(6)
    page.wait_for_timeout(2000)
    crop_results_panel(page, OUT / "04-dock.png", max_height=370)
    print("captured dock ->", OUT / "04-dock.png")


def cmd_full_run() -> int:
    with sync_playwright() as p:
        context, page = new_page(p)
        goto_app(page, BASE)
        run_find_pockets(page)
        run_cluster_and_dock(page)
        context.close()
    return 0


def cmd_resume(session_url: str) -> int:
    """Cluster + Dock against a session whose Find Pockets job already ran.

    Saves re-running the pocket search when only the later stages need
    recapturing. Pass the full `?s=…&edit=…` URL.
    """
    with sync_playwright() as p:
        context, page = new_page(p)
        goto_app(page, session_url)
        page.wait_for_timeout(4000)
        run_cluster_and_dock(page)
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
    elif cmd == "resume":
        if len(sys.argv) < 3:
            print("resume needs a session URL with its edit= token", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(cmd_resume(sys.argv[2]))
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        raise SystemExit(1)
