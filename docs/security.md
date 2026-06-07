# Security model + hardening checklist

PocketHunter-Suite v2 was designed to run public on the open internet
with untrusted user inputs (XTC trajectories, PDB ZIPs, SDF ligands).
The chemistry tooling — p2rank (Java + native libraries), smina,
openbabel, mdtraj — are all RCE surfaces that parse user-provided
binary formats. The Phase C design assumes one of them *will* be
compromised at some point and contains the blast radius accordingly.

This document is the operator's checklist: every flag the orchestrator
applies to worker containers, why, and how to verify each is in effect.

## Threat model

**In scope.** A malicious user uploads a crafted file that gives them
code execution inside a worker container. The deployment must:

- Prevent that code from reaching other sessions' data on disk.
- Prevent it from reaching the host filesystem or other host services.
- Prevent it from initiating outbound internet connections (data
  exfiltration, joining a botnet).
- Prevent it from escalating to Docker daemon control (container
  escape).
- Limit the resources one compromised session can consume (CPU, RAM,
  disk, queue slots).

**Out of scope.** A compromise of the orchestrator container itself
(privileged with Docker socket access via the proxy) — we mitigate
that by keeping the orchestrator small (`~150` MB image, Flask + Docker
SDK + a handful of helpers) and never feeding it user input.
Compromise of the host kernel — out of scope; mitigated only by
keeping the host minimal (no chemistry binaries, no user-facing
services besides the reverse proxy).

## Per-worker hardening flags

Every worker container the orchestrator spawns gets the flag set below.
The source is `orchestrator/docker_sdk.py:_spawn_worker`; every flag
exists for a specific failure mode and most were validated end-to-end
in the C0 spike on 2026-05-15.

| Flag | Source line | Purpose | Verify |
|---|---|---|---|
| `--read-only` | `read_only=True` | Root FS is read-only. Even with code execution, an attacker can't drop a persistence implant. | `docker inspect <id> --format '{{.HostConfig.ReadonlyRootfs}}'` → `true` |
| `--cap-drop=ALL` | `cap_drop=["ALL"]` | Worker process has *no* Linux capabilities. Can't bind low ports, can't chown, can't set caps, can't ptrace. | `docker inspect --format '{{.HostConfig.CapDrop}}'` → `[ALL]` |
| `--security-opt=no-new-privileges:true` | `security_opt=[...]` | Even if a setuid binary were present, exec'ing it can't grant capabilities back. Belt-and-braces with the cap-drop. | `docker inspect --format '{{.HostConfig.SecurityOpt}}'` → contains `no-new-privileges:true` |
| `--user 1000:1000` | `user="1000:1000"` | Worker runs as unprivileged UID 1000. Matches host bind-mount ownership; combined with cap-drop=ALL means no path to UID 0 inside the container. | `docker exec <id> id` → `uid=1000(worker) gid=1000(worker)` |
| `--tmpfs /tmp:exec,size=1g` | `tmpfs={"/tmp": ...}` | `/tmp` is a writable tmpfs (so JVM JNI / zstd-jni can `dlopen` extracted libs — C0 found `noexec` breaks p2rank). Capped at 1 GB so a malicious script can't fill disk. | `docker exec <id> mount` shows `/tmp tmpfs (rw,exec,size=1g)` |
| `--tmpfs /scratch:size=4g` | `tmpfs={"/scratch": ...}` | Job-local scratch space, cleared on container teardown. | as above |
| `--memory=<WORKER_MEMORY_LIMIT>` | `mem_limit=...` | Hard memory cap (`OOMKill` when exceeded). Default 12 GB. | `docker inspect --format '{{.HostConfig.Memory}}'` |
| `--cpus=<WORKER_CPU_LIMIT>` | `nano_cpus=...` | CFS quota limits worker to N cores. Default 6. | `docker inspect --format '{{.HostConfig.NanoCpus}}'` |
| `--network=pockethunter_internal` | `network=...` | Worker attaches only to the internal bridge — see below. | `docker inspect --format '{{.NetworkSettings.Networks}}'` |
| `--init` | `init=True` | Run a tini-style PID 1 so reaping zombies + signal propagation work for the celery worker process. | `docker inspect --format '{{.HostConfig.Init}}'` → `true` |
| `--restart=no` | `restart_policy=...` | Workers do *not* auto-restart. The orchestrator decides whether to respawn — restart-loops would hide crashes. | `docker inspect --format '{{.HostConfig.RestartPolicy.Name}}'` → `no` |
| `HOME=/tmp` etc. | env block | Reroutes `~/.config`, `~/.cache`, matplotlib's font cache onto the writable tmpfs. Without these, matplotlib + fontconfig + p2rank's Java wrapper silently fail on first cold-start (`mkdir(~/.config)` fails on read-only root). | `docker exec <id> env | grep -E "HOME|MPLCONFIGDIR|XDG"` |
| `PYTHONDONTWRITEBYTECODE=1` | env block | Python can't write `__pycache__` into the read-only `/app` mount. Combined with the build-time `python -m compileall /app` step. | `docker exec <id> env | grep PYTHONDONTWRITEBYTECODE` |

**Verify in one shot:**

```bash
WORKER=$(docker ps -q --filter "label=pockethunter.role=worker" | head -1)
docker inspect $WORKER --format '
ReadonlyRootfs : {{.HostConfig.ReadonlyRootfs}}
CapDrop        : {{.HostConfig.CapDrop}}
SecurityOpt    : {{.HostConfig.SecurityOpt}}
User           : {{.Config.User}}
NetworkMode    : {{.HostConfig.NetworkMode}}
Privileged     : {{.HostConfig.Privileged}}
Init           : {{.HostConfig.Init}}
'
```

A correctly-configured worker prints:

```
ReadonlyRootfs : true
CapDrop        : [ALL]
SecurityOpt    : [no-new-privileges:true]
User           : 1000:1000
NetworkMode    : pockethunter_internal
Privileged     : false
Init           : true
```

Any deviation means somebody edited the orchestrator's spawn path
without going through the security review — track it down before
serving public traffic.

## Network isolation

Three networks; the diagram in `CLAUDE.md` shows the wiring:

- **`pockethunter_internal`** (`internal: true`). Workers, Streamlit, Postgres, Redis, beat, orchestrator. `internal: true` blocks **all outbound traffic to the public internet**, including DNS to 8.8.8.8. Workers can still reach `redis:6379` and `postgres:5432` because Docker's embedded resolver handles intra-network lookups locally.
- **`pockethunter_docker_proxy`**. Only the orchestrator and `docker-socket-proxy` join this. Workers cannot reach the proxy and so cannot talk to the Docker daemon even if compromised.
- **`default`** (the compose-named bridge). Streamlit + Postgres + Redis also attach here, but workers do NOT. Legacy convenience for `pgsql`/`redis-cli` over host port-mappings during development; production should drop those mappings (`docker-compose.production.yml` in `docs/deployment.md`).

**Verify** no public egress from a worker:

```bash
WORKER=$(docker ps -q --filter "label=pockethunter.role=worker" | head -1)
docker exec $WORKER python -c "import socket; print(socket.create_connection(('1.1.1.1', 443), timeout=2))" 2>&1
# Expected: socket.timeout / OSError: Network is unreachable / Name does not resolve
```

If that succeeds the network isn't internal.

## Docker socket containment

The orchestrator never has `/var/run/docker.sock` mounted. It talks to
`tcp://docker-socket-proxy:2375` — a `tecnativa/docker-socket-proxy`
sidecar that holds the actual socket and exposes only the API endpoints
listed in its environment block (`CONTAINERS`, `NETWORKS`, `IMAGES`,
`INFO`, `PING`, `POST`).

Effect: even a fully-compromised orchestrator container can only do
what the proxy allows. It cannot read host secrets, cannot mount the
host filesystem, cannot create privileged containers (the proxy doesn't
expose those endpoints).

**Verify** by trying a banned endpoint:

```bash
docker compose exec orchestrator curl -s -w "\nHTTP %{http_code}\n" http://docker-socket-proxy:2375/v1.41/secrets
# Expected: HTTP 403
```

## Application-layer abuse mitigations (Phase C C4)

The container hardening contains a compromise *after* an attacker gets
code execution. The C4 mitigations make code execution harder to obtain
and limit a single attacker's blast radius before that point:

| Mitigation | Knob | Where |
|---|---|---|
| Per-session disk quota | `PER_SESSION_DISK_QUOTA_MB` | `security.handle_file_upload_secure(session_id=)`; periodic enforcement via `cleanup_job.enforce_session_disk_quotas_task` |
| Global per-pool concurrency cap | `MAX_CONCURRENT_FAST_JOBS`, `MAX_CONCURRENT_DOCKING_JOBS` | `panels/_shared.assert_submit_allowed` |
| Per-session-per-pool concurrency cap | `MAX_CONCURRENT_FAST_PER_SESSION`, `MAX_CONCURRENT_DOCKING_PER_SESSION` | same |
| Per-IP daily session-create limit | `MAX_SESSIONS_PER_IP_PER_DAY` | `rate_limiter.check_session_create_rate_limit(ip)` |
| Upload extension allowlist + ZIP-bomb check | `Config.ALLOWED_UPLOAD_EXTENSIONS` | `security.FileValidator` |
| Path traversal prevention | hardcoded | `security.FileValidator.validate_filename` + `validate_job_id` |
| Sliding-window per-browser rate limits | `RATE_LIMIT_*` | `rate_limiter` (legacy) |

All gated by `RATE_LIMIT_ENABLED` — set to `true` for production.
**Fail-open** is intentional: if Redis or Postgres hiccups, the
limiter logs a warning and allows the request. The orchestrator's
pool-size caps still bound actual concurrency. This trades a small
window of over-permissiveness for never-locked-out users during a
broker hiccup.

## Logging hygiene

`pockethunter-suite.log` collects per-task log lines, including job IDs
and partial inputs. In dev it's fine; for a public deployment:

1. Confirm it's `.gitignore`d (Phase C C6 untracks it).
2. Configure logrotate on the host so the file doesn't grow unbounded — see `docs/deployment.md § Logs`.
3. If you ship logs to a central aggregator, scrub any PII the app puts in there (no current code paths log user IPs to this file, but watch for drift).

## What's NOT covered by this design

- **The Streamlit process itself is shared across sessions.** It does not directly execute user uploads (the task workers do), but a Streamlit bug parsing a malicious URL could affect other users. Mitigation: keep Streamlit pinned, watch CVEs; if traffic outgrows acceptable risk, Phase D's multi-replica split is the right answer.
- **The host kernel.** Container hardening relies on the kernel's correctness. Run a recent stable kernel, keep `unattended-upgrades` on for security patches.
- **DDoS at the network edge.** Caddy / nginx terminate at the proxy; if you face L3/L4 floods, put a CDN or anti-DDoS service in front.
- **Phishing of legitimate session URLs.** A `?edit=` URL is the only credential — anyone who has it can edit. There's no recovery if a user pastes their editor URL in public. The strategy doc covers the future migration to real auth.

## Open follow-ups

- **CAPTCHA on session-create**: flagged but not wired. Watch abuse logs first; the per-IP daily cap covers the common case.
- **MIME sniffing on uploads**: the extension allowlist is 90% of the protection. Adding `python-magic` would harden against double-extension tricks. Not wired in C4 — `requirements.txt` doesn't ship the lib by default.
- **Audit log for editor actions**: every editor mutation (`is_editor=True` path) is anonymous to the server today. A future commit could log `(session_id, ip, action, timestamp)` rows for forensics.
