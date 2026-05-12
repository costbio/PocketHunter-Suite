# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This repo has **two layers** that are easy to confuse:

- **Top level** (`/`): the Streamlit + Celery web suite that orchestrates the pipeline. This is what you usually edit.
- **`PocketHunter/`**: a vendored CLI tool (`pockethunter.py`) that the suite *shells out to* for the extract/detect/cluster steps. It has its own `CLAUDE.md` and `requirements.txt`. It is listed in `.gitignore` and is treated as a black-box subprocess by the suite — do not couple the suite to its internals beyond CLI args.

Docking (Step 4) does **not** go through the PocketHunter CLI — it lives in `step4_docking.py` and `docking_app.py` and calls `smina` directly.

## Commands

### Docker (primary deploy path)

```bash
docker compose up --build           # Build + start all services
docker compose ps                   # Service status
docker compose logs -f streamlit    # Tail Streamlit logs
docker compose restart celery-worker
```

Compose brings up five containers: `redis`, `celery-worker` (queue `default`, concurrency 8), `celery-docking-worker` (queue `docking`, concurrency 4), `celery-beat` (scheduled cleanup), `streamlit` (port 8501). The repo is bind-mounted into the Streamlit container, so code edits hot-reload — but Celery workers do **not** hot-reload; restart them after editing `tasks.py`, `step4_docking.py`, or `celery_app.py`.

### Local development (no Docker)

```bash
pip install -r requirements.txt
redis-server --daemonize yes

# In separate terminals — note the queue routing must match docker-compose
celery -A celery_app worker -Q default,celery --concurrency=8 --loglevel=info
celery -A celery_app worker -Q docking --concurrency=4 --loglevel=info
celery -A celery_app beat --loglevel=info
streamlit run main.py --server.port=8501
```

`start_app.sh` and `setup.sh` reference a conda env named `dockspot` — these are legacy bootstrappers, not the documented path. Prefer Docker or a plain venv.

### Health / config / cleanup

```bash
python health.py                                                # Redis + Celery + disk health
python -c "from config import Config; Config.print_config()"    # Effective config
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

The test suite under `tests/` runs with `pytest tests/` (109+ tests as of the latest batch). New Phase A tests live in `tests/test_db_*.py`.

## Architecture

### Request flow

```
Streamlit (main.py → page module) ──► Celery task (tasks.py)
                                            │
                                            ├─► subprocess: python PocketHunter/pockethunter.py <step>
                                            │   (extract / detect / cluster)
                                            │
                                            └─► subprocess: smina  (docking only)
                                            │
                                            ▼
                              uploads/<job_id>/ ─► results/<job_id>/ + results/<job_id>_status.json
```

Redis is both the Celery broker and result backend. Two queues isolate workloads: the long, CPU-heavy `docking` queue has its own worker so docking jobs never starve pipeline steps. `worker_prefetch_multiplier = 1` is set globally — keep it that way; raising it makes one slow task block its sibling slots.

### Multi-page Streamlit (important quirk)

`main.py` is the only real Streamlit entrypoint. Page navigation is done with `option_menu`, and selected pages are executed via `runpy.run_path(..., init_globals={'st': st, ...}, run_name='__main__')` — **not** imported as modules. Consequences:

- Page files (`pipeline_app.py`, `extract_frames_app.py`, `detect_pockets_app.py`, `cluster_pockets_app.py`, `docking_app.py`, `task_monitor_app.py`) are written to be run as `__main__` and call `initialize_session_state()` from `session_state.py` at the top.
- Programmatic page switches set `st.session_state.pending_nav = "<page label>"` and rerun; `main.py` translates that to `option_menu`'s `manual_select` index.
- Don't `from pipeline_app import …` from another page — module identity is unstable under `runpy`.

### Job IDs and on-disk state

All persistent state for a run lives under a single `job_id` (a UUID generated when the job starts):

- `uploads/<job_id>/...` — user-uploaded inputs (sanitized via `Config.get_upload_path`)
- `results/<job_id>/...` — pipeline outputs (subfolders `pdbs/`, `pockets/`, `pocket_clusters/`, `docking/`)
- `results/<job_id>_status.json` — single source of truth for job state. Written by `tasks._update_status_file(...)` on every state change and by the page modules when they kick off a task. Schema: `{status, step, task_id, result_info, last_updated}`. The Task Monitor page reads these files; the pages also read them to recover state after a rerun.

Page modules pass the `job_id` between steps via `st.session_state.cached_job_ids` (keys: `extract`, `detect`, `cluster`, `docking`, `pipeline`) so a user can run Step 2 against a Step 1 job without re-uploading.

### `tasks.py` conventions

- `_run_stage(celery_task, command, cwd, timeout, prog_start, prog_end, stage_name)` is the only blessed way to run a subprocess inside a task. It redirects stdout/stderr to temp files — **don't switch to `subprocess.PIPE`**, p2rank and smina can emit >64 KB and deadlock the worker on the pipe buffer. It also emits `PROGRESS` state updates on a logarithmic ramp so the UI progress bar never lies by hitting 100% prematurely.
- The pipeline task `run_pockethunter_pipeline` orchestrates extract (0–25%) → detect (25–60%) → cluster (60–80%) → optional dock (80–97%). Per-step tasks (`run_extract_to_pdb_task`, `run_detect_pockets_task`, `run_cluster_pockets_task`, `run_docking_task`) exist for the individual Streamlit pages.
- All tasks call `_update_status_file` on failure to keep the status JSON in sync with Celery's own result backend.

### Configuration (`config.py`)

`Config` is a class with classmethods, not an instance. **It validates on import** — if `PocketHunter/pockethunter.py` is missing, the upload/results dirs can't be created, or the Redis URL is malformed, `from config import Config` will raise `ConfigurationError` and crash the worker/UI at startup. Treat this as the contract: never bypass `Config.validate()`, and always use `Config.get_upload_path(job_id, filename)` / `Config.get_results_path(job_id)` / `Config.get_status_file(job_id)` for path construction (they sanitize filenames and prevent path traversal).

Tunables come from `.env` (see `.env.example`). Notable: `MAX_UPLOAD_SIZE`, `MAX_ZIP_SIZE`, `RATE_LIMIT_*`, `CLEANUP_AFTER_DAYS`, `MAX_DOCKING_PDBS`, `DOCKING_TIMEOUT`, `P2RANK_PATH`, `SMINA_PATH`.

### Security boundary

`security.py` (`FileValidator`, `handle_file_upload_secure`) is the choke point for user input — extension allowlist, size limits, ZIP-bomb checks, path-traversal prevention. Every upload should go through it; do not write raw `st.file_uploader` bytes to disk in new code. User-supplied job IDs (from text inputs) go through `FileValidator.validate_job_id` before any `os.path.join(RESULTS_DIR, job_id, ...)` — see `session_state.render_load_previous_widget` for the canonical input boundary.

`rate_limiter.py` provides `check_upload_rate_limit` / `check_task_rate_limit`, backed by Redis. Pages call these before kicking off Celery tasks. `RATE_LIMIT_ENABLED=false` in `.env` is the local-dev escape hatch.

## External binaries

The pipeline depends on two binaries that are **not** Python packages:

- **p2rank** (`prank`): used inside `PocketHunter/` for pocket prediction. The Dockerfile installs Java; `PocketHunter/first_setup.sh` downloads p2rank into `PocketHunter/tools/p2rank/`.
- **smina**: used by `step4_docking.py` for docking. Compose bind-mounts `/usr/local/bin/smina` from the host read-only into the workers — make sure it's installed on the host before bringing the stack up, or set `SMINA_PATH` to point elsewhere.

`install_docking_deps.sh` is a conda-specific helper (ProDy + OpenBabel) and is orthogonal to the Docker path.
