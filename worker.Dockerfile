# Phase C C1: slim worker image — Celery + chemistry stack only.
#
# Distinct from streamlit.Dockerfile: NO Streamlit / Streamlit-* / py3Dmol /
# frontend Mol* bridge. Hardening-ready: non-root ``worker`` user (UID 1000
# matching the host bind-mount owner), PYTHONDONTWRITEBYTECODE, precompiled
# bytecode so the runtime can use --read-only without __pycache__ writes.
#
# The C0 spike (2026-05-15) confirmed this stack runs cleanly under
# --read-only --cap-drop=ALL --security-opt=no-new-privileges --user 1000:1000,
# provided /tmp is mounted exec (Java JNI / zstd-jni) and HOME points at a
# writable tmpfs. Those flags are applied by the C2 orchestrator at spawn
# time; this Dockerfile sets the in-image prerequisites.

FROM python:3.10-slim

# System deps + Java (for p2rank). Identical to streamlit.Dockerfile minus
# anything UI-specific. ``libxrender1`` + ``libxext6`` stay — matplotlib uses
# them for the PNG backend that PocketHunter's ``plot_clustermap`` invokes
# inside the cluster task.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    bzip2 \
    ca-certificates \
    gcc \
    g++ \
    make \
    python3-dev \
    libxrender1 \
    libxext6 \
    libmagic1 \
    default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# B11.15 conda stack: smina + openbabel + prody with matching ABIs.
RUN curl -L --retry 5 --retry-delay 3 --max-time 120 \
        https://micro.mamba.pm/api/micromamba/linux-64/latest \
    | tar -xj -C /usr/local bin/micromamba

ENV MAMBA_ROOT_PREFIX=/opt/conda
RUN micromamba create -y -n docking -c conda-forge \
        python=3.10 \
        smina \
        openbabel \
        prody \
    && micromamba clean -ayfp

ENV PATH=/opt/conda/envs/docking/bin:$PATH

WORKDIR /app

# Worker-only Python deps (no streamlit / py3Dmol / etc.).
COPY worker-requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r worker-requirements.txt

# Application code.
COPY . .

# Bake p2rank into the image — same script the streamlit image runs.
RUN cd /app/PocketHunter && bash first_setup.sh

# Build-time toolchain cleanup.
RUN apt-get purge -y gcc g++ make python3-dev \
    && apt-get autoremove -y

# ── Hardening prep (C0-validated) ────────────────────────────────────
# Non-root worker user, UID matches the host's bind-mount owner so the
# bind-mounted uploads/results stay readable+writable. ``-M`` because the
# orchestrator passes ``HOME=/tmp`` (writable tmpfs); no /home/worker needed.
RUN groupadd -g 1000 worker \
    && useradd -u 1000 -g worker -M -s /usr/sbin/nologin worker

# Precompile bytecode so the read-only root FS at runtime never tries to
# write __pycache__ during import. Plus the env var as belt-and-braces.
ENV PYTHONDONTWRITEBYTECODE=1
RUN python -m compileall -q /app 2>/dev/null || true

# Runtime dirs the task code expects. The orchestrator bind-mounts the
# whole repo onto /app:ro at run time, so the image-baked ownership of
# /app is overridden anyway — we only need /app/{uploads,results,logs}
# to exist at the right paths so the bind-mounts have targets. No
# chown -R: the C3 build observed that walking /app's ~500 MB tree
# (source + p2rank) duplicated every file into a new layer; with this
# step dropped, the same image lands ~500 MB smaller.
RUN mkdir -p uploads results logs

USER worker

# Default command — celery worker on the legacy ``default``/``celery`` queue
# (so the existing celery-worker service in docker-compose.yml runs
# unchanged). The C3 orchestrator overrides this per spawned container,
# passing ``-Q docking`` for the docking pool.
CMD ["celery", "-A", "celery_app", "worker", "-Q", "default,celery", "--concurrency=4", "--loglevel=info"]
