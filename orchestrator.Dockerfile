# Phase C C2: orchestrator image — Flask + Docker SDK only.
#
# Tiny by design (~150 MB). Does NOT include the chemistry stack from
# worker.Dockerfile — the orchestrator never runs jobs, it only manages
# the lifecycle of containers that do. Keeping the surface small means
# fewer libraries to keep patched on a public-facing box.
#
# The orchestrator container itself is not hardened with --read-only /
# cap-drop=ALL the way workers are: it needs to talk TCP to the
# docker-socket-proxy, which docker-py opens as a long-lived connection.
# The privilege escalation surface is instead contained by the proxy's
# endpoint allowlist (see docker-compose.yml).

# Match the worker image's base so we don't re-pull a different slim variant.
FROM python:3.10-slim

# Minimum runtime deps. We do NOT install gcc/build tools — wheels exist
# for every pin below, so the image stays small and toolchain-free.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pinned floor versions matching the rest of the suite. docker SDK is
# the only new dependency vs the main worker image.
RUN pip install --no-cache-dir \
        "flask>=3.0,<4.0" \
        "docker>=7.1,<8.0" \
        "pydantic-settings>=2.0,<3.0" \
        "psycopg[binary]>=3.1,<4.0" \
        "sqlalchemy>=2.0,<3.0" \
        "redis>=4.6,<5.0"

# Application code: orchestrator package + the settings module it
# imports. The whole repo gets bind-mounted at runtime too (read-only),
# so this COPY is really just the bake-time fallback for an image-only
# launch.
COPY orchestrator/ /app/orchestrator/
COPY settings.py config.py /app/

# Non-root user for defence-in-depth. UID 1001 to keep clear of the
# worker's UID 1000 (different host bind-mount semantics).
RUN groupadd -g 1001 orch \
    && useradd -u 1001 -g orch -M -s /usr/sbin/nologin orch
USER orch

EXPOSE 9000

# Reconcile loop + Flask are both inside the same process; see __main__.
CMD ["python", "-m", "orchestrator"]
