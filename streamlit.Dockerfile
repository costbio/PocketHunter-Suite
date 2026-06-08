FROM python:3.10-slim

# System dependencies + build tools + Java (for p2rank).
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

# B11.15: install micromamba (single static binary, ~10 MB) and use it
# to create a coherent docking environment from conda-forge. This
# replaces the previous "bind-mount the host's conda smina + libs"
# approach which created ABI mismatches between smina's bundled
# openbabel and the pip-installed ``openbabel-wheel``. With everything
# from one channel, smina / libopenbabel / pybel all agree on one ABI.
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

# Prepend the conda env's bin/ to PATH so smina, obabel, and python
# resolve to the env's binaries. Subsequent ``pip install`` then
# targets this env's site-packages, keeping everything in one tree.
ENV PATH=/opt/conda/envs/docking/bin:$PATH

WORKDIR /app

# Python deps from PyPI for everything else (streamlit, celery, etc).
# ``openbabel-wheel`` and ``prody`` are no longer here — they're
# already installed in the conda env above with matching ABIs.
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

# Application code
COPY . .

# Bake p2rank into the image. first_setup.sh downloads p2rank 2.5
# (~300 MB) into PocketHunter/tools/p2rank/ and installs PocketHunter's
# own Python deps. Running it during build means workers always have
# p2rank regardless of whether the host has run first_setup.sh.
RUN cd /app/PocketHunter && bash first_setup.sh

# Build-time toolchain cleanup AFTER pip installs are complete.
RUN apt-get purge -y gcc g++ make python3-dev \
    && apt-get autoremove -y

# Runtime directories. The bind-mounted uploads/results/logs in
# docker-compose.yml supersede these at runtime; they exist here so
# the image is self-contained when run standalone (e.g. ad-hoc tests).
RUN mkdir -p uploads results logs \
    && chown -R 1000:1000 /app/uploads /app/results /app/logs

# Non-root runtime user. The worker and orchestrator images both already
# drop privilege; streamlit is the most-exposed surface (reverse proxy
# terminates onto this port) — so it needs the same treatment.
# UID 1000 matches the host bind-mount owner (see docs/deployment.md §1).
# HOME=/tmp because `useradd -M` skips creating /home/streamlituser; the
# few tools that touch $HOME at runtime (pip cache during CI/dev test
# installs, matplotlib's fontcache) need a writable path. /tmp is the
# only writable mount once we drop privilege.
RUN groupadd -g 1000 streamlituser \
    && useradd -u 1000 -g streamlituser -M -s /usr/sbin/nologin streamlituser
ENV HOME=/tmp
USER 1000:1000

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Entrypoint runs `alembic upgrade head` against $DATABASE_URL on every
# boot, then execs the CMD. Idempotent — no-op once schema is current.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["streamlit", "run", "main.py", "--server.port=8501", "--server.address=0.0.0.0"]
