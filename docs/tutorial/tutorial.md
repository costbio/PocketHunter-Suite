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

## Cluster

## Dock
