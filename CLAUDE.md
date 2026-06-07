# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This repo has **two layers** that are easy to confuse:

- **Top level** (`/`): the Streamlit + Celery web suite that orchestrates the pipeline. This is what you usually edit.
- **`PocketHunter/`**: a vendored CLI tool (`pockethunter.py`) that the suite *shells out to* for the extract/detect/cluster steps. It has its own `CLAUDE.md` and `requirements.txt`. It is listed in `.gitignore` and is treated as a black-box subprocess by the suite — do not couple the suite to its internals beyond CLI args.

Docking does **not** go through the PocketHunter CLI — it lives in `step4_docking.py` (the task-side helper) and is invoked from `panels/docking.py`; it calls `smina` directly. The score grid the panel renders aggregates per-receptor best-pose affinities via `docking_aggregation.py` (`best_per_pair`, `aggregate_ensemble`, `ecr_scores`) — the four ranking metrics surfaced in the UI are Mean (arithmetic), Median (robust to one mis-scoring receptor), Best (RCS-style ensemble minimum), and ECR (Exponential Consensus Ranking, rank-based per Palacio-Rodríguez et al., Sci Rep 2019).

## Commands

### Docker (primary deploy path)

```bash
docker compose up --build           # Build + start all services
docker compose ps                   # Service status
docker compose logs -f streamlit    # Tail Streamlit logs
docker compose restart celery-worker
```

Compose brings up six static services: `redis`, `postgres`, `docker-socket-proxy`, `orchestrator` (port 9001 → 9000), `celery-beat`, `streamlit` (port 8511 → 8501). The **orchestrator** (Phase C) then dynamically spawns hardened worker containers named `ph-worker-<pool>-<id>` — `FAST_POOL_SIZE` of them on the `default,celery` queue and `DOCKING_POOL_SIZE` on the `docking` queue. The repo is bind-mounted into `streamlit` so its code edits hot-reload — workers do **not** hot-reload; after editing `tasks.py`, `step4_docking.py`, or `celery_app.py`, kill the workers (`docker rm -f $(docker ps -aq -f label=pockethunter.role=worker)`) and the orchestrator's reconcile loop will respawn them within 30 s on the new code.

### Local development (no Docker)

The orchestrator-managed pool design is Docker-native — on a bare host the simplest approximation is to run regular Celery workers (no container hardening, no recycle policy, no `/pool/status` API):

```bash
pip install -r requirements.txt
redis-server --daemonize yes

# In separate terminals — queue routing matches celery_app.task_routes
celery -A celery_app worker -Q default,celery --concurrency=8 --loglevel=info
celery -A celery_app worker -Q docking --concurrency=4 --loglevel=info
celery -A celery_app beat --loglevel=info
streamlit run main.py --server.port=8501
```

Skipping the orchestrator here is fine for unit-test / panel-development work; the hardening only matters when running untrusted inputs (production). `legacy/start_app.sh` and `legacy/setup.sh` reference a conda env named `dockspot` — these are pre-v2 bootstrappers preserved only for historical reference (see `legacy/README.md`). Prefer Docker or a plain venv.

### Health / config / cleanup

```bash
# Orchestrator deep healthz (Phase C5): postgres + redis + docker + per-pool counts.
curl -s http://localhost:9001/healthz | python3 -m json.tool

# Worker pool snapshot — one entry per fast/docking pool with each worker's state.
curl -s http://localhost:9001/pool/status | python3 -m json.tool

# Legacy host-side health probe (still works; pre-dates the orchestrator).
python health.py
python -c "from config import Config; Config.print_config()"
python -c "from resource_manager import ResourceManager; print(ResourceManager.get_usage_report())"
python -c "from cleanup_job import cleanup_old_jobs_task; cleanup_old_jobs_task()"
```

### Database migrations (v2 Phase A)

```bash
# Apply pending migrations against $DATABASE_URL
alembic upgrade head

# Create a new migration (commit A2+):
alembic revision --autogenerate -m "<message>"

# Inside docker compose:
docker compose run --rm streamlit alembic upgrade head
```

The Postgres service uses a named volume `pgdata`; `docker compose down` keeps data, `docker compose down -v` wipes it. `DATABASE_URL` and `BASE_URL` are required env vars — `.env.example` has the defaults.

The test suite under `tests/` runs with `pytest tests/` (280 tests as of the v2 Phase C C5 batch). DB tests live in `tests/test_db_*.py`; viewer/annotation tests live in `tests/test_viewer_*.py` and `tests/test_molstar_annotations.py`; panel-helper tests in `tests/test_panels_shared.py` and `tests/test_derive_session_annotations.py`; orchestrator + abuse-limits + observability tests in `tests/test_orchestrator_*.py`, `tests/test_c4_abuse_limits.py`, `tests/test_c5_observability.py`.

The streamlit container doesn't ship pytest by default — `docker compose exec streamlit pip install pytest pytest-mock` (one-shot) and then `docker compose exec streamlit python -m pytest tests/`.

## Architecture

### Request flow

```
Browser (Mol* viewer + panels) ──► Streamlit (main.py → analysis_app.py)
                                            │
                                            ▼
                                  Postgres (sessions, jobs) + Redis
                                            │
                                            ▼               ┌─ Orchestrator
                                    Celery task (tasks.py)  │  /pool/status
                                            │               │  /healthz
                            spawned + recycled by ──────────┤  reconcile loop
                            the Phase C orchestrator        │  spawns workers
                                            │               │  via the socket
                                            ▼               │  proxy (not host
                            HARDENED WORKER CONTAINER       │  /var/run/...)
                            (--read-only, cap-drop=ALL,     │
                             UID 1000, tmpfs /tmp:exec,     │
                             no public network egress)      │
                                            │               │
                            ├─► subprocess: python PocketHunter/pockethunter.py <step>
                            │   (extract / detect / cluster)
                            └─► subprocess: smina  (docking)
                                            │
                                            ▼
                       uploads/<job_id>/ ─► results/<job_id>/
                       (per-session viewer.cif symlinked into static/<short>/)
```

Redis is both the Celery broker and result backend. **Two pools** (Phase C cut-over from the B-era two-worker setup): `fast_pool` consumes `default,celery` (find_pockets / cluster / pipeline tasks); `docking_pool` consumes `docking` (smina runs). A flood of slow docking jobs pins at most `DOCKING_POOL_SIZE` workers — the fast pool keeps draining. `worker_prefetch_multiplier = 1` is set globally — keep it that way; raising it makes one slow task block its sibling slots.

### Phase C orchestrator + worker pools

`orchestrator/` is its own slim Python service (Flask + Docker SDK, ~150 MB image). It runs in a container alongside Streamlit, talks to the Docker daemon **only** through `docker-socket-proxy` (tecnativa, allow-listed endpoints), and exposes:

- `GET /healthz` — deep liveness (postgres + redis + docker + per-pool occupancy). Returns 200 or 503 + JSON naming the failing component.
- `GET /pool/status` — per-pool snapshot: `{"fast": {desired, actual, workers:[…]}, "docking": {…}}`.
- `POST /pool/recycle/<worker_id>` — manual tear-down + respawn.

Worker hardening applied at spawn time (see `orchestrator/docker_sdk.py:_spawn_worker`): `--read-only --cap-drop=ALL --security-opt=no-new-privileges:true --user 1000:1000 --tmpfs /tmp:exec,size=1g --tmpfs /scratch:size=4g --memory=$WORKER_MEMORY_LIMIT --cpus=$WORKER_CPU_LIMIT --network=pockethunter_internal --init`. The internal network has `internal: true` — workers have **no public internet egress**, but DNS to `redis` and `postgres` still resolves within the bridge.

Worker recycle policy: after `WORKER_JOBS_BEFORE_RECYCLE` completed tasks (default 10), the orchestrator tears the container down and spawns a fresh replacement. `/app` is bind-mounted read-only into every worker so suite source changes hot-reload across recycles without rebuilding the image.

`USE_ORCHESTRATOR=true` is the C3 default — the orchestrator owns the pools. Setting it to `false` runs the orchestrator process with zero workers (HTTP API still serves), useful for development scenarios where you want to run a celery worker outside the hardening (see "Local development" above).

### Single-page Streamlit shell (`main.py` + `analysis_app.py` + `panels/`)

`main.py` is a thin dispatcher: it resolves the session from `st.query_params` via `session_routes.resolve_session_from_query`, renders the landing / not-found / expired pages when applicable, then calls `analysis_app.render_analysis_app(_resolved)`. The legacy `option_menu` nav and `runpy.run_path` multi-page machinery are gone (B7).

**Editor vs Viewer.** A session URL is `/?s=<short_code>&edit=<edit_secret>`. Visiting with a valid `edit=` token makes `resolve_session` return `is_editor=True` (can run jobs, mutate the session); visiting with just `?s=` is a read-only **viewer** (`is_editor=False`). The check is `db.sessions.is_editor` (constant-time compare on the unhashed `edit_secret`); panels gate writes with `disabled=not is_editor`. The header chip (`landing.render_session_chip`) shows ✏️ Editor / 👁 Viewer and, for editors, the full copyable share URL. `recent_sessions.py` keeps a per-browser history of visited sessions in a cookie (client-side only — never persisted server-side).

`analysis_app.py` is the only page. It renders:

- A dynamic brutalist header (`● POCKETS ► ● CLUSTER ► ○ DOCK`, dots reflect completed jobs in the session).
- A `Run all stages` button + `st.segmented_control(["Pockets", "Cluster", "Dock"])` stage selector.
- A two-column body via `st.columns([3, 2])`:
  - **Left**: the persistent Mol* viewer, wrapped in `@st.fragment(run_every="3s")`. The fragment re-derives pocket + cluster annotations from the latest completed Job rows via `components.molstar_annotations.derive_session_annotations` and overlays the panel-set UI state (`ligand_pose`, `focus`) on top.
  - **Right**: dispatch to the active stage panel — `panels/find_pockets.py`, `panels/cluster.py`, or `panels/docking.py`. Each panel is a `render(session, is_editor) -> None` function with three states (settings → running → results), polling its own task with `celery_app.AsyncResult` + `time.sleep(3) + st.rerun()`.
- A full-width `Jobs` expander below the columns, also wrapped in `@st.fragment(run_every="3s")`. Renders the session's jobs newest-first with kind / status / duration / inline failure details.

Use `Skill` / Read / Edit on the panel modules — they're regular Python imports, not `__main__`-style scripts. Don't add new top-level Streamlit pages; everything goes through analysis_app or a new panel module.

### Job IDs and on-disk state

All persistent state for a run lives under a single `job_id` (a timestamp + uuid fragment generated when the job starts):

- `uploads/<job_id>/...` — user-uploaded inputs (sanitized via `security.handle_file_upload_secure`).
- `results/<job_id>/...` — pipeline outputs (subfolders `pdbs/`, `pockets/`, `pocket_clusters/`, `docking/`, plus a top-level `viewer.cif` produced by `viewer_pipeline.convert_pdb_dir_to_viewer`).
- `static/<session_short>/viewer.cif` — runtime symlink (or copy fallback) created by `viewer_pipeline.link_viewer_for_session` so Streamlit's static-file route can serve the Mol* trajectory to the browser. Listed in `.gitignore`.
- `results/<job_id>_status.json` — disk mirror of the Job row. Written by `tasks._update_status_file(...)` on every state change for backwards compatibility (read by older diagnostic tooling). The Postgres `Job` table is the authoritative source.

Per-session state lives in the Postgres `Session` and `Job` tables (see `db/models.py`). Each `Job` carries a `legacy_id` column matching the disk `job_id` so the task layer's disk writes + the panel-side DB reads stay consistent. `session_routes.register_session_job(job_id, kind)` tags a new disk job with the current session at submission time; `db.jobs.update_by_legacy_id(...)` updates a row from the task layer.

`failure_view.load_status_for_session(session_id)` returns the session's Job rows as dicts in the same shape the legacy on-disk status JSON used. Panels + the viewer fragment use it as their primary state source.

**Worker write contract (Phase C C3).** Inside hardened workers (every container the orchestrator spawns) the bind mounts are `/app:ro`, `/app/uploads:ro`, `/app/results:rw`, `/app/logs:rw`. **Workers must never write to `/app/uploads`** — that mount holds raw user inputs and is read-only by design so a compromised worker can't tamper with another session's uploads. Any worker-generated artifact (obabel-converted PDBQTs, `pdb_list.ds`, anything else) belongs under `/app/results/<job_id>/`. The two places that broke this rule pre-fix (`tasks._prepare_ligands_with_progress` writing alongside SDF inputs in `ligands_<job>/`, and `find_pockets_helpers.write_pdb_list_for_detect` called against an uploads dir) now stage their inputs into `results/<job>/{ligands,pdbs}/` before any writes. Streamlit-side code paths (`panels/docking._prepare_ligand_dir`, `panels/find_pockets._submit`) still write uploads RW because the `streamlit` service has `/app/uploads` RW — only workers are gated.

### `tasks.py` conventions

- `_run_stage(celery_task, command, cwd, timeout, prog_start, prog_end, stage_name)` is the only blessed way to run a subprocess inside a task. It redirects stdout/stderr to temp files — **don't switch to `subprocess.PIPE`**, p2rank and smina can emit >64 KB and deadlock the worker on the pipe buffer. It also emits `PROGRESS` state updates on a logarithmic ramp so the UI progress bar never lies by hitting 100% prematurely.
- The pipeline task `run_pockethunter_pipeline` orchestrates extract (0–25%) → detect (25–60%) → cluster (60–80%) → optional dock (80–97%). Per-step tasks (`run_extract_to_pdb_task`, `run_detect_pockets_task`, `run_cluster_pockets_task`, `run_docking_task`) exist for the individual Streamlit pages.
- All tasks call `_update_status_file` on failure to keep the status JSON in sync with Celery's own result backend.

### Configuration (`config.py`)

`Config` is a class with classmethods, not an instance. **It validates on import** — if `PocketHunter/pockethunter.py` is missing, the upload/results dirs can't be created, or the Redis URL is malformed, `from config import Config` will raise `ConfigurationError` and crash the worker/UI at startup. Treat this as the contract: never bypass `Config.validate()`, and always use `Config.get_upload_path(job_id, filename)` / `Config.get_results_path(job_id)` / `Config.get_status_file(job_id)` for path construction (they sanitize filenames and prevent path traversal).

Tunables come from `.env` (see `.env.example`). Notable: `MAX_UPLOAD_SIZE`, `MAX_ZIP_SIZE`, `RATE_LIMIT_*`, `CLEANUP_AFTER_DAYS`, `MAX_DOCKING_PDBS`, `DOCKING_TIMEOUT`, `P2RANK_PATH`, `SMINA_PATH`. Phase C added: `USE_ORCHESTRATOR`, `FAST_POOL_SIZE`, `DOCKING_POOL_SIZE`, `WORKER_CPU_LIMIT`, `WORKER_MEMORY_LIMIT`, `WORKER_JOBS_BEFORE_RECYCLE`, `WORKER_IMAGE_TAG`, `PER_SESSION_DISK_QUOTA_MB`, `MAX_CONCURRENT_FAST_JOBS`, `MAX_CONCURRENT_DOCKING_JOBS`, `MAX_CONCURRENT_FAST_PER_SESSION`, `MAX_CONCURRENT_DOCKING_PER_SESSION`, `MAX_SESSIONS_PER_IP_PER_DAY`.

### Security boundary

`security.py` (`FileValidator`, `handle_file_upload_secure`) is the choke point for user input — extension allowlist, size limits, ZIP-bomb checks, path-traversal prevention. Every upload should go through it; do not write raw `st.file_uploader` bytes to disk in new code. Disk-style job IDs round-trip through `FileValidator.validate_job_id` before any `os.path.join(RESULTS_DIR, job_id, ...)` (see the validation calls in `panels/cluster.py` and `panels/docking.py`).

`rate_limiter.py` provides `check_upload_rate_limit` / `check_task_rate_limit` (per-browser sliding windows) and `check_session_create_rate_limit(ip)` (Phase C C4 — Redis-backed per-IP daily cap, fail-open on Redis hiccups). Panels call these before kicking off Celery tasks. `client_ip.py` resolves the client IP from `st.context.headers` (`X-Forwarded-For` first hop, fallback `X-Real-IP`, then `"unknown"`). `RATE_LIMIT_ENABLED=false` in `.env` is the local-dev escape hatch.

**Phase C C4 abuse limits** (gated by `RATE_LIMIT_ENABLED`):

- Per-session disk quota: `security.handle_file_upload_secure(..., session_id=)` raises `SessionQuotaExceeded` when accepting the upload would push `db.sessions.disk_usage_mb(session_id)` over `PER_SESSION_DISK_QUOTA_MB`. Enforced periodically by `cleanup_job.enforce_session_disk_quotas_task` (hourly beat schedule), which prunes oldest job dirs until the session is back under quota.
- Per-pool concurrency: `panels/_shared.assert_submit_allowed(pool, session_id)` refuses a submission with `PoolCapHit` when `db.jobs.in_flight_count` hits `MAX_CONCURRENT_*_JOBS` (global) or `MAX_CONCURRENT_*_PER_SESSION` (per session). All three panels call it before `.delay()`.
- Per-IP session creation: `landing.py` routes all three "Start new analysis" buttons through `_create_session_with_rate_limit()`.

### Mol* viewer + annotations

The persistent Mol* viewer is a Streamlit Components v2 component (`components/molstar_viewer.py`). It loads our **own bundle** at `/app/static/js/molstar-bridge.js` — built from `frontend/src/index.ts` via Vite. Mol*'s default jsDelivr UMD only exposes the `Viewer` class; everything we need for programmatic residue selection (`MolScriptBuilder`, `StructureSelection`, `Color`, `StateTransforms`) is bundled-but-private. The bridge re-exposes a focused API on `window.molstarBridge.MolViewer` covering: `loadStructure`, `setCurrentModel`, `showPocketSurface`, `clearPocketSurfaces`, `showClusterOverpaint`, `clearClusterOverpaints`, `loadLigandPose`, `clearLigandPose`, `focusOnResidues`, `resetCamera`, `onResidueClick`.

The Python signature accepts `structure_url`, `structure_format`, an `annotations` dict, `current_model`, and an `on_residue_clicked_change` callback. The component JS owns idempotent annotation application (fingerprint check on `lastAnnotations`) and frame switching via `setCurrentModel`.

`components/molstar_annotations.py` is the Python side. `derive_session_annotations(session_id, results_dir)` is what the viewer fragment calls every 3 s; the panels' own annotation writes are for UI state (`ligand_pose`, `focus`, table-driven pocket surfaces).

### Frontend build (Mol* bridge)

The bridge bundle ships in git at `static/js/molstar-bridge.js` + `static/js/molstar-bridge.css`. Contributors who don't edit it don't need Node. To rebuild after editing `frontend/src/`:

```bash
cd frontend
npm install      # one-time
npm run build    # writes the .js + .css into ../static/js/
```

Commit the rebuilt artefacts alongside your source change. Pinned to `molstar@4.7.0` + `vite@5.2.11` in `frontend/package.json`. Dockerfile does NOT install Node — `COPY . .` brings the pre-built bundle in.

## External binaries

The pipeline depends on two binaries that are **not** Python packages:

- **p2rank** (`prank`): used inside `PocketHunter/` for pocket prediction. The Dockerfile installs Java; `PocketHunter/first_setup.sh` downloads p2rank into `PocketHunter/tools/p2rank/`.
- **smina**: used by `step4_docking.py` for docking. Now installed *inside* the worker image via the micromamba `docking` conda env (`worker.Dockerfile`); previous host-bind-mount approach is gone. Set `SMINA_PATH` only if you're running locally (no Docker) and your smina binary isn't on `$PATH`.

`install_docking_deps.sh` is a conda-specific helper (ProDy + OpenBabel) and is orthogonal to the Docker path.

## Phase C deployment

Public deployment guidance lives in `docs/deployment.md` (single-box / 64-core target sizing, reverse-proxy / TLS, production env-overrides, smoke checklist, rollback). The hardening checklist for every flag the orchestrator applies to spawned workers is in `docs/security.md` — refer to those before flipping the stack live for untrusted internet users.
