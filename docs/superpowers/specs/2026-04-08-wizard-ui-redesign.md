# PocketHunter Suite — Wizard UI Redesign
**Date:** 2026-04-08  
**Branch:** feature/discrimination  
**Refs:** `2026-04-07-pockethunter-repurposing-design.md`

---

## 1. Problem

The current UI uses a horizontal `option_menu` with 6 separate pages (`Full Pipeline`, `Step 1`, `Step 2`, `Step 3`, `Step 4`, `Task Monitor`). This creates friction:

- Users must manually copy Job IDs between tabs to chain steps
- Session state is lost if the browser is closed mid-run
- No clear sense of progress or where in the workflow the user is
- Multiple redundant pages for what is conceptually one workflow

The supervisor identified separate tabs as poor UX for a web server targeting computational biologists who run a single end-to-end analysis.

---

## 2. Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Navigation model | Linear wizard | Guides user through a single flow; no tab-switching |
| Individual step pages | Removed | Replaced entirely by the wizard |
| Full Pipeline page | Replaced by wizard Step 2 | Wizard wraps the same `run_pockethunter_pipeline` Celery task |
| Background job resume | Job ID (user-saved) | No auth in scope; disk auto-detect leaks other users' jobs on public server |
| Task Monitor | Kept as separate nav page | Useful for admins and debugging; accessible via header link |
| Color palette | `#6B7FA8` slate blue primary, `#B0BDD0` border, `#F5F6F8` background | Distinct from supervisor's grinn-web (`#7C9885` sage green) while sharing the same muted academic aesthetic |
| Font | Roboto (Google Fonts) | Matches grinn-web, professional |
| Emoji policy | None in UI | Avoid "AI-generated" appearance |

---

## 3. Wizard Structure

The wizard is a single Streamlit page (`wizard_app.py`) with four sequential steps. Each step unlocks only after the previous one completes.

```
Step 1 (Setup)  →  Step 2 (Pipeline)  →  Step 3 (Discrimination)  →  Step 4 (Results)
     ↑
  Always editable before launch; locked after
```

### Step 1 — Setup
- Upload: trajectory (`.xtc`), topology (`.pdb` / `.gro`)
- Parameters: stride, threads, min probability, clustering method
- Action: **Run Pipeline** button
- State: active on page load; collapses to "Done" after launch

### Step 2 — Extract · Detect · Cluster
- Runs `run_pockethunter_pipeline` Celery task (unchanged)
- Shows: animated progress bar, stage chips (Frame extraction / Pocket detection / Clustering), log tail
- Job ID banner appears immediately after launch with copy button and warning to save it
- User can close browser; job continues in background
- State: polling every 3s via `st.rerun()`; on success shows metrics (frames, pockets, representatives)

### Step 3 — Discrimination Analysis
- Unlocks after Step 2 completes
- Upload: actives `.sdf` (max 200), decoys `.sdf` (max 2000)
- Action: **Run Discrimination Analysis** button
- Runs `run_discrimination_task` Celery task (unchanged)
- Shows: progress bar, same polling loop

### Step 4 — Results
- Unlocks after Step 3 completes
- Shows: ranked conformations table (Rank, Cluster, ROC-AUC, EF1%, EF5%, Representative PDB)
- Inline: ROC curve chart (Plotly, ROC-AUC per cluster), 3D structure viewer (py3Dmol) showing the top-ranked cluster representative with pocket residues highlighted in orange — reuses `show_molecule_3d_with_pocket()` from `pipeline_app.py`
- Action: **Download Top-3 PDB (ZIP)** button
- Secondary action: **Start New Analysis** resets wizard state

---

## 4. Job Resume Flow

When the user returns after closing the browser:

1. Page loads with wizard in "new analysis" state (Streamlit session is gone)
2. A **Resume** bar at the top shows a text input for Job ID
3. User pastes their saved Job ID and clicks **Resume**
4. App reads disk state: checks status JSON and result files for that Job ID
5. Wizard jumps to the correct step and state (running / awaiting discrimination / complete)

Resume logic reads:
- `{RESULTS_DIR}/{job_id}_status.json` → overall status
- `{RESULTS_DIR}/{job_id}/pocket_clusters/cluster_representatives.csv` → Step 2 done
- `{RESULTS_DIR}/{job_id}/discrimination_results.csv` → Step 3 done (path from status JSON)

---

## 5. Navigation

```
Header: [PocketHunter Suite logo]  [Analysis ●]  [Task Monitor]
```

- **Analysis** — wizard page (default, `main.py` renders this directly)
- **Task Monitor** — existing `task_monitor_app.py`, routed via header link

`main.py` drops the `option_menu` and the individual step pages. It renders the wizard by default and adds a header with the two nav links.

---

## 6. File Changes

| File | Change |
|---|---|
| `main.py` | Remove `option_menu`; add simple header with two nav links; route to `wizard_app.py` or `task_monitor_app.py` |
| `wizard_app.py` | New file — full wizard implementation |
| `extract_frames_app.py` | Removed from navigation (file kept but not routed) |
| `detect_pockets_app.py` | Removed from navigation |
| `cluster_pockets_app.py` | Removed from navigation |
| `pipeline_app.py` | Removed from navigation (logic absorbed into wizard) |
| `discrimination_app.py` | Removed from navigation (logic absorbed into wizard) |
| `task_monitor_app.py` | Kept; routed via header |
| `session_state.py` | Add wizard-specific keys: `wizard_step`, `wizard_job_id`, `wizard_task_id`, `wizard_disc_task_id` |

---

## 7. Visual Design

### CSS Tokens

```css
--primary:     #6B7FA8;   /* slate blue */
--primary-dk:  #5a6e97;   /* hover/active */
--border:      #B0BDD0;   /* panel borders */
--bg:          #F5F6F8;   /* page background */
--bg-panel:    rgba(255,255,255,0.92);
--text:        #2a3a4a;
--text-muted:  #9aa0b8;
--success:     #00a085;
--warning:     #c0863a;
--danger:      #d63031;
--running:     #2d74da;
```

### Component Patterns

**Panel (section card)**
```css
background: rgba(255,255,255,0.92);
border: 2px solid #B0BDD0;
border-radius: 12px;
box-shadow: 0 4px 16px rgba(0,0,0,0.06);
```
Active panel: `border-color: #6B7FA8`, stronger shadow.

**Status badges** — text only, `border: 1px solid`, subdued background — no emoji, no icons.

**Progress bar** — animated gradient shimmer while running; solid on complete.

**Upload zones** — dashed border, softens to primary on hover.

**Launch button** — full-width, `background: #6B7FA8`, `border-radius: 8px`.

---

## 8. Out of Scope

- User accounts / authentication
- Result persistence beyond session (Job ID approach covers the resume case)
- Cluster heatmap page (stays in `cluster_pockets_app.py`, not in wizard MVP)
- Dark mode toggle
- Mobile layout

---

## 9. Open Questions

None — all design decisions resolved during brainstorming session.
