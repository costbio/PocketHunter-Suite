# PocketHunter Suite tutorial

## Welcome

Find transient, druggable pockets across a molecular-dynamics trajectory —
then dock ligands against them.

A pocket that's wide open in one frame can be shut in the next. Run pocket
detection on a single crystal structure and you see one snapshot of that
motion — the cryptic site that opens for a handful of frames simply isn't
there. This service takes the trajectory instead. It extracts every tenth
frame by default, runs p2rank on each extracted structure, groups the hits
that share the same lining residues into clusters, and docks your ligands
into the cluster representatives with SMINA. What you hand it is an `.xtc`
and a topology; what you get back is a ranked pocket list with the frames
each pocket appeared in, plus binding scores for the ligands you tried.

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
<p class="scope-label">FOR YOU IF</p>

- You have an MD trajectory and want the pockets a single static structure would miss.
- You want those pockets ranked and grouped rather than one hit per frame.
- You want to dock ligands against the pockets you select.

<p class="scope-label negative">NOT FOR YOU IF</p>

- You have one static structure — run p2rank directly instead.
- You need covalent docking, or docking into a membrane or nucleic-acid site.
- You need a guaranteed turnaround; this is a shared, quota-limited service.
</div>

<dl class="facts">
  <dt>Worked example</dt>
  <dd>MEASURED_EXAMPLE</dd>
  <dt>You end up with</dt>
  <dd>Ranked pockets per frame · cluster representatives · SMINA scores · downloadable poses</dd>
</dl>

## Upload your files {: #upload }

The Find pockets panel opens with an **Input source** radio button. Pick
*From trajectory (XTC + topology)* and two uploaders appear: **Trajectory
(.xtc)** takes a GROMACS `.xtc` and nothing else, while **Topology (.pdb,
.gro)** takes either extension, so a `.gro` reference structure works just
as well as a PDB. Under them sits **Frame stride**, an integer of at least
1 that starts at 10 — raise it for a long trajectory, drop it to 1 to
search every frame. Pick *From PDB ZIP archive* instead and you get a
single **PDB structures (.zip)** uploader; extraction is skipped and
p2rank runs straight over the structures in the archive.

Two size limits apply, and the tighter one is the browser's. Streamlit's
uploader refuses a file over 200 MB before it ever reaches the server, so
200 MB per file is the number that actually binds; the server-side
validator behind it is set to 500 MB (`MAX_UPLOAD_SIZE=524288000`). A ZIP
clears two further checks before anything is unpacked: its compression
ratio has to stay at or below 100:1, and its contents have to come to less
than 1 GB uncompressed (`MAX_ZIP_SIZE=1073741824`). The extension
allowlist is short — `.xtc`, `.pdb`, `.gro`, `.csv`, `.zip`, `.sdf`,
`.pdbqt` — and anything else is rejected on its name before it lands on
disk.

No trajectory to hand? The landing page carries a **Try with example
trajectory** button beside **Start new analysis**. It copies the bundled
trypsin demo into a fresh session and queues the pocket search without
asking you for anything: `examples/trypsin/topology.pdb` is 104,845 bytes
and `examples/trypsin/trajectory.xtc` is 500,352 bytes. The deployed
instance reads that pair from `/app/examples/trypsin`, set as
`EXAMPLE_TRAJECTORY_DIR`; when the directory is missing the button hides
itself rather than failing on you.

## Sessions {: #sessions }

There is no sign-up. Pressing **Start new analysis** mints a workspace and
lands you on `https://pockethunter.bio-cloud.site/?s=<code>&edit=<secret>`,
where `s` is an 11-character short code and `edit` is a 32-character
secret, both drawn from `secrets.token_urlsafe`. That URL is the only
credential the session has — nothing gets emailed to you, and a lost
`edit=` token can't be recovered. Bookmark it, then start a job before you
wander off; an unused session does not survive its first cleanup sweep.

Which half of the URL you're holding decides what you can do. With a
matching `edit=` token the masthead chip reads **✏️ Editor** and every
control works; with only `?s=` it reads **👁 Viewer**, and the uploaders
and the buttons that launch jobs all come up greyed out. A viewer still
gets the whole read side: the Mol\* structure, the pocket tables, the
cluster assignments, the docking scores. An editor also gets two copy
buttons beside the chip, labelled *view-only link* and *editor link*; a
viewer sees only the first, having no token to hand out. Send colleagues
the view-only link unless you mean for them to run jobs on your session.

What the session has done persists on the server. Job rows live in
Postgres and their artefacts under `results/<job_id>/`, so reopening the
URL next week brings the pocket tables and the poses back with nothing
recomputed. Your browser keeps its own list of the sessions you've
visited, in a cookie named `ph_recent_sessions` — that list is client-side
only and never reaches the server, so clearing cookies loses your session
links unless you saved them somewhere else.

**A bookmark is not storage.** A session that has never submitted a job is
deleted 15 minutes after it was *created* (`SESSION_GRACE_MINUTES=15`),
by a `cleanup-abandoned-sessions` task that runs every five minutes. That
task reads creation time, not last activity, so keeping the tab open buys
you nothing. Make a session, run something. Bookmark the URL, come back an
hour later having started nothing, and there is nothing to come back to.

Sessions that did run work live far longer, on two slower timers, both
UTC. Job directories untouched for 30 days go at 02:00 daily
(`CLEANUP_AFTER_DAYS=30`), uploads and results together. At 15 past every
hour a third task measures each session's disk usage and, for any session
over 5 GB (`PER_SESSION_DISK_QUOTA_MB=5000`), deletes its oldest jobs
until it fits again — the job rows survive, so the history still lists a
run whose files have gone.

## Find pockets

p2rank looks at one structure at a time, so the suite hands it every frame
in turn. Extraction runs first, with your stride applied; then `prank
predict` runs over a list of the extracted structures on four threads
(`P2RANK_THREADS=4`, not exposed in the UI); then the per-frame
prediction files are merged into
one `pockets.csv` with five columns — `File name`, `Frame`,
`pocket_index`, `probability`, `residues`.

**A row is one pocket in one frame. It is not one site.** A groove that
stays open across forty frames produces forty rows, each with its own
`pocket_index` and its own slightly different residue list, and nothing
at this stage knows they describe the same place. The count in the stats
strip is therefore a count of detections: ninety frames returning ten
hits apiece is 900 rows and possibly ten actual pockets. Turning those
rows back into sites is the next stage's entire job.

Take the `Frame` number for what it is, because **it is not an index into
your trajectory.** The label is the stride times the structure's 1-based
position among the extracted structures, so at stride 10 the structures
come out labelled 10, 20, 30 — while the conformations inside them are
your trajectory's frames 0, 10, 20. Every label sits one full stride
ahead of the frame it was cut from. The labels agree with each other, so
comparing pockets between frames inside the app is unaffected; it is the
trip back to your own XTC that needs the correction. Subtract one stride
before you go looking at a conformation.

`probability` is p2rank's ligand-binding score for that pocket, and the
panel bins it into three badges — High from 0.7 up, Medium from 0.4, Low
below. The strip above the table gives the detection count, the mean
probability, how many rows cleared 0.7, and the single best score. Two
controls narrow the view: a **Min probability** slider running 0.0 to 1.0
in steps of 0.05 and starting at 0.0, and a **Confidence** multiselect
that starts with all three badges ticked. Both filter the table and
nothing else. No job re-runs, no row is deleted, and the caption
underneath keeps reporting how many of the total are on screen.

Click a row and the Mol\* viewer paints that pocket's residues and jumps
to the frame it was found in. A pocket with fewer than three residues
gets a warning instead of a surface — below three points there is no mesh
worth drawing. Ctrl- or shift-click to take several rows at once and an
**Add N selected → docking** button appears under the table: pockets can
go straight from here into the docking selection without being clustered
at all, which is the right move when you already know which site you
care about.

The remaining two tabs are quieter. **Distribution** plots the
probability histogram and probability against residue count, the quickest
way to see whether a threshold you have in mind will keep everything or
nothing. **Downloads** offers `pockets.csv` and the subset at or above
0.7 on its own, plus a **Generate PDB archive** button that packs the
per-frame PDBs p2rank ran on into a ZIP you then download — the same
structures that become receptors when you dock.

Two outcomes get their own message rather than an empty table. Zero
pockets anywhere in the trajectory means detection finished and found
nothing, and the panel points at the stride and at a possible
topology/trajectory mismatch before anything else. More than 1,000
extracted frames (`MAX_TRAJECTORY_FRAMES=1000`) stops the job before
detection and names the stride to re-run with, because the viewer cannot
render a trajectory that long.

## Cluster

Clustering answers the question the pocket table cannot: which of those
per-frame detections are the same pocket? Pick a completed run from
**Source pockets**, and the stage reads its `pockets.csv`, drops every
row below **Min. ligand-binding probability** (slider 0.0–1.0 in 0.05
steps, starting at 0.5), and rewrites each surviving pocket as a binary
vector over the union of every pocket-lining residue seen anywhere in the
run — 1 where that residue lines this pocket, 0 where it does not.

**Those vectors hold no coordinates. Pockets are grouped by which
residues line them, not by where they sit.** DBSCAN runs with
`metric='hamming'`, so the distance between two pockets is the fraction
of residue slots on which they disagree. A pocket in frame 3 and a pocket
in frame 88 land together because the same residue identifiers line both,
whatever the geometry did in between. Read a cluster as a recurring
residue signature, not as a neighbourhood.

Each cluster's representative is its medoid — the member whose summed
Hamming distance to the rest of its cluster is smallest — written to
`cluster_representatives.csv`. Pockets DBSCAN cannot place go to label
`-1`, the noise bin, and are dropped from both the heatmap and the
representatives table.

**The `eps` and `min_samples` a run settles on are not optimal, and
nothing in the pipeline claims they are.** `PocketHunter/pockethunter.py`
sweeps `eps` from `1/num_residues` toward `10/num_residues` in 0.005
steps and `min_samples` upward from 2 % of the frame count (0.5 % above
100 frames), stopping short of 20 % of it, fits DBSCAN at every
combination with the Hamming metric, and keeps whichever fit scored
highest on `silhouette_score(df, labels)`. That scoring call sits in
`optimized_dbscan`, the same loop that does the fitting, and it uses
scikit-learn's default euclidean distance rather than the Hamming
distance that formed the groups, and it receives the noise rows as well,
scored as though `-1` were a cluster like any other. The winner is the
best fit under a ruler that is not the one that did the cutting. So treat
the output as a proposal and check it: open the Heatmap and confirm that
each block's residue signature really is distinct from its neighbours',
and if two clusters look like one pocket split in half, re-run at a
different `min_prob` rather than assuming the choice was made for you.

Results open on **Heatmap** — one strip per cluster, one row per pocket
labelled `p=… · F=…`, one column per residue, a filled cell meaning that
residue lines that pocket. Clicking a row pushes the pocket to the viewer
and jumps to its frame. **Clustered pockets** is the same information as
a table. **Representatives** lists one row per cluster and, where
hierarchical refinement ran, a K spinner per cluster: K=1 keeps the
DBSCAN medoid, K of 2 or more re-cuts that cluster's dendrogram into that
many sub-representatives, up to ten or the member count, whichever is
smaller. **Downloads** holds the CSVs.

**Add all N cluster representatives → docking**, at the foot of the
Representatives tab, is the normal handoff to the next stage — one
receptor per displayed row, sub-cluster representatives grouped under
their DBSCAN parent.

When DBSCAN finds nothing the panel says so and names the two usual
causes: `min_prob` filtered out too much, or too few pockets survived to
form a dense group. Lowering the threshold and re-running Find pockets at
a smaller stride are the fixes. Switching **Method** to hierarchical will
always return clusters, which is occasionally what you want and never
evidence that the clusters mean anything.

## Dock

Docking needs two things: a set of pockets and a set of ligands. Pockets
arrive in a bucket that carries across stages, filled either from **Add
all N cluster representatives** or from rows you ticked in the pocket
table. Ligands come through one uploader taking `.pdbqt`, `.sdf`, `.pdb`
and `.zip`, several files at a time; SDF and PDB inputs are split
server-side into one PDBQT per molecule with OpenBabel. Leave **Generate
3D coordinates** off unless your input genuinely is 2D — curated
libraries already carry coordinates, and the option costs minutes.

The **smina parameters** expander holds three controls. **Scoring
function** defaults to `vinardo`, with `vina`, `ad4_scoring` and
`dkoes_scoring` also on offer. **Number of poses** is smina's
`--num_modes`, 1 to 50, default 10 — how many binding modes are kept per
ligand-receptor pair. **pH (protonation)** runs 4.0 to 10.0 in 0.1 steps,
default 7.4, and is the pH OpenBabel protonates the receptor at before
writing it as PDBQT. Exhaustiveness is deliberately absent: it is pinned
server-side at `DOCKING_EXHAUSTIVENESS=8` with no slider, and the `.env`
comment beside it says as much.

You never draw a box. For each pocket the task takes the coordinates of
that pocket's lining residues, pads their bounding box by 2 Å on every
side, and clamps each edge into the range 10 Å to 25 Å — tight enough to
keep the search on the pocket, capped so that an over-large p2rank hit
cannot quietly become a whole-protein blind dock.

**Every cell in the score grid is a predicted binding affinity in
kcal/mol, and more negative is better** — −9.2 beats −6.4. The cell holds
the best pose of that one ligand against that one receptor: smina
generates up to **Number of poses** modes and the grid keeps the lowest
affinity among them. Rows are ligands, columns are the receptor
conformations you selected. That shape is the payoff for having run a
trajectory at all — one ligand scored against an ensemble of
conformations rather than against a single crystal structure.

Four columns on the right collapse each row to one number, and **Rank
ligands by** decides which of them sorts the grid. **Mean** and
**Median** average that ligand's per-receptor affinities in kcal/mol,
lowest first; reach for Median when one receptor in the ensemble scores
oddly. **Best** takes the single most-negative cell in the row, which is
the reading that surfaces a ligand fitting one rare open conformation and
nothing else. **ECR** is Exponential Consensus Ranking: rank the ligands
separately on each receptor, then sum `exp(−rank/σ)` across receptors
with σ set to a tenth of the ligand count, floored at 1. It is unit-free
and higher is
better, and because it uses only ranks, a receptor whose scores are all
shifted cannot drag the consensus with it.

Click a row and the viewer loads that ligand's best pose in whichever
receptor the slider is on, with a download button for exactly that
complex — receptor PDB plus pose SDF. The Downloads expander carries the
rest: the full per-pose results CSV, a best-pose-per-pair CSV, a ZIP of
every pose SDF, and the receptors on their own.

Two gates apply before **Dock** will run. Molecules × pockets must come
to no more than 1,000 pairs (`DOCKING_MAX_PAIRS=1000`) — over that the
panel refuses the submission up front and tells you how many molecules
would fit — and a bucket holding more than 20 pockets (`MAX_DOCKING_PDBS`)
is trimmed to the 20 with the highest probability, with a warning shown
while you are still choosing.
