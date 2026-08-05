# PocketHunter Suite tutorial

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

## Find pockets

## Cluster

## Dock

## Before you start

Placeholder. Replaced in Task 4.
