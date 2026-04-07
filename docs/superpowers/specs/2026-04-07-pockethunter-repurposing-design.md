# PocketHunter Repurposing Design Spec
**Date:** 2026-04-07  
**Target publication:** Nucleic Acids Research — Web Server Issue  
**Repos:** `PocketHunter` (CLI) · `PocketHunter-Suite` (Streamlit web app)

---

## 1. Scientific Story

PocketHunter helps computational biologists answer: **"Which protein conformation from my MD trajectory should I use for virtual screening?"**

The tool:
1. Clusters MD trajectory frames by pocket geometry (p2rank + DBSCAN/hierarchical, already built)
2. Generates a residue-based pharmacophore for each cluster representative
3. Scores user-provided active and decoy ligands against each pharmacophore
4. Ranks conformations by their ability to discriminate actives from decoys (ROC-AUC, EF1%, EF5%)
5. Lets users download the top-ranked representative PDB(s) for use in any external docking tool

**NAR framing:** No other publicly accessible web server combines MD trajectory pocket clustering with active/decoy discrimination to guide conformation selection for virtual screening.

---

## 2. What Changes

### Removed
- Docking page (`docking_app.py`) removed from the Streamlit navigation in `main.py`
- Docking-related session state keys cleaned up from `session_state.py`
- Docking step removed from the Full Pipeline page (`pipeline_app.py`)
- AutoDock Vina invocation stays in CLI (`tasks.py`, `PocketHunter/`) for internal validation use only — not exposed on the public server

### Added
- `PocketHunter/discriminate.py` — core pharmacophore scoring logic (CLI)
- `discriminate` subcommand in `PocketHunter/pockethunter.py`
- `discrimination_app.py` — new Streamlit page
- Celery task `run_discrimination_task` in `tasks.py`
- Representative PDB download (ZIP) on the discrimination results page

---

## 3. Pharmacophore Scoring — Technical Design

### 3a. Pocket Pharmacophore Generation
Input: a cluster representative PDB + the pocket residue IDs (already in `cluster_representatives.csv` from p2rank)

Residue → pharmacophore feature mapping (pure lookup, no external tools):

| Feature | Residues |
|---|---|
| H-bond donor | Ser, Thr, Tyr, Asn, Gln, Lys, Arg, His, Trp |
| H-bond acceptor | Asp, Glu, Asn, Gln, Ser, Thr, Tyr, His |
| Hydrophobic | Ala, Val, Ile, Leu, Met, Phe, Trp, Pro |
| Aromatic | Phe, Trp, Tyr, His |
| Positive charge | Lys, Arg, His |
| Negative charge | Asp, Glu |

Result: a 6-dimensional feature-count vector per pocket (one value per feature type, counting residues contributing to it).

### 3b. Ligand Feature Extraction
- Parse SDF file → RDKit mol objects (handle 3D if present; generate ETKDG conformer if 2D)
- Extract pharmacophore features using RDKit `MolChemicalFeatures` with the built-in `BaseFeatures.fdef`
- Result: a 6-dimensional feature-count vector per ligand

### 3c. Complementarity Scoring
Score(ligand, pocket) = cosine similarity between the ligand feature vector and pocket feature vector.

Cosine similarity is chosen because it is magnitude-independent (a large ligand matching a large pocket does not inflate the score vs. feature type overlap).

### 3d. Discrimination Metrics
For each conformation (cluster representative):
- Pool actives + decoys, sort by Score descending
- Compute **ROC-AUC**
- Compute **EF1%** and **EF5%** (enrichment factor at 1% and 5% of the ranked list)

Output per conformation: `{conformation_id, cluster_id, ROC-AUC, EF1%, EF5%, representative_pdb_path}`

---

## 4. New CLI Subcommand

```bash
python pockethunter.py discriminate \
  --cluster_dir path/to/pocket_clusters \
  --actives actives.sdf \
  --decoys decoys.sdf \
  --outfolder path/to/discrimination_output
```

Output files:
- `discrimination_results.csv` — one row per conformation, columns: cluster_id, ROC-AUC, EF1%, EF5%, pdb_path
- `roc_curves.png` — overlaid ROC curves, one per conformation
- `top_representatives/` — folder of top-ranked representative PDB files

---

## 5. New Streamlit Page: Discrimination Analysis

**Placement:** New tab/page in `main.py` option menu, after "Cluster Pockets". The docking page is removed.

**Inputs:**
- Job ID from clustering step (dropdown, same as current cluster → docking handoff pattern)
- Actives SDF file upload (max 200 molecules, validated)
- Decoys SDF file upload (max 2000 molecules, validated)

**Processing:**
- Celery task `run_discrimination_task` runs `discriminate.py` logic
- Async status polling (same pattern as existing tasks)

**Results display:**
- Ranked conformations table (sortable by ROC-AUC, EF1%, EF5%)
- ROC curve per conformation overlaid on a single Plotly chart
- 3D viewer (py3Dmol) for the selected top-ranked representative, with pocket residues highlighted
- Download button: ZIP of top-N representative PDBs (user selects N, default 3)

---

## 6. File I/O

| File | Description |
|---|---|
| `actives.sdf` / `decoys.sdf` | User upload, stored in `UPLOAD_DIR/{job_id}/` |
| `discrimination_results.csv` | Per-conformation metrics, in `RESULTS_DIR/{job_id}/discrimination/` |
| `top_representatives/*.pdb` | Top-ranked PDBs, in `RESULTS_DIR/{job_id}/discrimination/top_representatives/` |
| `roc_curves.png` | ROC figure, same dir |

---

## 7. Validation (Paper Only — Not a Tool Feature)

Validation is performed by the authors offline and reported in the paper. It is not a user-facing feature.

**Benchmark system:** 1-2 targets from DUD-E (CDK2 recommended). Authors run their own MD simulations, apply the full PocketHunter pipeline, then:

1. **Experimental ground truth:** Compare top-ranked conformation's pocket against PDB crystal structures with bound actives (pocket residue Cα RMSD). Claim: PocketHunter recovers the experimentally relevant conformation.

2. **Docking correlation:** Run Vina offline against each cluster representative, compute docking-based ROC-AUC per conformation, compare ranking to pharmacophore-based ranking (Spearman ρ). Claim: pharmacophore scoring approximates docking-based ranking at negligible compute cost.

---

## 8. Resource Limits (Public Server)

| Limit | Value | Reason |
|---|---|---|
| Max actives | 200 molecules | Pharmacophore scoring is fast; 200 is generous |
| Max decoys | 2000 molecules | DUD-E decoy sets are typically ~1000-2000 per target |
| Max conformations scored | All cluster representatives (typically <20) | No bottleneck |
| No docking on server | — | Removed from public UI |

---

## 9. Branch Strategy

All implementation goes on new branches:
- CLI repo (`PocketHunter`): branch `feature/discrimination`
- Streamlit repo (`PocketHunter-Suite`): branch `feature/discrimination`

The `PocketHunter/` subdirectory inside `PocketHunter-Suite` is a copy; keep both in sync manually when CLI changes are made.

---

## 10. Out of Scope

- Automatic active/decoy fetching from ChEMBL or PubChem
- Docking on the public server
- Multi-target batch analysis
- User accounts / result persistence beyond session
