#!/usr/bin/env bash
# scripts/backup.sh — automated backup of the suite's stateful data.
#
# Bundles two artefacts into BACKUP_DIR:
#   1. Postgres dump  → pg_<UTC-date>.sql.gz
#         (covers the authoritative sessions + jobs tables)
#   2. Filesystem tar → data_<UTC-date>.tar.gz
#         (covers ./results and ./uploads referenced from those rows)
#
# Run on a host cron — daily is typical. The artefacts need to be in
# sync: a Postgres row pointing at a missing results/<job_id>/ is
# recoverable but ugly. This script dumps Postgres FIRST, then tars
# the on-disk dirs, so a backup taken mid-job has the disk artefacts
# ahead of (or equal to) the DB rows — restore works.
#
# Retention: keep the most recent KEEP backups of each type; older
# ones are deleted at the end. Default KEEP=14 (two weeks of dailies).
#
# Usage:
#     ./scripts/backup.sh
#     BACKUP_DIR=/mnt/backup KEEP=30 ./scripts/backup.sh
#
# Cron example (daily at 03:15):
#     15 3 * * *  /path/to/pockethunter-suite/scripts/backup.sh \
#                   >> /var/log/pockethunter-backup.log 2>&1
#
# For off-site storage, pipe the resulting files to your cloud-uploader
# of choice (rclone / aws s3 cp / restic) after this script finishes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups}"
KEEP="${KEEP:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

cd "$REPO_ROOT"

# 1. Postgres dump. Uses the running postgres container's pg_dump so we
# don't need a host-side Postgres install. The compose project name is
# inferred from `cd`-ing into the repo root.
echo "[$(date -u +%FT%TZ)] dumping postgres..."
docker compose exec -T postgres pg_dump -U pockethunter pockethunter \
    | gzip > "${BACKUP_DIR}/pg_${STAMP}.sql.gz"
echo "  -> ${BACKUP_DIR}/pg_${STAMP}.sql.gz ($(du -h "${BACKUP_DIR}/pg_${STAMP}.sql.gz" | cut -f1))"

# 2. Results + uploads tarball. Logs and pgdata are excluded — logs are
# rotated separately, pgdata is covered by the Postgres dump.
echo "[$(date -u +%FT%TZ)] tarring results/ + uploads/..."
tar --exclude='./logs' --exclude='./pgdata' --exclude='./backups' \
    -czf "${BACKUP_DIR}/data_${STAMP}.tar.gz" \
    results uploads 2>/dev/null || {
    echo "warning: results/ or uploads/ missing — backing up whichever exists." >&2
    [[ -d results ]] && tar -czf "${BACKUP_DIR}/data_${STAMP}.tar.gz" results
    [[ -d uploads ]] && tar -rzf "${BACKUP_DIR}/data_${STAMP}.tar.gz" uploads 2>/dev/null || true
}
echo "  -> ${BACKUP_DIR}/data_${STAMP}.tar.gz ($(du -h "${BACKUP_DIR}/data_${STAMP}.tar.gz" | cut -f1))"

# 3. Retention sweep — keep the most recent $KEEP of each type.
echo "[$(date -u +%FT%TZ)] retention sweep (keep last ${KEEP})..."
for prefix in pg_ data_; do
    ls -1t "${BACKUP_DIR}/${prefix}"*.gz 2>/dev/null \
        | tail -n +$((KEEP + 1)) \
        | xargs -r rm -v
done

echo "[$(date -u +%FT%TZ)] backup complete."
echo
echo "Restore procedure (full):"
echo "  1. docker compose down -v                 # wipe live state"
echo "  2. rm -rf results uploads && tar xzf data_${STAMP}.tar.gz"
echo "  3. docker compose up -d postgres"
echo "  4. zcat pg_${STAMP}.sql.gz | docker compose exec -T postgres psql -U pockethunter pockethunter"
echo "  5. docker compose up -d                   # full stack with restored data"
