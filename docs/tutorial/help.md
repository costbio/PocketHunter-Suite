# PocketHunter Suite help

## What this page is

This page explains how to read what the server hands back. It assumes
you have a result on screen — or are looking at the worked example
below — and want to know what a number means, how much weight it will
carry, and where it stops being evidence.

The [tutorial](/app/static/tutorial/index.html) is the other half: it
walks the pipeline stage by stage, from uploading a trajectory to
downloading a pose, and documents every control. Start there if you have
not run anything yet. Come back here when you have output in front of
you.

Everything below describes the service running at
`pockethunter.bio-cloud.site`. No account, no registration, no email
address — the service never asks for one.

## Try it before you upload anything {: #sample }

Two things are published so you can see the whole thing work without
preparing a file of your own.

**A finished analysis you can click through.** This link opens a real
session, in view-only mode, with all three stages already run:

- [Worked example — TEM-1 β-lactamase](https://pockethunter.bio-cloud.site/?s=t610wnb-_kc)

It is not a recording or a set of screenshots. It is the ordinary
result page, driven by the ordinary code, on data the ordinary pipeline
produced; the only difference from your own session is that you cannot
launch new jobs inside it. Switch stages with the segmented control,
click a row to paint that pocket in the viewer, drag the frame slider,
open the Downloads expanders and take the files. What you see is what
your own run will look like.

**The input files.** The same two files the example ran on:

- [`topology.pdb`](/app/static/example/topology.pdb) — 104,845 bytes
- [`trajectory.xtc`](/app/static/example/trajectory.xtc) — 500,352 bytes

Download them to check what the uploaders expect before you prepare
your own. The topology is a single 263-residue chain; the trajectory
holds 98 conformations of it. If your own pair loads in `mdtraj` with
`md.load_xtc(traj, top)` and returns the frame count you expect, it will
load here.

You can also start a fresh copy of this analysis yourself: the landing
page's **Try with example trajectory** button creates a new session,
copies those two files into it and queues the pocket search, with no
input from you.

<div class="scope" markdown="1">
<p class="scope-label">READ THIS PAGE IF</p>

- A `probability` of 0.63 is on your screen and you want to know what it claims.
- You are about to put a docking score in a figure.
- You need the licence, the retention schedule, or the DOIs to cite.

<p class="scope-label negative">READ THE TUTORIAL INSTEAD IF</p>

- You have not run anything yet and want the guided path.
- You are looking for a specific control and what it does.
- You hit an error message and want the fix.
</div>

## Reading the pocket table {: #pockets }

The Find pockets stage returns one row per pocket per frame. The
worked example produces 778 of them from 98 conformations — roughly
eight detections in each.

| Column | What it is | How to read it |
| --- | --- | --- |
| `Frame` | 0-based index of the conformation this pocket was found in | Use it directly against your own trajectory: `traj[57]` in mdtraj is the structure behind `Frame 57` |
| `pocket_index` | p2rank's rank within that one frame | 1 is that frame's top-scoring pocket. Not comparable across frames |
| `probability` | p2rank's ligand-binding score, 0 to 1 | Higher means p2rank found the local geometry and chemistry more like a known ligand-binding site |
| `num_residues` | How many residues line the pocket | Very small pockets are usually noise; under three residues the viewer cannot even draw a surface |
| `Confidence` | A badge binned from `probability` | High from 0.70, Medium from 0.40, Low below |

**The single most common misreading is treating the row count as a
pocket count.** It is a detection count. A groove that stays open across
forty frames produces forty rows, and nothing at this stage knows they
describe one place. The example's 778 rows correspond to a handful of
actual sites, which is what the next stage exists to work out.

**`probability` is a similarity score, not a probability of anything
happening.** p2rank was trained to recognise surfaces resembling known
binding sites, so 0.9 means "this looks a great deal like sites that
bind ligands", not "a ligand binds here 90% of the time". Read it as a
ranking device: the top of the list is where to look first, and the
absolute value is not a quantity to quote.

Useful signal, on the other hand, is **how often a site appears**. A
pocket that scores 0.65 in eighty of your frames is a more interesting
prospect than one that scores 0.95 in a single frame, because the first
is a feature of the protein's accessible conformations and the second
may be one distorted structure. The pocket table cannot show you that;
clustering can.

## Reading the clusters {: #clusters }

Clustering answers the question the pocket table cannot: which of those
per-frame detections are the same site?

Each surviving pocket becomes a binary vector over every pocket-lining
residue seen anywhere in the run — 1 where that residue lines this
pocket, 0 where it does not — and DBSCAN groups them with a Hamming
metric.

**A cluster is a recurring residue signature, not a region of space.**
The vectors hold no coordinates. Two pockets land together because the
same residues line both, whatever the geometry did in between. This is
usually what you want for a conformational ensemble, and it is worth
knowing when a result surprises you.

| Column | How to read it |
| --- | --- |
| `cluster` | An arbitrary label. **Not stable across runs** — re-cluster the same pockets and the same groups can come back numbered differently. Identify a cluster by its residues or its representative's frame |
| `Frame` | Which conformation the representative came from, in your trajectory's own numbering |
| `Location` | The residues lining the representative, as chain and residue numbers. This is the cluster's identity |
| `probability` | The representative's own p2rank score |
| `num_residues` | Size of the representative pocket |

The representative is the cluster's medoid — the member with the
smallest summed Hamming distance to the rest — so it is the most typical
member, not the best-scoring one. Pockets DBSCAN cannot place go to
label `-1` and are dropped.

In the worked example the 778 detections reduce to three clusters, with
representatives from frames 57, 1 and 68, averaging 31 lining residues.
Open the **Heatmap** tab in the demo session to see the structure
directly: one strip per cluster, one row per pocket, one column per
residue, a filled cell meaning that residue lines that pocket. Blocks
that look alike are the thing to be sceptical about.

**Treat the clustering as a proposal.** The `eps` and `min_samples` are
chosen by a sweep that scores candidates with `silhouette_score` under
euclidean distance, while the grouping itself used Hamming — the winner
is the best fit under a ruler that is not the one that did the cutting,
and noise points are scored as though they were a cluster. So check the
heatmap rather than trusting the numbers, and if two clusters look like
one site split in half, re-run at a different minimum probability.

## Reading the docking scores {: #docking }

The Dock stage returns a grid: one row per ligand, one column per
receptor conformation, and four ranking columns on the right.

**Every cell is a predicted binding affinity in kcal/mol, and more
negative is better.** −9.2 beats −6.4. The cell holds the best pose of
that one ligand against that one receptor — smina generates up to
**Number of poses** modes and the grid keeps the lowest.

Column headers read `C<cluster>_F<frame>`, so `C2_F68` is the
representative of cluster 2, taken from frame 68 of your trajectory.

The four right-hand columns collapse a row to one number, and they
answer different questions:

| Metric | What it rewards | Reach for it when |
| --- | --- | --- |
| **Mean** | Consistent scoring across the whole ensemble | You want a general-purpose ranking |
| **Median** | The same, but ignoring outliers | One receptor scores oddly and you do not want it steering the result |
| **Best** | The single most-negative cell in the row | You are hunting a ligand that fits one rare open conformation and nothing else — the point of docking an ensemble at all |
| **ECR** | Agreement across receptors, by rank rather than value | Receptors disagree on absolute scale. Higher is better here, unlike the other three |

Mean, Median and Best stay in kcal/mol. ECR is unit-free: it ranks the
ligands separately on each receptor, then sums `exp(−rank/σ)` across
receptors, so a receptor whose scores are all shifted cannot drag the
consensus with it.

### What these numbers will not support

Docking scores are the most over-read output this service produces, so
it is worth being blunt about the ceiling.

**A score is not a binding affinity you can quote.** Vinardo and Vina
scoring functions correlate with measured affinity loosely at best;
their reliable use is *ranking* candidates under identical conditions,
not predicting a Kd. Two ligands scored against the same receptor set
can be compared. A single number cannot be converted into anything
experimental.

**Compare within a run, not between runs.** Change the scoring function,
the box, the protonation pH or the receptor set and the numbers move.
Rankings survive that better than values do.

**A good score is not a pose you have checked.** Click the row, load
the pose in the viewer, and look at it. Scores are assigned to
geometries that are sometimes physically silly — a ligand threaded
through a gap, or floating in a box corner. The viewer is there for
this.

**Receptor quality bounds everything.** If the conformations came from
a backbone-only model, as the worked example's do, there are no side
chains for a ligand to pack against and the scores are a demonstration
that the pipeline runs rather than a statement about the molecules. The
example's grid shows CBT scoring better than sulbactam on all three
receptors; that is a pleasing outcome and not evidence of anything. Your
own all-atom trajectories carry no such caveat.

### The counters under the grid

The completion line reads like `6/6 pairs · 6/6 ligand-receptor pairs
scored`. The two count different things: how many docking jobs ran, and
how many cells came back with a number. They part company in two
situations, and only one is a failure.

- **A pair failed.** A warning appears above the grid, an expander names
  each failed pair with its exception, and a log is downloadable. An
  empty cell here means no usable pose, not a bad score.
- **Two of your pockets came from the same frame.** One frame is one PDB
  file and the grid keys its columns on the receptor file, so both get
  docked but only one column collects a result. Its twin sits empty with
  no warning and no log, because nothing failed. If a column is empty on
  a run that reported no failures, check the `Frame` labels of the
  pockets you selected.

## Reading the viewer {: #viewer }

The left-hand column holds one Mol\* viewer for the whole session, and
it does not reset when you switch stages — whatever you clicked last
stays on screen.

Clicking a row always beats dragging the slider. A pocket row, a cluster
representative or a docking receptor carries the filename of the
structure it came from, so selecting it loads that exact structure. The
slider is for looking around.

**The slider's number is not the `Frame` column.** It reads `Frame n /
N`, where `N` counts the models baked into the viewer file and `n` is
your 1-based position among them. While every extracted structure is
baked, slider `n` is the row labelled `(n − 1) × stride`. Above 200
structures the viewer thins the set with a stride of its own and no
simple arithmetic relates the two — click a row when you need a
specific pocket.

## Getting your results out {: #downloads }

Everything on screen has a download behind it, built on demand from what
the job wrote to disk. The full inventory is in the
[tutorial's downloads table](/app/static/tutorial/index.html#reading-the-results);
in short: `pockets.csv` for every detection,
`cluster_representatives.csv` for one row per cluster, a full per-pose
CSV and a best-pose-per-pair CSV for docking, plus ZIPs of the
structures, the poses, and receptor-and-pose complexes.

Archives are assembled against a 512 MB budget, smallest file first. One
that hits the budget is still delivered, with a caption counting what
was left out — download per cluster rather than in bulk when you see
that.

## Your session, and how long it lasts {: #sessions }

There is no sign-up. Starting an analysis mints a URL of the form
`?s=<code>&edit=<secret>`, and that URL is the only credential the
session has. Nothing is emailed. A lost `edit=` token cannot be
recovered, so bookmark it.

Sharing is by URL. The `?s=` half alone is a view-only link — recipients
see every result and can download everything, but cannot launch jobs or
change anything. The masthead gives you both links as copy buttons. The
demo session linked at the top of this page is exactly such a view-only
link.

Retention, all UTC:

| What | When it goes |
| --- | --- |
| A session that never ran a job | 15 minutes after creation |
| Job files (uploads and results) | 30 days after they were last touched |
| Oldest jobs of a session over 5 GB | At quarter past each hour, until it fits |

Job rows outlive their files, so history can list a run whose artefacts
have gone. Download what matters rather than treating a bookmark as
storage.

## Privacy and cookies {: #privacy }

**Your data is yours.** Uploads and results are readable only through
your session URL. Nothing is shared, published, or shown to other users,
and no one else can enumerate your session. Files are deleted on the
schedule above.

**No accounts, ever.** The service asks for no email address, no
registration and no login, and there is no guest login either.

**One cookie, and only if you say yes.** A banner asks before anything
persistent is written. The single cookie, `ph_recent_sessions`, holds
the list of analyses this browser has opened so the landing page can
show them to you; it lives a year, stays in your browser and never
reaches the server. Decline and everything still works — you just keep
your own links. Your answer is remembered in `ph_cookie_consent` so you
are not asked again.

**No third parties.** No tracking cookies, no analytics, no external
fonts or scripts. Every asset on every page is served from this domain,
so opening these pages tells nobody but us that you did.

## Licence and citing {: #licence }

PocketHunter Suite and the PocketHunter pipeline are released under the
**MIT licence** — free to use, copy, modify, redistribute and self-host,
academically or commercially, provided the copyright notice travels
along. Source:
[PocketHunter-Suite](https://github.com/costbio/PocketHunter-Suite) ·
[PocketHunter](https://github.com/costbio/PocketHunter).

The external tools keep their own terms: p2rank is MIT, Mol\* is MIT,
and SMINA has its own. Check those before redistributing a bundle that
includes them.

If this service contributed to something you publish, most of the work
was done by other people's tools — the
[tutorial's citing section](/app/static/tutorial/index.html#citing)
lists each with its DOI: p2rank for the pocket detection, SMINA and
Vinardo for the docking, Mol\* for the viewer, and the paper behind
whichever ranking metric you quote.

## When something goes wrong {: #problems }

A failed job renders one card: a red headline, a suggestion, a **Show
full error details** expander with the raw exception, and a
**Download error log** button when the job wrote one. Read the headline,
then open the details — the headline is a category and the exception is
the fact.

The [tutorial's troubleshooting section](/app/static/tutorial/index.html#troubleshooting)
carries the full table of headlines with causes and fixes, including the
stage-specific messages for zero pockets, zero clusters, and partial
docking failures.

Two things that are not failures and read like them: *You already have
1/1 docking job(s) running in this session* and *The docking pool is
busy* are concurrency caps and clear on their own.

If you have found a genuine bug — a wrong number, a crash, a result you
can show is incorrect — the error log plus the session's short code is
what makes it fixable. Send both.

## Contact {: #contact }

The service is built and run by the
[COSTBIO group](https://costbio.github.io) at Gebze Technical
University. Questions, bug reports and feature requests go to the group
via that page; bugs are best filed as issues on
[the repository](https://github.com/costbio/PocketHunter-Suite/issues),
where they are visible and get tracked.
