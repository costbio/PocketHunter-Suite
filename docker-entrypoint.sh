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

# Pass through whatever CMD compose / Dockerfile defined.
exec "$@"
