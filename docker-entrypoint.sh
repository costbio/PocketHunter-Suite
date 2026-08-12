#!/usr/bin/env bash
# docker-entrypoint.sh — streamlit container init.
#
# Runs Alembic migrations against $DATABASE_URL before exec'ing whatever
# command Compose passes in (defaults to `streamlit run main.py ...`).
# Without this step, main.py's module-level call to
# `resolve_session_from_query` issues a SELECT against the `sessions`
# table before any user interaction — on a fresh Postgres volume the
# table doesn't exist and the first page load crashes with
# `psycopg.errors.UndefinedTable`.
#
# Idempotent: `alembic upgrade head` is a no-op when the schema is
# already at the latest revision, so every container restart is safe.
# Workers do NOT use this entrypoint — only the streamlit image does;
# they have no migration concern.

set -euo pipefail

cd /app

echo "[entrypoint] applying database migrations (alembic upgrade head)..."
alembic upgrade head
echo "[entrypoint] migrations complete."

# Patch Streamlit's index.html: replace default favicon with ours,
# and inject early shell CSS to hide the toolbar from first paint.
ST_INDEX="/opt/conda/envs/docking/lib/python3.10/site-packages/streamlit/static/index.html"
if [ -f "$ST_INDEX" ] && [ -w "$ST_INDEX" ]; then
    sed -i 's|href="./favicon.png"|href="/app/static/favicon.svg"|' "$ST_INDEX"
    sed -i 's|</head>|<link rel="icon" type="image/svg+xml" href="/app/static/favicon.svg"><style>header[data-testid="stHeader"]{display:none!important}.stMainBlockContainer,.main .block-container{padding-top:0.6rem!important}.stElementContainer.st-key-recent_sessions_cm{visibility:hidden!important;height:0!important;min-height:0!important;margin:0!important;padding:0!important;overflow:hidden!important}</style></head>|' "$ST_INDEX"
    echo "[entrypoint] patched Streamlit index.html (favicon + early CSS)."
else
    echo "[entrypoint] WARNING: cannot patch $ST_INDEX (missing or read-only)."
fi

# Pass through whatever CMD compose / Dockerfile defined.
exec "$@"
