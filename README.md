# PocketHunter Suite

![CI](https://github.com/costbio/PocketHunter-Suite/actions/workflows/ci.yml/badge.svg)

A Streamlit web frontend for the [PocketHunter](https://github.com/costbio/PocketHunter-Suite/tree/main/PocketHunter) molecular-dynamics pocket-detection pipeline plus
[SMINA](https://sourceforge.net/projects/smina/) docking, designed for **public deployment serving untrusted users on a single
64-core / 128 GB box**. Long-running compute runs in hardened, capability-dropped
worker containers managed by an in-cluster orchestrator with abuse limits and
per-session disk quotas. The persistent 3D viewer is built on
[Mol\*](https://molstar.org/).

> This README is the **local-development** entry point. For public production
> deployment (TLS, reverse proxy, sizing, smoke checks, rollback) follow
> [`docs/deployment.md`](docs/deployment.md) — it is the authoritative guide.

## Quick start (Docker, local dev)

```bash
git clone https://github.com/costbio/PocketHunter-Suite.git
cd PocketHunter-Suite

# .env is gitignored — every fresh clone needs one. POSTGRES_PASSWORD is
# required (docker compose refuses to start without it).
cp .env.example .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -base64 24)|" .env

# Build + bring up all six services. The streamlit container's entrypoint
# auto-runs `alembic upgrade head` against Postgres on every boot, so no
# manual migration step is needed.
docker compose up --build -d
```

The application is then served at **http://localhost:8511** (the host port is
8511; the container exposes 8501).

Verify everything came up cleanly:

```bash
docker compose ps                         # all services healthy
curl -s http://localhost:9001/healthz     # orchestrator + DB + Redis + pools
```

> **Don't pre-create `pgdata/`.** Postgres' `initdb` chowns the data dir to
> UID 70 on first boot — a pre-existing dir owned by the host user blocks
> that, leaving the postgres container in a restart loop.

To tear everything down (keeps data on disk):

```bash
docker compose down            # keeps ./pgdata, ./uploads, ./results
docker compose down -v         # also drops named volumes (redis cache)
```

## Architecture

```
                Browser (Mol* viewer + Streamlit UI)
                              ▲
                              │ TLS (Caddy / nginx in prod)
                              │
                       ┌──────┴───────┐
                       │  Streamlit   │── reads/writes ──┐
                       │  (main.py +  │                  │
                       │  analysis_   │     ┌────────────▼──────────────┐
                       │  app.py +    │     │ Postgres (sessions, jobs) │
                       │  panels/)    │     │ Redis    (broker + cache) │
                       └──────┬───────┘     └────────────▲──────────────┘
                              │ submits Celery tasks      │
                              │                           │
                       ┌──────▼─────────┐                 │
                       │  Orchestrator  │── HTTP /pool/status, /healthz
                       │  (Flask +      │
                       │  Docker SDK)   │── spawns ───────┐
                       └──────┬─────────┘                 │
                              │ via docker-socket-proxy   │
                              │ (allow-listed endpoints)  ▼
                              │           ┌───────────────────────────┐
                              │           │  Hardened worker pools    │
                              │           │  --read-only --cap-drop   │
                              │           │  =ALL --no-new-privileges │
                              │           │  --user 1000 --network    │
                              │           │  =pockethunter_internal   │
                              │           │  (internal: true — no     │
                              │           │  public egress)           │
                              │           │                           │
                              │           │  fast pool ─ default/celery │
                              │           │  docking pool ─ docking    │
                              │           └───────────────────────────┘
                              ▼
                       uploads/<job> → results/<job>
```

The orchestrator's `WORKER_JOBS_BEFORE_RECYCLE` defence-in-depth recycle policy
tears down + replaces each worker container after 10 completed jobs. Per-session
disk quotas, per-pool concurrency caps, per-IP daily session-create caps, and
optional Cloudflare Turnstile CAPTCHA are all wired through `RATE_LIMIT_ENABLED`.

See [`CLAUDE.md`](CLAUDE.md) (contributor reference) and
[`docs/deployment.md`](docs/deployment.md) (production deployment) for depth.

## Services (six)

| Service               | Role                                                            |
|-----------------------|-----------------------------------------------------------------|
| `streamlit`           | Web frontend (auto-migrates on boot via `docker-entrypoint.sh`) |
| `postgres`            | Authoritative store: sessions, jobs                             |
| `redis`               | Celery broker + result backend                                  |
| `orchestrator`        | Spawns + recycles hardened worker containers; exposes `/healthz`, `/pool/status` |
| `docker-socket-proxy` | Tecnativa allow-listed proxy in front of `/var/run/docker.sock` |
| `celery-beat`         | Scheduler for cleanup + quota-enforcement beat tasks            |

Worker containers (`ph-worker-fast-*` / `ph-worker-docking-*`) are **dynamic** —
spawned by the orchestrator's reconcile loop, not declared in
`docker-compose.yml`.

## Repository layout

- **Top level** (`/`) — the Streamlit + Celery suite. This is what you usually edit.
  - `main.py`, `analysis_app.py`, `landing.py` — Streamlit dispatcher + single-page app + landing screen.
  - `panels/{find_pockets,cluster,docking,jobs_panel}.py` — per-stage UI panels.
  - `tasks.py`, `celery_app.py`, `step4_docking.py` — Celery task definitions + docking shell-out.
  - `orchestrator/` — Flask + Docker-SDK service that manages worker pools.
  - `db/`, `alembic/` — SQLAlchemy models + migrations.
  - `components/`, `static/js/molstar-bridge.js` — Mol\* viewer integration.
  - `frontend/src/` — Mol\* bridge source (Vite); pre-built bundle ships in `static/js/`.
  - `security.py`, `rate_limiter.py`, `client_ip.py`, `captcha.py` — input validation + abuse limits.
- **`PocketHunter/`** — vendored CLI tool that the suite shells out to for extract / detect / cluster.
  Has its own `CLAUDE.md` and `requirements.txt`. Treat as a black-box subprocess; only the CLI
  surface is part of the contract.

## Environment variables

Defer to [`.env.example`](.env.example) as the authoritative list. The required
ones (`docker compose up` will refuse without them):

- `POSTGRES_PASSWORD` — Postgres + DATABASE_URL credential. Generate with `openssl rand -base64 24`.
- `BASE_URL` — public URL the app generates share links from (e.g. `https://app.example.com`).

Public-deployment knobs to tune before flipping the stack live:

- `PER_SESSION_DISK_QUOTA_MB`, `MAX_CONCURRENT_FAST_JOBS`, `MAX_CONCURRENT_DOCKING_JOBS`,
  `MAX_CONCURRENT_FAST_PER_SESSION`, `MAX_CONCURRENT_DOCKING_PER_SESSION`,
  `MAX_SESSIONS_PER_IP_PER_DAY` — abuse limits.
- `TURNSTILE_ENABLED`, `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` — Cloudflare CAPTCHA
  (disabled by default for local dev; enable + populate for production).
- `FAST_POOL_SIZE`, `DOCKING_POOL_SIZE`, `WORKER_CPU_LIMIT`, `WORKER_MEMORY_LIMIT` — orchestrator sizing.

## Frontend (Mol\* bridge)

The Mol\* bridge bundle (`static/js/molstar-bridge.js` + `.css`) ships pre-built in
git. Contributors who don't edit it don't need Node. To rebuild after editing
`frontend/src/`:

```bash
cd frontend
npm install
npm run build           # writes the .js + .css into ../static/js/
```

Commit the rebuilt artefacts alongside your source change. Pinned versions live in
`frontend/package.json`. The Dockerfile does NOT install Node — `COPY . .` brings
the pre-built bundle in.

## Testing

```bash
# Inside the running stack
docker compose exec streamlit pip install pytest pytest-mock
docker compose exec streamlit python -m pytest tests/ -q
```

For a CI-sized stack (smaller worker pools fit on a laptop / CI runner) use the
shipped test overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
docker compose exec -T streamlit python -m pytest tests/ -q
```

## Health & operations

```bash
# Deep liveness (200 / 503): postgres + redis + docker + per-pool occupancy
curl -s http://localhost:9001/healthz | python3 -m json.tool

# Worker pool snapshot
curl -s http://localhost:9001/pool/status | python3 -m json.tool

# Hardening smoke: asserts every per-worker flag (--read-only, cap-drop=ALL,
# no-new-privileges, UID 1000, internal-only network, no public egress)
./scripts/verify_hardening.sh
```

`docs/deployment.md` covers TLS reverse proxy, sizing on a 64c / 128 GB box,
production env overrides, backup, rollback, upgrades, and the
`docker-compose.production.yml` overlay that locks down the host-port surface.

## Local development (no Docker)

The orchestrator-managed pool design is Docker-native, but the suite itself runs
fine on a bare host for unit-test / panel-development work:

```bash
pip install -r requirements.txt
redis-server --daemonize yes
# (provision Postgres + run `alembic upgrade head` against it once)

# Queue routing matches celery_app.task_routes — keep these two queue assignments.
celery -A celery_app worker -Q default,celery --concurrency=8 --loglevel=info
celery -A celery_app worker -Q docking --concurrency=4 --loglevel=info
celery -A celery_app beat --loglevel=info
streamlit run main.py --server.port=8501
```

You lose the worker hardening, the recycle policy, and the orchestrator HTTP
API — only run this for development against trusted inputs.

## Production deployment

For public production deployment on a single-box server, follow
[`docs/deployment.md`](docs/deployment.md) — it is the authoritative guide and
covers everything this README intentionally omits (TLS via Caddy, locked-down
host ports via `docker-compose.production.yml`, sizing for 64c / 128 GB,
hardening smoke checklist, log rotation, backup, rollback).

The hardening flag-by-flag checklist lives in [`docs/security.md`](docs/security.md).

## Contributing

Issues and pull requests welcome. Please run the test suite before submitting:
`docker compose exec streamlit python -m pytest tests/ -q`.

Contributor-facing architecture and conventions live in [`CLAUDE.md`](CLAUDE.md).
