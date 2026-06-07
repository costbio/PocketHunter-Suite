# Deployment guide — single-box public server

This document covers the production deployment target the v2 refactor was
designed for: **one Linux host, 64 cores / 128 GB RAM, behind a TLS
reverse proxy, serving untrusted public traffic.** Anything smaller
(personal use, internal team) still works — pick smaller `_POOL_SIZE`
defaults — but the hardening, network isolation, and abuse limits are
all wired for hostile workloads.

If you're deploying to multiple hosts or Kubernetes, you've outgrown the
plan that shipped in Phase C; the `orchestrator/k8s.py` stub flags
where that work would slot in.

## 1. Host prep

A clean Ubuntu 22.04 / Debian 12 box is the assumed baseline. You need:

```bash
# Docker engine + compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out and back in

# Required because the orchestrator spawns containers as UID 1000 and
# bind-mounts ./uploads ./results ./logs RW. The mount-target dirs must
# be writable by 1000 — easiest if your deploy user IS uid 1000.
id   # should show uid=1000(<your-user>)
```

If your deploy user is **not** uid 1000, two options:

1. Create a `pockethunter` user with `useradd -u 1000 -m pockethunter` and run the stack as them.
2. Edit `orchestrator/docker_sdk.py:_spawn_worker` to use a different UID matching your host user. Match the Dockerfile too (`worker.Dockerfile` creates `worker:worker` at UID 1000) — or rebuild the image with a different UID.

The host needs **no chemistry tooling installed** (p2rank, smina, openbabel, prody all live inside the worker image). It only needs Docker + a reverse proxy.

### Docker Desktop on WSL2 (development only)

The production target is a real Linux box where `/var/run/docker.sock` is a regular Unix socket. On a developer's machine running Docker Desktop on WSL2, the socket is *forwarded through* `/run/desktop/mnt/host/wsl/docker-desktop-bind-mounts/<distro>/docker.sock` — a path that the WSL2 layer occasionally fails to resolve immediately after a `compose down`. When that happens, `pockethunter-docker-proxy` exits with code 127 (`mount: not a directory: Are you trying to mount a directory onto a file?`) and the orchestrator can never connect.

Mitigation: the proxy now has a `wget /_ping` healthcheck and the orchestrator's `depends_on` waits for `service_healthy`, so a broken proxy keeps the orchestrator in a clean waiting state instead of restart-looping. When this happens in dev, just retry the proxy:

```bash
docker rm -f pockethunter-docker-proxy
docker compose up -d docker-socket-proxy
```

The second attempt almost always succeeds — WSL2's bind-mount shim is reconciled by then. **On real Linux this issue does not exist.**

## 2. Repo + environment

```bash
git clone <repo>
cd pockethunter-suite
cp .env.example .env
```

Open `.env` and set the production values. The required overrides are:

```ini
# Required. Generate a strong password — the compose file's ${VAR:?…}
# substitution causes `docker compose up` to refuse to start without it.
# Both the Postgres container and DATABASE_URL read this single value.
POSTGRES_PASSWORD=$(openssl rand -base64 24)

# Required. The DATABASE_URL line in .env.example references the
# POSTGRES_PASSWORD above via shell substitution — leave it as shipped
# unless you point Postgres at a different host.
DATABASE_URL=postgresql+psycopg://pockethunter:${POSTGRES_PASSWORD}@postgres:5432/pockethunter

# Required.
BASE_URL=https://your-domain.example.com

# Optional. Host path where Postgres stores its data (bind-mounted
# into the container). Defaults to ./pgdata next to the compose file —
# fine for dev. Production deployments commonly point this at a
# dedicated mount, e.g. /var/lib/pockethunter/pgdata on a data disk.
# Caveat: don't pre-create the directory; Postgres' initdb chowns it
# to UID 70 on first boot and refuses if a non-empty/wrong-owner
# directory is already there.
POSTGRES_DATA_DIR=/var/lib/pockethunter/pgdata

# Phase C: orchestrator owns the pools. Defaults below sized for 64c/128GB.
USE_ORCHESTRATOR=true
FAST_POOL_SIZE=6           # 6 × 6 cores = 36 cores for pipeline tasks
DOCKING_POOL_SIZE=3        # 3 × 6 cores = 18 cores for docking
WORKER_CPU_LIMIT=6         # per-worker --cpus
WORKER_MEMORY_LIMIT=12g    # per-worker --memory (9 × 12 GB = 108 GB)
WORKER_JOBS_BEFORE_RECYCLE=10  # defence-in-depth refresh cadence

# Public-deployment abuse limits — tune to your traffic.
PER_SESSION_DISK_QUOTA_MB=5000
MAX_CONCURRENT_FAST_JOBS=60
MAX_CONCURRENT_DOCKING_JOBS=30
MAX_CONCURRENT_FAST_PER_SESSION=2
MAX_CONCURRENT_DOCKING_PER_SESSION=1
MAX_SESSIONS_PER_IP_PER_DAY=20
RATE_LIMIT_ENABLED=true
```

**Sizing math for the default 64c/128GB box** with the defaults above:

| Component | Cores | Memory |
|---|---|---|
| 6 fast workers × 6/12 | 36 | 72 GB |
| 3 docking workers × 6/12 | 18 | 36 GB |
| Streamlit + orchestrator + beat + postgres + redis + proxy | ~6 | ~10 GB |
| Reserved for OS / kernel / page cache | ~4 | ~10 GB |
| **Total** | **~64** | **~128 GB** |

Docker caps with `--cpus` and `--memory` so peak headroom is fine; the workers spend most of their time well under those ceilings. If you have a smaller box, halve both `_POOL_SIZE` knobs first; cutting `WORKER_CPU_LIMIT` is the second knob (cutting cores per task starves the chemistry inside).

## 3. Lock down host-port exposure

The default `docker-compose.yml` keeps host-port mappings on Redis (6380) and Postgres (5433) for *local-development convenience* — they let you point `psql` / `redis-cli` from the host. **For public deploy these MUST go.** Create `docker-compose.production.yml` next to the main compose:

```yaml
# docker-compose.production.yml — overlay for the public deployment.
# Use as:  docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
services:
  redis:
    # Remove host-port mapping — internal-only on pockethunter_internal.
    ports: !reset []
  postgres:
    ports: !reset []
  orchestrator:
    # The /pool/status + /healthz API stays internal-only too — only the
    # reverse proxy on the host should know how to reach it.
    ports: !reset []
  streamlit:
    # Keep port 8501 INSIDE the bridge network. The reverse proxy on the
    # host (next section) is the only external entry point.
    ports: !reset
      - "127.0.0.1:8511:8501"
```

`!reset` is a docker-compose v2 directive that clears the inherited list. Streamlit binds to 127.0.0.1 only — only the host-local reverse proxy reaches it.

## 4. Reverse proxy + TLS

Run a TLS-terminating reverse proxy on the host that forwards to Streamlit on `127.0.0.1:8511`. Caddy is the lowest-friction choice — auto-renews ACME certificates and handles WebSocket upgrades that Streamlit needs.

Install Caddy on the host (not inside Docker — it needs to bind to :443 and you don't want to put TLS keys inside a container):

```bash
sudo apt install caddy
```

Copy the shipped template + set the hostname:

```bash
sudo cp Caddyfile.example /etc/caddy/Caddyfile
echo 'DOMAIN=app.your-domain.example.com' | sudo tee -a /etc/caddy/Caddyfile.env
sudo systemctl reload caddy
```

`Caddyfile.example` (at the repo root) is the production-ready template — it sets up WebSocket-aware `reverse_proxy`, propagates `X-Forwarded-For` / `X-Real-IP` (the suite's per-IP rate limiter depends on these), raises the request-body limit to match `MAX_UPLOAD_SIZE`, and writes a rotated access log to `/var/log/caddy/`. The single substitution it needs is `{$DOMAIN}` — pulled from `/etc/caddy/Caddyfile.env`.

The `X-Forwarded-For` / `X-Real-IP` headers are what `client_ip.py` reads to enforce the per-IP session-create cap — they MUST be set by the proxy or every request looks like it came from the same IP.

## 5. First-time bring-up

```bash
# Build the images. The worker image is ~7 GB (chemistry stack + p2rank).
docker compose -f docker-compose.yml -f docker-compose.production.yml build

# Apply the database schema.
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm streamlit alembic upgrade head

# Start the stack.
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d

# Watch the orchestrator come up and spawn 6 fast + 3 docking workers.
docker compose logs -f orchestrator
```

When the orchestrator says `spawned worker fast-<id>` nine times, the worker pool is alive.

**Mandatory hardening smoke before pointing DNS at the box:**

```bash
./scripts/verify_hardening.sh
```

This asserts every flag from `docs/security.md § Per-worker hardening flags` against a live worker — `--read-only`, `cap-drop=ALL`, `no-new-privileges:true`, UID 1000, the internal-only network, `Init`, `/tmp:exec` tmpfs — AND that the worker cannot reach the public internet (a TCP connect to 1.1.1.1:443 must fail). Exit 0 means the box is safe to publish; exit 1 means a flag is missing or `pockethunter_internal` isn't isolating workers — investigate before any user sees the URL.

Then visit `https://your-domain.example.com` — the landing page should load. Click **Start new analysis** to confirm the session-create flow works end-to-end.

## 6. Smoke / monitoring

```bash
# Deep health check — useful for uptime monitoring (200 vs 503).
curl https://your-domain.example.com/orchestrator/healthz   # only if reverse-proxied
curl 127.0.0.1:9001/healthz                                  # direct, on the host

# Worker pool snapshot.
curl 127.0.0.1:9001/pool/status | python3 -m json.tool
```

### Local pytest run

The committed `docker-compose.test.yml` is an override that shrinks the orchestrator pools (`FAST_POOL_SIZE=2`, `DOCKING_POOL_SIZE=1`, smaller per-worker caps) so the full stack fits on a CI runner or a laptop. To run the unit tests against the stack you'd ship:

```bash
export POSTGRES_PASSWORD=devpassword
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
docker compose exec -T streamlit pip install pytest pytest-mock flask 'docker>=7.1,<8.0'
docker compose exec -T streamlit python -m pytest tests/ -q
```

CI (`.github/workflows/ci.yml`) runs exactly this sequence on every push.

Point your monitoring stack at `/healthz` — it returns 200 with `{"status":"ok", "checks":{...}}` when every component is reachable, 503 with the failing component named when something's off. Sample failure body:

```json
{"status":"unhealthy","checks":{"redis":{"ok":false,"error":"ConnectionError: …"},
                                 "db":{"ok":true},"docker":{"ok":true},
                                 "pools":{"ok":true,"detail":{"fast":{"desired":6,"actual":6},
                                                                "docking":{"desired":3,"actual":3}}}}}
```

## 7. Logs

Container logs go to Docker's JSON driver by default. On a public deploy you probably want them shipped to a central log aggregator (Loki, Vector, etc.); the orchestrator's reconcile-loop output is the most useful single stream.

Inside the workers, application logs land in `/app/logs/pockethunter-suite.log` (bind-mounted to `./logs/` on the host) via the standard `logging_config.py` rotating handler. Add a logrotate entry so the host-side file doesn't grow unbounded:

```
# /etc/logrotate.d/pockethunter
/path/to/pockethunter-suite/logs/pockethunter-suite.log {
    weekly
    rotate 8
    compress
    delaycompress
    notifempty
    missingok
    copytruncate
}
```

`copytruncate` is critical — the workers don't reopen the file on rotation; truncating in place keeps the handle valid.

## 8. Upgrades

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.production.yml build
docker compose -f docker-compose.yml -f docker-compose.production.yml run --rm streamlit alembic upgrade head
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

Image rebuilds only swap the *image* tag in compose; the orchestrator will recycle workers naturally over the next `WORKER_JOBS_BEFORE_RECYCLE` runs, or you can force a recycle wave:

```bash
docker rm -f $(docker ps -aq -f label=pockethunter.role=worker)
# Reconcile loop respawns them off the new image within 30 s.
```

The `streamlit` container restart-policy is `unless-stopped`, so compose handles it.

**Image hygiene.** Older builds (pre-`image:` directive in compose) left a `pockethunter-suite-celery-beat:latest` tag pointing at the same content as `pockethunter-worker:latest` — two tags for one image. If you see both in `docker images`, drop the duplicate to reclaim ~7 GB on the host:

```bash
docker rmi pockethunter-suite-celery-beat:latest 2>/dev/null || true
```

Safe — `pockethunter-worker:latest` is the canonical tag every service references today.

## 9. Rollback

```bash
git log --oneline | head        # find the SHA to rollback to
git checkout <SHA>
docker compose -f docker-compose.yml -f docker-compose.production.yml build
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --force-recreate
```

If the breakage is specifically the orchestrator architecture and you need to bail out to the B-era two-worker design, `git revert` the C3 commit — `celery-worker` and `celery-docking-worker` come back, the orchestrator is still present but harmless with `USE_ORCHESTRATOR=false`.

## 10. Backup

The Postgres named volume `pgdata` holds session + job metadata; `./results/` and `./uploads/` hold the on-disk artefacts referenced from those rows.

```bash
# Postgres
docker compose exec postgres pg_dump -U pockethunter pockethunter | gzip > pg_$(date +%F).sql.gz

# Filesystem (results + uploads — exclude logs if you ship logs elsewhere)
tar -czf data_$(date +%F).tar.gz results/ uploads/
```

Restore by re-importing the dump (`docker compose exec -T postgres psql -U pockethunter < dump.sql`) and untarring the data archive *before* bringing the stack up. The two need to be in sync — a Postgres row pointing at a missing `results/<job_id>/` is recoverable but ugly.

## Open follow-ups not addressed in this guide

- **CAPTCHA on session-create** — flagged in the v2 strategy as a follow-up to watch abuse-log volume first. Wire it into `landing._create_session_with_rate_limit` if you see abuse patterns.
- **Multi-replica Streamlit** — the single shared process is the bottleneck if traffic outgrows one box. Phase D, not Phase C.
- **k8s orchestrator backend** — stub only at `orchestrator/k8s.py:K8sOrchestrator`. Replace `NotImplementedError` with a real Job/Pod-based implementation when scaling beyond one host.
