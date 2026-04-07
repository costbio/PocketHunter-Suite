# Discrimination Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add pharmacophore-based active/decoy discrimination to PocketHunter so users can rank MD trajectory conformations by their ability to distinguish active ligands from decoys — and remove docking from the public Streamlit server.

**Architecture:** A new `discriminate.py` module in the CLI computes residue-based pharmacophore vectors, scores each ligand (active/decoy) via cosine similarity, and ranks cluster representatives by ROC-AUC and enrichment factor. The Streamlit app gets a new `discrimination_app.py` page backed by a Celery task, while docking is removed from navigation, session state, and the pipeline page.

**Tech Stack:** RDKit (pharmacophore features), scikit-learn (ROC-AUC), scipy (cosine distance), Celery, Streamlit, Plotly, py3Dmol

---

## Repo Paths

| Repo | Path |
|---|---|
| CLI | `/home/bogrum/workspace/my_projects/PocketHunter/` |
| Suite | `/home/bogrum/workspace/my_projects/PocketHunter-Suite/` |

The `PocketHunter/` subdirectory inside Suite is a manual copy of the CLI repo. Changes to the CLI must be synced there.

---

## File Map

**CLI repo — new/modified files:**
- Create: `PocketHunter/discriminate.py` — pharmacophore scoring logic (source of truth)
- Create: `PocketHunter/tests/test_discriminate.py` — unit tests
- Modify: `PocketHunter/pockethunter.py` — add `discriminate` subcommand
- Modify: `PocketHunter/requirements.txt` — add `rdkit`

**Suite repo — new/modified files:**
- Create: `PocketHunter/discriminate.py` — synced copy from CLI repo
- Create: `discrimination_app.py` — Streamlit page
- Modify: `tasks.py` — add `run_discrimination_task`
- Modify: `main.py` — swap docking page for discrimination page
- Modify: `session_state.py` — remove docking keys, add discrimination keys
- Modify: `pipeline_app.py` — remove all docking UI/logic
- Modify: `PocketHunter/requirements.txt` — add `rdkit`
- Modify: `requirements.txt` — add `rdkit`

---

## Task 1: Create Feature Branches

**Files:** (git operations only)

- [ ] **Step 1: Create branch on CLI repo**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
git checkout -b feature/discrimination
```

Expected: `Switched to a new branch 'feature/discrimination'`

- [ ] **Step 2: Create branch on Suite repo**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
git checkout -b feature/discrimination
```

Expected: `Switched to a new branch 'feature/discrimination'`

---

## Task 2: Add rdkit to Requirements

**Files:**
- Modify: `PocketHunter/requirements.txt`
- Modify: `PocketHunter-Suite/requirements.txt`
- Modify: `PocketHunter-Suite/PocketHunter/requirements.txt`

- [ ] **Step 1: Add rdkit to CLI requirements**

Append `rdkit` to `/home/bogrum/workspace/my_projects/PocketHunter/requirements.txt`:

```
pandas
matplotlib
tqdm
glob2
seaborn
scipy
numpy
nglview
mdtraj
scikit-learn
rdkit
```

- [ ] **Step 2: Add rdkit to Suite requirements**

Append to `/home/bogrum/workspace/my_projects/PocketHunter-Suite/requirements.txt` under `# Scientific Computing`:

```
rdkit>=2023.3.1
```

- [ ] **Step 3: Verify rdkit is importable in the current environment**

```bash
python -c "from rdkit import Chem; print('rdkit ok')"
```

Expected: `rdkit ok` (if not installed: `pip install rdkit`)

- [ ] **Step 4: Commit requirements changes on CLI repo**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
git add requirements.txt
git commit -m "chore: add rdkit dependency for pharmacophore scoring"
```

---

## Task 3: Write Failing Tests for `discriminate.py`

**Files:**
- Create: `PocketHunter/tests/__init__.py`
- Create: `PocketHunter/tests/test_discriminate.py`

- [ ] **Step 1: Create tests directory and init**

```bash
mkdir -p /home/bogrum/workspace/my_projects/PocketHunter/tests
touch /home/bogrum/workspace/my_projects/PocketHunter/tests/__init__.py
```

- [ ] **Step 2: Write the test file**

Create `/home/bogrum/workspace/my_projects/PocketHunter/tests/test_discriminate.py`:

```python
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from discriminate import (
    get_pocket_features,
    get_ligand_features,
    score_complementarity,
    compute_discrimination_metrics,
    FEATURE_KEYS,
)


# ── get_pocket_features ──────────────────────────────────────────────────────

def test_get_pocket_features_returns_all_keys():
    features = get_pocket_features(['A_1_ALA'])
    assert set(features.keys()) == set(FEATURE_KEYS)


def test_get_pocket_features_hydrophobic():
    # ALA, VAL are both hydrophobic
    features = get_pocket_features(['A_1_ALA', 'A_2_VAL'])
    assert features['hydrophobic'] == 2


def test_get_pocket_features_donor():
    # ARG is a donor
    features = get_pocket_features(['A_1_ARG'])
    assert features['donor'] >= 1


def test_get_pocket_features_acceptor():
    # ASP is an acceptor
    features = get_pocket_features(['A_1_ASP'])
    assert features['acceptor'] >= 1


def test_get_pocket_features_aromatic():
    # PHE is aromatic
    features = get_pocket_features(['A_1_PHE'])
    assert features['aromatic'] >= 1


def test_get_pocket_features_positive():
    # LYS is positive
    features = get_pocket_features(['A_1_LYS'])
    assert features['positive'] >= 1


def test_get_pocket_features_negative():
    # GLU is negative
    features = get_pocket_features(['A_1_GLU'])
    assert features['negative'] >= 1


def test_get_pocket_features_empty():
    features = get_pocket_features([])
    assert all(v == 0 for v in features.values())


def test_get_pocket_features_unknown_residue():
    # Unknown residue IDs should not raise; just contribute 0
    features = get_pocket_features(['A_1_UNK'])
    assert all(v >= 0 for v in features.values())


# ── get_ligand_features ──────────────────────────────────────────────────────

def test_get_ligand_features_returns_all_keys():
    from rdkit import Chem
    mol = Chem.MolFromSmiles('c1ccccc1')  # benzene
    mol = Chem.AddHs(mol)
    features = get_ligand_features(mol)
    assert set(features.keys()) == set(FEATURE_KEYS)


def test_get_ligand_features_aromatic_benzene():
    from rdkit import Chem
    mol = Chem.MolFromSmiles('c1ccccc1')
    mol = Chem.AddHs(mol)
    features = get_ligand_features(mol)
    assert features['aromatic'] >= 1


def test_get_ligand_features_donor_ethanol():
    from rdkit import Chem
    mol = Chem.MolFromSmiles('CCO')  # ethanol, has OH donor
    mol = Chem.AddHs(mol)
    features = get_ligand_features(mol)
    assert features['donor'] >= 1


def test_get_ligand_features_none_mol():
    features = get_ligand_features(None)
    assert all(v == 0 for v in features.values())


# ── score_complementarity ────────────────────────────────────────────────────

def test_score_complementarity_identical_vectors():
    pocket = {'donor': 2, 'acceptor': 0, 'hydrophobic': 0, 'aromatic': 0, 'positive': 0, 'negative': 0}
    ligand = {'donor': 2, 'acceptor': 0, 'hydrophobic': 0, 'aromatic': 0, 'positive': 0, 'negative': 0}
    score = score_complementarity(pocket, ligand)
    assert score == pytest.approx(1.0)


def test_score_complementarity_orthogonal_vectors():
    pocket = {'donor': 1, 'acceptor': 0, 'hydrophobic': 0, 'aromatic': 0, 'positive': 0, 'negative': 0}
    ligand = {'donor': 0, 'acceptor': 1, 'hydrophobic': 0, 'aromatic': 0, 'positive': 0, 'negative': 0}
    score = score_complementarity(pocket, ligand)
    assert score == pytest.approx(0.0)


def test_score_complementarity_zero_pocket():
    pocket = {k: 0 for k in FEATURE_KEYS}
    ligand = {'donor': 1, 'acceptor': 0, 'hydrophobic': 0, 'aromatic': 0, 'positive': 0, 'negative': 0}
    score = score_complementarity(pocket, ligand)
    assert score == pytest.approx(0.0)


def test_score_complementarity_range():
    pocket = {'donor': 2, 'acceptor': 1, 'hydrophobic': 3, 'aromatic': 1, 'positive': 0, 'negative': 0}
    ligand = {'donor': 1, 'acceptor': 2, 'hydrophobic': 1, 'aromatic': 0, 'positive': 1, 'negative': 0}
    score = score_complementarity(pocket, ligand)
    assert 0.0 <= score <= 1.0


# ── compute_discrimination_metrics ───────────────────────────────────────────

def test_compute_metrics_perfect_discrimination():
    scores_actives = [1.0, 0.9, 0.8]
    scores_decoys = [0.3, 0.2, 0.1]
    metrics = compute_discrimination_metrics(scores_actives, scores_decoys)
    assert metrics['roc_auc'] == pytest.approx(1.0)


def test_compute_metrics_random_discrimination():
    import random
    random.seed(42)
    scores_actives = [random.random() for _ in range(100)]
    scores_decoys = [random.random() for _ in range(100)]
    metrics = compute_discrimination_metrics(scores_actives, scores_decoys)
    assert 0.0 <= metrics['roc_auc'] <= 1.0


def test_compute_metrics_keys_present():
    metrics = compute_discrimination_metrics([0.9, 0.8], [0.2, 0.1])
    assert 'roc_auc' in metrics
    assert 'ef1' in metrics
    assert 'ef5' in metrics


def test_compute_metrics_ef1_perfect():
    # Perfect: all actives ranked first → EF1% should be high
    scores_actives = [1.0] * 10
    scores_decoys = [0.0] * 90
    metrics = compute_discrimination_metrics(scores_actives, scores_decoys)
    assert metrics['ef1'] > 1.0  # better than random
```

- [ ] **Step 3: Run tests to verify they all fail (module not found)**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
python -m pytest tests/test_discriminate.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'discriminate'` or similar — all tests fail.

---

## Task 4: Implement `discriminate.py`

**Files:**
- Create: `PocketHunter/discriminate.py`

- [ ] **Step 1: Create the module**

Create `/home/bogrum/workspace/my_projects/PocketHunter/discriminate.py`:

```python
"""
discriminate.py — Pharmacophore-based active/decoy discrimination.

Given a pocket_clusters directory (output of cluster_pockets) and two SDF files
(actives and decoys), scores each cluster representative by its ability to
discriminate actives from decoys using residue-based pharmacophore vectors and
cosine similarity. Outputs ranked conformations with ROC-AUC, EF1%, and EF5%.
"""

import os
import csv
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from rdkit import Chem, RDConfig
from rdkit.Chem import ChemicalFeatures

# ── Constants ────────────────────────────────────────────────────────────────

FEATURE_KEYS = ['donor', 'acceptor', 'hydrophobic', 'aromatic', 'positive', 'negative']

# Residue name → pharmacophore feature membership (a residue can belong to multiple)
_DONOR_RESNAMES = {'SER', 'THR', 'TYR', 'ASN', 'GLN', 'LYS', 'ARG', 'HIS', 'TRP'}
_ACCEPTOR_RESNAMES = {'ASP', 'GLU', 'ASN', 'GLN', 'SER', 'THR', 'TYR', 'HIS'}
_HYDROPHOBIC_RESNAMES = {'ALA', 'VAL', 'ILE', 'LEU', 'MET', 'PHE', 'TRP', 'PRO'}
_AROMATIC_RESNAMES = {'PHE', 'TRP', 'TYR', 'HIS'}
_POSITIVE_RESNAMES = {'LYS', 'ARG', 'HIS'}
_NEGATIVE_RESNAMES = {'ASP', 'GLU'}

# RDKit feature family → our feature key
_RDKIT_FEATURE_MAP = {
    'Donor': 'donor',
    'Acceptor': 'acceptor',
    'Hydrophobe': 'hydrophobic',
    'LumpedHydrophobe': 'hydrophobic',
    'Aromatic': 'aromatic',
    'PosIonizable': 'positive',
    'NegIonizable': 'negative',
}

# Build RDKit feature factory once at import time
_FDEF_PATH = os.path.join(RDConfig.RDDataDir, 'BaseFeatures.fdef')
_FACTORY = ChemicalFeatures.BuildFeatureFactory(_FDEF_PATH)


# ── Core Functions ───────────────────────────────────────────────────────────

def _extract_resname(residue_id: str) -> str:
    """
    Extract the 3-letter residue name from a p2rank residue ID.

    P2rank residue IDs are typically formatted as 'CHAIN_RESNUM_RESNAME'
    (e.g. 'A_1_ALA'). This function tries underscore-split first, then
    falls back to the first 3 alphabetic characters.
    """
    parts = residue_id.strip().split('_')
    for part in reversed(parts):
        clean = part.strip().upper()
        if len(clean) == 3 and clean.isalpha():
            return clean
    # Fallback: first 3 alpha chars
    alpha = ''.join(c for c in residue_id if c.isalpha())
    return alpha[:3].upper() if len(alpha) >= 3 else ''


def get_pocket_features(residue_ids: list) -> dict:
    """
    Compute pharmacophore feature counts for a pocket defined by residue IDs.

    Args:
        residue_ids: List of p2rank residue ID strings (e.g. ['A_1_ALA', 'A_2_ARG']).

    Returns:
        Dict with keys matching FEATURE_KEYS and integer counts.
    """
    features = {k: 0 for k in FEATURE_KEYS}
    for rid in residue_ids:
        resname = _extract_resname(rid)
        if not resname:
            continue
        if resname in _DONOR_RESNAMES:
            features['donor'] += 1
        if resname in _ACCEPTOR_RESNAMES:
            features['acceptor'] += 1
        if resname in _HYDROPHOBIC_RESNAMES:
            features['hydrophobic'] += 1
        if resname in _AROMATIC_RESNAMES:
            features['aromatic'] += 1
        if resname in _POSITIVE_RESNAMES:
            features['positive'] += 1
        if resname in _NEGATIVE_RESNAMES:
            features['negative'] += 1
    return features


def get_ligand_features(mol) -> dict:
    """
    Extract pharmacophore feature counts from an RDKit Mol object.

    Args:
        mol: RDKit Mol object. Returns all-zero dict if None.

    Returns:
        Dict with keys matching FEATURE_KEYS and integer counts.
    """
    features = {k: 0 for k in FEATURE_KEYS}
    if mol is None:
        return features
    try:
        feats = _FACTORY.GetFeaturesForMol(mol)
        for feat in feats:
            family = feat.GetFamily()
            key = _RDKIT_FEATURE_MAP.get(family)
            if key:
                features[key] += 1
    except Exception:
        pass
    return features


def score_complementarity(pocket_features: dict, ligand_features: dict) -> float:
    """
    Compute cosine similarity between pocket and ligand pharmacophore vectors.

    Returns a float in [0, 1]: 1 means identical feature profile, 0 means
    no shared features. Returns 0.0 if either vector is all zeros.
    """
    p = np.array([pocket_features[k] for k in FEATURE_KEYS], dtype=float)
    l = np.array([ligand_features[k] for k in FEATURE_KEYS], dtype=float)
    norm_p = np.linalg.norm(p)
    norm_l = np.linalg.norm(l)
    if norm_p == 0 or norm_l == 0:
        return 0.0
    return float(np.dot(p, l) / (norm_p * norm_l))


def _enrichment_factor(scores_actives: list, scores_decoys: list, fraction: float) -> float:
    """
    Compute enrichment factor (EF) at a given fraction of the ranked list.

    EF = (actives found in top fraction) / (expected actives if random)
    """
    n_actives = len(scores_actives)
    n_total = n_actives + len(scores_decoys)
    n_top = max(1, int(fraction * n_total))

    all_scores = [(s, 1) for s in scores_actives] + [(s, 0) for s in scores_decoys]
    all_scores.sort(key=lambda x: -x[0])

    actives_in_top = sum(label for _, label in all_scores[:n_top])
    expected = fraction * n_actives
    if expected == 0:
        return 0.0
    return float(actives_in_top / expected)


def compute_discrimination_metrics(scores_actives: list, scores_decoys: list) -> dict:
    """
    Compute ROC-AUC, EF1%, and EF5% for a conformation given active and decoy scores.

    Args:
        scores_actives: Complementarity scores for active ligands.
        scores_decoys: Complementarity scores for decoy ligands.

    Returns:
        Dict with keys 'roc_auc', 'ef1', 'ef5'.
    """
    labels = [1] * len(scores_actives) + [0] * len(scores_decoys)
    scores = list(scores_actives) + list(scores_decoys)

    try:
        roc_auc = float(roc_auc_score(labels, scores))
    except ValueError:
        roc_auc = 0.5

    ef1 = _enrichment_factor(scores_actives, scores_decoys, 0.01)
    ef5 = _enrichment_factor(scores_actives, scores_decoys, 0.05)

    return {'roc_auc': roc_auc, 'ef1': ef1, 'ef5': ef5}


def _load_mols_from_sdf(sdf_path: str) -> list:
    """Load valid RDKit molecules from an SDF file, skipping invalid entries."""
    supplier = Chem.SDMolSupplier(sdf_path, removeHs=False)
    mols = []
    for mol in supplier:
        if mol is not None:
            mols.append(mol)
    return mols


def run_discrimination(cluster_dir: str, actives_sdf: str, decoys_sdf: str,
                       outfolder: str) -> pd.DataFrame:
    """
    Full discrimination pipeline.

    Reads cluster_representatives.csv, scores all actives and decoys against
    each representative's pocket pharmacophore, computes discrimination metrics,
    and saves results.

    Args:
        cluster_dir: Path to pocket_clusters output directory (contains
                     cluster_representatives.csv).
        actives_sdf: Path to actives SDF file (max 200 molecules).
        decoys_sdf: Path to decoys SDF file (max 2000 molecules).
        outfolder: Directory where output files are saved.

    Returns:
        DataFrame with columns: cluster_id, roc_auc, ef1, ef5, residues,
        sorted by roc_auc descending.
    """
    os.makedirs(outfolder, exist_ok=True)

    reps_csv = os.path.join(cluster_dir, 'cluster_representatives.csv')
    if not os.path.exists(reps_csv):
        raise FileNotFoundError(f"cluster_representatives.csv not found in {cluster_dir}")

    df_reps = pd.read_csv(reps_csv)
    if 'residues' not in df_reps.columns:
        raise ValueError("cluster_representatives.csv must contain a 'residues' column")

    # Load ligands
    actives = _load_mols_from_sdf(actives_sdf)
    decoys = _load_mols_from_sdf(decoys_sdf)

    if not actives:
        raise ValueError(f"No valid molecules loaded from actives SDF: {actives_sdf}")
    if not decoys:
        raise ValueError(f"No valid molecules loaded from decoys SDF: {decoys_sdf}")

    # Precompute ligand features
    active_features = [get_ligand_features(mol) for mol in actives]
    decoy_features = [get_ligand_features(mol) for mol in decoys]

    rows = []
    for _, rep in df_reps.iterrows():
        residue_ids = [r for r in str(rep['residues']).split() if r]
        pocket_feats = get_pocket_features(residue_ids)

        scores_actives = [score_complementarity(pocket_feats, lf) for lf in active_features]
        scores_decoys = [score_complementarity(pocket_feats, df) for df in decoy_features]

        metrics = compute_discrimination_metrics(scores_actives, scores_decoys)

        rows.append({
            'cluster_id': rep.get('cluster', ''),
            'frame': rep.get('Frame', ''),
            'roc_auc': round(metrics['roc_auc'], 4),
            'ef1': round(metrics['ef1'], 4),
            'ef5': round(metrics['ef5'], 4),
            'residues': rep['residues'],
            'probability': rep.get('probability', ''),
        })

    df_results = pd.DataFrame(rows).sort_values('roc_auc', ascending=False).reset_index(drop=True)
    df_results.to_csv(os.path.join(outfolder, 'discrimination_results.csv'), index=False)

    # Save top representatives
    top_dir = os.path.join(outfolder, 'top_representatives')
    os.makedirs(top_dir, exist_ok=True)
    # (PDB copying handled by Streamlit app using frame numbers; CLI outputs CSV only)

    return df_results
```

- [ ] **Step 2: Run the tests — they should now pass**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
python -m pytest tests/test_discriminate.py -v
```

Expected: All tests pass (`PASSED` for each). If any fail, fix the implementation before continuing.

- [ ] **Step 3: Commit**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
git add discriminate.py tests/test_discriminate.py tests/__init__.py
git commit -m "feat: add pharmacophore-based discrimination module with tests"
```

---

## Task 5: Add `discriminate` Subcommand to CLI

**Files:**
- Modify: `PocketHunter/pockethunter.py`

- [ ] **Step 1: Add import at top of `pockethunter.py`**

In `/home/bogrum/workspace/my_projects/PocketHunter/pockethunter.py`, add after line 9 (`import plot`):

```python
import discriminate
```

- [ ] **Step 2: Add `discriminate_conformations` function before `main()`**

Add this function before the `main()` function (before line 161):

```python
def discriminate_conformations(args, config):
    """Rank cluster representatives by active/decoy discrimination."""
    logger = config['logger']
    logger.info("Starting discrimination analysis.")
    df = discriminate.run_discrimination(
        cluster_dir=os.path.abspath(args.cluster_dir),
        actives_sdf=os.path.abspath(args.actives),
        decoys_sdf=os.path.abspath(args.decoys),
        outfolder=os.path.abspath(args.outfolder),
    )
    logger.info(f"Discrimination complete. Results saved to {args.outfolder}")
    logger.info(f"\n{df[['cluster_id', 'frame', 'roc_auc', 'ef1', 'ef5']].to_string(index=False)}")
```

- [ ] **Step 3: Register the subparser inside `main()`**

In `main()`, after the `parser_plot` block (after line 220, before `args = parser.parse_args()`), add:

```python
    # Discrimination analysis
    parser_disc = subparsers.add_parser(
        "discriminate",
        help="Rank cluster representatives by active/decoy pharmacophore discrimination."
    )
    parser_disc.add_argument(
        "--cluster_dir", required=True,
        help="Path to pocket_clusters output directory (contains cluster_representatives.csv)."
    )
    parser_disc.add_argument(
        "--actives", required=True,
        help="Path to actives SDF file (max 200 molecules)."
    )
    parser_disc.add_argument(
        "--decoys", required=True,
        help="Path to decoys SDF file (max 2000 molecules)."
    )
    parser_disc.add_argument(
        "--outfolder", required=True,
        help="Output folder for discrimination results."
    )
    parser_disc.add_argument(
        "--overwrite", action='store_true',
        help="Overwrite existing output directory."
    )
```

- [ ] **Step 4: Add routing in the command dispatch block**

After `elif args.command == "plot_clustermap":` (around line 243), add:

```python
    elif args.command == "discriminate":
        discriminate_conformations(args, config)
```

Also update the `if not args.command == "plot_clustermap":` guard to also skip config for discriminate if outfolder already exists — actually, the existing `get_config(args.outfolder, args.overwrite)` already handles this since `discriminate` does have `--outfolder`. So no change needed to the config guard; it already works.

- [ ] **Step 5: Smoke-test the subcommand help**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
python pockethunter.py discriminate --help
```

Expected output includes `--cluster_dir`, `--actives`, `--decoys`, `--outfolder`.

- [ ] **Step 6: Commit**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
git add pockethunter.py requirements.txt
git commit -m "feat: add discriminate subcommand to CLI"
```

---

## Task 6: Sync CLI Changes to Suite

**Files:**
- Create/overwrite: `PocketHunter-Suite/PocketHunter/discriminate.py`
- Modify: `PocketHunter-Suite/PocketHunter/pockethunter.py`
- Modify: `PocketHunter-Suite/PocketHunter/requirements.txt`

- [ ] **Step 1: Copy discriminate.py to Suite**

```bash
cp /home/bogrum/workspace/my_projects/PocketHunter/discriminate.py \
   /home/bogrum/workspace/my_projects/PocketHunter-Suite/PocketHunter/discriminate.py
```

- [ ] **Step 2: Copy updated pockethunter.py and requirements.txt to Suite**

```bash
cp /home/bogrum/workspace/my_projects/PocketHunter/pockethunter.py \
   /home/bogrum/workspace/my_projects/PocketHunter-Suite/PocketHunter/pockethunter.py

cp /home/bogrum/workspace/my_projects/PocketHunter/requirements.txt \
   /home/bogrum/workspace/my_projects/PocketHunter-Suite/PocketHunter/requirements.txt
```

- [ ] **Step 3: Commit sync to Suite repo**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
git add PocketHunter/discriminate.py PocketHunter/pockethunter.py PocketHunter/requirements.txt
git commit -m "chore: sync discriminate module from CLI repo"
```

---

## Task 7: Add `run_discrimination_task` Celery Task

**Files:**
- Modify: `PocketHunter-Suite/tasks.py`

- [ ] **Step 1: Add the task at the end of `tasks.py`**

Append to `/home/bogrum/workspace/my_projects/PocketHunter-Suite/tasks.py`:

```python

@celery_app.task(bind=True, time_limit=600)
def run_discrimination_task(self, cluster_job_id, actives_path, decoys_path, job_id):
    """
    Celery task: run pharmacophore-based active/decoy discrimination.

    Args:
        cluster_job_id: Job ID of the completed cluster step (used to locate
                        cluster_representatives.csv).
        actives_path: Absolute path to the uploaded actives SDF file.
        decoys_path: Absolute path to the uploaded decoys SDF file.
        job_id: Unique ID for this discrimination job (used for output paths).
    """
    import sys
    sys.path.insert(0, POCKETHUNTER_DIR)
    import discriminate as disc

    output_dir = os.path.join(RESULTS_DIR, job_id, 'discrimination')
    cluster_dir = os.path.join(RESULTS_DIR, cluster_job_id, 'pocket_clusters')

    _update_status_file(job_id, 'running', step='discrimination', task_id=self.request.id)

    self.update_state(state='PROGRESS', meta={
        'current_step': 'Loading ligands and computing pharmacophore features…',
        'progress': 10,
    })

    try:
        df_results = disc.run_discrimination(
            cluster_dir=cluster_dir,
            actives_sdf=actives_path,
            decoys_sdf=decoys_path,
            outfolder=output_dir,
        )

        self.update_state(state='PROGRESS', meta={
            'current_step': 'Discrimination complete.',
            'progress': 95,
        })

        result_info = {
            'discrimination_results_csv': os.path.join(output_dir, 'discrimination_results.csv'),
            'n_conformations': len(df_results),
            'best_cluster_id': str(df_results.iloc[0]['cluster_id']) if len(df_results) > 0 else None,
            'best_roc_auc': float(df_results.iloc[0]['roc_auc']) if len(df_results) > 0 else None,
        }

        _update_status_file(job_id, 'completed', step='discrimination',
                            task_id=self.request.id, result_info=result_info)
        return result_info

    except Exception as e:
        logger.error(f"Discrimination task failed for job {job_id}: {e}")
        _update_status_file(job_id, 'failed', step='discrimination', task_id=self.request.id)
        raise
```

- [ ] **Step 2: Verify the task is importable**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
python -c "from tasks import run_discrimination_task; print('task ok')"
```

Expected: `task ok`

- [ ] **Step 3: Commit**

```bash
git add tasks.py
git commit -m "feat: add run_discrimination_task Celery task"
```

---

## Task 8: Remove Docking from Streamlit

**Files:**
- Modify: `PocketHunter-Suite/session_state.py`
- Modify: `PocketHunter-Suite/main.py`
- Modify: `PocketHunter-Suite/pipeline_app.py`

### 8a — session_state.py

- [ ] **Step 1: Remove docking keys from `initialize_session_state()`**

In `session_state.py`, remove these blocks (lines 58–83):

```python
    # Docking state
    if 'docking_job_id' not in st.session_state:
        st.session_state.docking_job_id = None
    if 'docking_task_id' not in st.session_state:
        st.session_state.docking_task_id = None
    if 'docking_display_job_id' not in st.session_state:
        st.session_state.docking_display_job_id = None
    if 'view_mode' not in st.session_state:
        st.session_state.view_mode = 'setup'

    # Docking PDB selections - stores selected PDB files by filename
    if 'docking_selected_pdbs' not in st.session_state:
        st.session_state.docking_selected_pdbs = {}

    # Heatmap job tracking - used to detect job changes and clear stale state
    if 'heatmap_last_job_id' not in st.session_state:
        st.session_state.heatmap_last_job_id = None

    # Pipeline inline docking state
    if 'pipe_docking_task_id' not in st.session_state:
        st.session_state.pipe_docking_task_id = None
    if 'pipe_docking_job_id' not in st.session_state:
        st.session_state.pipe_docking_job_id = None
    if 'pipe_selected_pose' not in st.session_state:
        st.session_state.pipe_selected_pose = None
```

Also remove these docking-related heatmap keys (lines 86–92):

```python
    if 'heatmap_docking_clusters' not in st.session_state:
        st.session_state.heatmap_docking_clusters = []
```

Replace with discrimination state keys, adding after the `heatmap_selected_residues` block:

```python
    # Heatmap job tracking
    if 'heatmap_last_job_id' not in st.session_state:
        st.session_state.heatmap_last_job_id = None

    # Discrimination state
    if 'discrimination_job_id' not in st.session_state:
        st.session_state.discrimination_job_id = None
    if 'discrimination_task_id' not in st.session_state:
        st.session_state.discrimination_task_id = None
    if 'discrimination_status' not in st.session_state:
        st.session_state.discrimination_status = 'idle'
```

Also remove `clear_docking_selections()` function (lines 119–125) entirely.

Also update `cached_job_ids` initialization in `session_state.py` — change `'docking': None` to `'discrimination': None`:

```python
    if 'cached_job_ids' not in st.session_state:
        st.session_state.cached_job_ids = {
            'extract': None,
            'detect': None,
            'cluster': None,
            'discrimination': None,
            'pipeline': None,
        }
```

### 8b — main.py

- [ ] **Step 2: Update `main.py` to remove docking page and add discrimination page**

In `main.py` line 17, change the import:

```python
from tasks import run_pockethunter_pipeline, run_extract_to_pdb_task, run_detect_pockets_task, run_cluster_pockets_task, run_discrimination_task
```

At line 205–211, update `cached_job_ids`:

```python
if 'cached_job_ids' not in st.session_state:
    st.session_state.cached_job_ids = {
        'extract': None,
        'detect': None,
        'cluster': None,
        'discrimination': None,
        'pipeline': None,
    }
```

At lines 214–220, update the `pages` dict:

```python
pages = {
    "Full Pipeline": "pipeline_app.py",
    "Step 1: Extract Frames": "extract_frames_app.py",
    "Step 2: Detect Pockets": "detect_pockets_app.py",
    "Step 3: Cluster Pockets": "cluster_pockets_app.py",
    "Step 4: Discrimination Analysis": "discrimination_app.py",
    "Task Monitor": "task_monitor_app.py"
}
```

At line 235–236, update the icons list (replace `'flask'` with `'funnel'`):

```python
    icons=['lightning-charge', 'file-earmark-arrow-down', 'search', 'diagram-3', 'funnel', 'activity'],
```

### 8c — pipeline_app.py (docking removal)

- [ ] **Step 3: Remove docking import from pipeline_app.py line 17**

Change:
```python
from tasks import run_pockethunter_pipeline, run_docking_task
```
To:
```python
from tasks import run_pockethunter_pipeline
```

- [ ] **Step 4: Remove docking helper functions and inline docking section**

`pipeline_app.py` contains the following docking-related blocks that must be removed entirely:

1. Lines 143–320: `_show_docking_molecule_3d()` function and its CSS — **delete entirely**
2. Lines 322–786: The entire inline docking section inside `_show_heatmap_with_inline_results()` — specifically delete:
   - The `heatmap_docking_clusters` session state logic
   - The docking status polling block (`pipe_task_id = st.session_state.get('pipe_docking_task_id')` and everything that follows until the "← Select clusters" info line at ~line 786)
   - The docking launch form (`include_docking` checkbox and all related widgets in the main pipeline form)
   - The `run_docking=include_docking` argument in the `run_pockethunter_pipeline.delay()` call

Keep the heatmap visualization itself (`_show_heatmap_with_inline_results()` structure, 3D pocket viewer, cluster selection checkboxes) — only remove the docking-specific logic.

- [ ] **Step 5: Verify the app loads without errors**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
python -c "import ast, sys; ast.parse(open('pipeline_app.py').read()); print('syntax ok')"
python -c "import ast, sys; ast.parse(open('session_state.py').read()); print('syntax ok')"
python -c "import ast, sys; ast.parse(open('main.py').read()); print('syntax ok')"
```

Expected: `syntax ok` for each.

- [ ] **Step 6: Commit**

```bash
git add session_state.py main.py pipeline_app.py
git commit -m "refactor: remove docking from Streamlit app, wire discrimination page"
```

---

## Task 9: Create `discrimination_app.py`

**Files:**
- Create: `PocketHunter-Suite/discrimination_app.py`

- [ ] **Step 1: Create the page**

Create `/home/bogrum/workspace/my_projects/PocketHunter-Suite/discrimination_app.py`:

```python
"""
discrimination_app.py — Step 4: Discrimination Analysis

Users provide their cluster job ID, upload actives and decoys SDF files,
and the app ranks cluster representatives by pharmacophore-based active/decoy
discrimination (ROC-AUC, EF1%, EF5%).
"""

import streamlit as st
import os
import uuid
import json
import zipfile
import shutil
import time
import pandas as pd
import plotly.graph_objects as go
from pathlib import Path
import py3Dmol
import streamlit.components.v1 as components

from tasks import run_discrimination_task
from celery_app import celery_app
from config import Config
from session_state import initialize_session_state
from logging_config import setup_logging

RESULTS_DIR = str(Config.RESULTS_DIR)
UPLOAD_DIR = str(Config.UPLOAD_DIR)
logger = setup_logging(__name__)

initialize_session_state()

# ── Page header ──────────────────────────────────────────────────────────────

st.markdown("""
<div style="background: linear-gradient(135deg, #1565C0 0%, #2E7D32 100%);
            padding: 2rem; border-radius: 16px; margin-bottom: 2rem; color: white; text-align: center;">
    <h1 style="margin:0; font-size:2rem;">🔬 Discrimination Analysis</h1>
    <p style="margin:0.5rem 0 0 0; opacity:0.9;">
        Rank conformations by their ability to distinguish active from decoy ligands
    </p>
</div>
""", unsafe_allow_html=True)

# ── Input section ────────────────────────────────────────────────────────────

st.subheader("1. Provide Clustering Results")

cluster_job_id_input = st.text_input(
    "Cluster Job ID",
    value=st.session_state.cached_job_ids.get('cluster') or '',
    placeholder="e.g. 20260407_123456_abcd",
    help="The Job ID from the Cluster Pockets step. Auto-filled if you ran clustering in this session.",
)

cluster_valid = False
if cluster_job_id_input:
    reps_csv = os.path.join(RESULTS_DIR, cluster_job_id_input, 'pocket_clusters', 'cluster_representatives.csv')
    if os.path.exists(reps_csv):
        df_reps_preview = pd.read_csv(reps_csv)
        st.success(f"✅ Found {len(df_reps_preview)} cluster representatives.")
        cluster_valid = True
    else:
        st.error("❌ No cluster_representatives.csv found for this Job ID. Run Step 3 first.")

st.subheader("2. Upload Ligand Sets")

col1, col2 = st.columns(2)
with col1:
    actives_file = st.file_uploader(
        "Actives (SDF)", type=["sdf"],
        help="Known active ligands. Max 200 molecules.",
        key="disc_actives_upload"
    )
with col2:
    decoys_file = st.file_uploader(
        "Decoys (SDF)", type=["sdf"],
        help="Decoy (non-binding) ligands. Max 2000 molecules.",
        key="disc_decoys_upload"
    )

# ── Launch ───────────────────────────────────────────────────────────────────

st.subheader("3. Run Discrimination")

can_launch = cluster_valid and actives_file is not None and decoys_file is not None
disc_task_id = st.session_state.get('discrimination_task_id')
disc_job_id = st.session_state.get('discrimination_job_id')

if st.button("▶ Run Discrimination Analysis", disabled=not can_launch,
             type="primary", key="disc_launch"):
    # Save uploaded files
    job_id = f"disc_{uuid.uuid4().hex[:8]}"
    upload_job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(upload_job_dir, exist_ok=True)

    actives_path = os.path.join(upload_job_dir, 'actives.sdf')
    decoys_path = os.path.join(upload_job_dir, 'decoys.sdf')

    with open(actives_path, 'wb') as f:
        f.write(actives_file.getbuffer())
    with open(decoys_path, 'wb') as f:
        f.write(decoys_file.getbuffer())

    task = run_discrimination_task.delay(
        cluster_job_id=cluster_job_id_input,
        actives_path=actives_path,
        decoys_path=decoys_path,
        job_id=job_id,
    )

    st.session_state.discrimination_task_id = task.id
    st.session_state.discrimination_job_id = job_id
    st.session_state.discrimination_status = 'running'
    st.session_state.cached_job_ids['discrimination'] = job_id
    st.rerun()

# ── Status polling ───────────────────────────────────────────────────────────

if disc_task_id:
    task_result = celery_app.AsyncResult(disc_task_id)
    state = task_result.state
    meta = task_result.info or {}

    if state == 'PROGRESS':
        progress = meta.get('progress', 0)
        step = meta.get('current_step', 'Running…')
        st.progress(progress / 100, text=f"⏳ {step}")
        time.sleep(2)
        st.rerun()

    elif state == 'SUCCESS':
        st.session_state.discrimination_status = 'completed'
        result_info = task_result.result or {}
        _show_results(disc_job_id, result_info)

    elif state in ('FAILURE', 'REVOKED'):
        st.error(f"❌ Discrimination failed. Error: {meta.get('exc_message', 'Unknown error')}")
        if st.button("🔄 Reset", key="disc_reset"):
            st.session_state.discrimination_task_id = None
            st.session_state.discrimination_job_id = None
            st.session_state.discrimination_status = 'idle'
            st.rerun()

    elif state == 'PENDING':
        st.info("⏳ Job queued — waiting for a worker…")
        time.sleep(3)
        st.rerun()

    else:
        # Already completed in a previous run — reload from disk
        if st.session_state.discrimination_status == 'completed' and disc_job_id:
            status_file = os.path.join(RESULTS_DIR, disc_job_id + '_status.json')
            if os.path.exists(status_file):
                with open(status_file) as f:
                    status_data = json.load(f)
                _show_results(disc_job_id, status_data.get('result_info', {}))


# ── Results display ──────────────────────────────────────────────────────────

def _show_results(job_id, result_info):
    """Render the discrimination results table, ROC curves, 3D viewer, and download."""
    results_csv = result_info.get('discrimination_results_csv')
    if not results_csv or not os.path.exists(results_csv):
        st.warning("Results file not found. The job may still be running.")
        return

    df = pd.read_csv(results_csv)

    st.success(f"✅ Discrimination complete — {len(df)} conformations ranked.")
    st.markdown("### Ranked Conformations")

    # Colour-code by ROC-AUC
    def colour_auc(val):
        if val >= 0.7:
            return 'background-color: #c8e6c9'
        if val >= 0.6:
            return 'background-color: #fff9c4'
        return ''

    display_cols = ['cluster_id', 'frame', 'roc_auc', 'ef1', 'ef5']
    st.dataframe(
        df[display_cols].style.applymap(colour_auc, subset=['roc_auc']),
        use_container_width=True,
    )

    # ROC curve placeholder (per-conformation curves require per-ligand scores;
    # we show a bar chart of ROC-AUC per conformation instead)
    st.markdown("### ROC-AUC per Conformation")
    fig = go.Figure(go.Bar(
        x=[f"Cluster {row['cluster_id']} (Fr.{row['frame']})" for _, row in df.iterrows()],
        y=df['roc_auc'],
        marker_color=['#2E7D32' if v >= 0.7 else '#F57C00' if v >= 0.6 else '#C62828'
                      for v in df['roc_auc']],
    ))
    fig.update_layout(
        xaxis_title="Conformation",
        yaxis_title="ROC-AUC",
        yaxis=dict(range=[0, 1]),
        height=350,
    )
    fig.add_hline(y=0.5, line_dash='dash', line_color='grey', annotation_text='Random')
    st.plotly_chart(fig, use_container_width=True)

    # Download ZIP of top conformations
    st.markdown("### Download Top Conformations")
    n_top = st.slider("Number of top conformations to download", 1, min(5, len(df)), 3,
                      key="disc_n_top")

    top_job_dir = os.path.join(RESULTS_DIR, job_id, 'discrimination')

    if st.button("📦 Prepare ZIP", key="disc_prepare_zip"):
        top_df = df.head(n_top)
        zip_path = os.path.join(top_job_dir, 'top_conformations.zip')
        cluster_dir = os.path.join(RESULTS_DIR,
                                   st.session_state.cached_job_ids.get('cluster', ''),
                                   'pocket_clusters')
        reps_csv_path = os.path.join(cluster_dir, 'cluster_representatives.csv')

        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.write(results_csv, 'discrimination_results.csv')
            # Try to include PDB files if they can be located via the extract job
            extract_job_id = st.session_state.cached_job_ids.get('extract', '')
            pdb_dir = os.path.join(RESULTS_DIR, extract_job_id, 'pdbs') if extract_job_id else ''
            for _, row in top_df.iterrows():
                frame = str(row['frame'])
                if pdb_dir:
                    matches = [f for f in os.listdir(pdb_dir) if f'_{frame}.pdb' in f] if os.path.isdir(pdb_dir) else []
                    for pdb_file in matches:
                        zf.write(os.path.join(pdb_dir, pdb_file),
                                 f"top_conformations/cluster_{row['cluster_id']}_{pdb_file}")

        with open(zip_path, 'rb') as f:
            st.download_button(
                "⬇ Download ZIP",
                data=f.read(),
                file_name=f"top_conformations_{job_id}.zip",
                mime="application/zip",
                key="disc_download_zip"
            )
```

- [ ] **Step 2: Verify syntax**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
python -c "import ast; ast.parse(open('discrimination_app.py').read()); print('syntax ok')"
```

Expected: `syntax ok`

- [ ] **Step 3: Commit**

```bash
git add discrimination_app.py
git commit -m "feat: add discrimination_app.py Streamlit page"
```

---

## Task 10: Smoke Test & Final Integration Commit

- [ ] **Step 1: Verify all modified files parse cleanly**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
for f in main.py session_state.py pipeline_app.py tasks.py discrimination_app.py; do
  python -c "import ast; ast.parse(open('$f').read())" && echo "$f: ok" || echo "$f: SYNTAX ERROR"
done
```

Expected: `ok` for all five files.

- [ ] **Step 2: Verify Celery task registry includes new task**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
python -c "
from tasks import run_discrimination_task, run_pockethunter_pipeline
print('discrimination task:', run_discrimination_task.name)
print('pipeline task:', run_pockethunter_pipeline.name)
"
```

Expected: both task names print without error.

- [ ] **Step 3: Run CLI discrimination unit tests**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter
python -m pytest tests/test_discriminate.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Final commit on Suite repo with requirements**

```bash
cd /home/bogrum/workspace/my_projects/PocketHunter-Suite
git add requirements.txt PocketHunter/requirements.txt
git commit -m "chore: add rdkit to Suite requirements"
```

- [ ] **Step 5: Verify branch state on both repos**

```bash
git -C /home/bogrum/workspace/my_projects/PocketHunter branch
git -C /home/bogrum/workspace/my_projects/PocketHunter-Suite branch
```

Expected: `* feature/discrimination` for both.

---

## Self-Review Against Spec

| Spec requirement | Task |
|---|---|
| Remove docking from Streamlit nav | Task 8b |
| Remove docking from session state | Task 8a |
| Remove docking from pipeline page | Task 8c |
| Add `discriminate.py` CLI module | Task 4 |
| Add `discriminate` CLI subcommand | Task 5 |
| Pharmacophore from residue types | Task 4 (get_pocket_features) |
| Ligand features via RDKit | Task 4 (get_ligand_features) |
| Cosine similarity scoring | Task 4 (score_complementarity) |
| ROC-AUC, EF1%, EF5% metrics | Task 4 (compute_discrimination_metrics) |
| Celery task | Task 7 |
| Streamlit discrimination page | Task 9 |
| ROC-AUC bar chart | Task 9 (_show_results) |
| Download ZIP of top PDBs | Task 9 (_show_results) |
| Max 200 actives / 2000 decoys | Documented; enforcement can be added in Task 9 file upload step |
| Feature branches on both repos | Task 1 |
| rdkit dependency | Task 2 |
| Sync CLI copy to Suite | Task 6 |

**One gap noted:** The spec mentions molecule count limits (200 actives / 2000 decoys) but the page doesn't enforce them server-side in `run_discrimination_task`. Add a guard at the top of `run_discrimination_task` after loading mols:

```python
if len(actives) > 200:
    raise ValueError(f"Too many actives: {len(actives)} (max 200)")
if len(decoys) > 2000:
    raise ValueError(f"Too many decoys: {len(decoys)} (max 2000)")
```

Add this to Task 7 Step 1, inside `run_discrimination_task` after `df_results = disc.run_discrimination(...)` is replaced with the call — specifically, insert the guard inside the task before calling `disc.run_discrimination()`, since `_load_mols_from_sdf` is in `discriminate.py`. The guard should be added to `run_discrimination()` in `discriminate.py` itself (before the `active_features` computation line).
