#!/usr/bin/env bash
# scripts/install_logrotate.sh — install the production logrotate config
# for pockethunter-suite logs.
#
# The streamlit + worker + orchestrator containers all bind-mount the
# repo's logs/ directory and write to /app/logs/pockethunter-suite.log
# via logging_config.py's rotating handler. The IN-CONTAINER rotation
# is just a memory-cap safeguard — the file ultimately lives on the
# host and grows unbounded unless host-side logrotate rotates it.
#
# This script drops the canonical config into /etc/logrotate.d/
# (compatible with Ubuntu / Debian).
#
# Usage:
#     sudo ./scripts/install_logrotate.sh
#
# Notes:
#   - Idempotent: re-running overwrites the existing config.
#   - `copytruncate` is critical — the workers don't reopen the file
#     on rotation; truncating in place keeps the FD valid.
#   - The path embedded below is resolved from $REPO_ROOT (one level
#     up from this script). Override LOG_ROOT to point logrotate at
#     a different location.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_ROOT="${LOG_ROOT:-${REPO_ROOT}/logs}"
TARGET="/etc/logrotate.d/pockethunter"

if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (writes to /etc/logrotate.d/)." >&2
    echo "Try:  sudo $0" >&2
    exit 1
fi

if [[ ! -d "$LOG_ROOT" ]]; then
    echo "warning: $LOG_ROOT does not exist yet — creating it." >&2
    mkdir -p "$LOG_ROOT"
fi

cat > "$TARGET" <<EOF
# Managed by pockethunter-suite/scripts/install_logrotate.sh
# Source of truth for the rotation policy lives there.
${LOG_ROOT}/pockethunter-suite.log {
    weekly
    rotate 8
    compress
    delaycompress
    notifempty
    missingok
    copytruncate
}
EOF

chmod 644 "$TARGET"

echo "Installed: $TARGET"
echo "  rotates: ${LOG_ROOT}/pockethunter-suite.log"
echo "  policy:  weekly, keep 8, compress, copytruncate"
echo
echo "Validate:    sudo logrotate -d $TARGET"
echo "Force-run:   sudo logrotate -f $TARGET"
