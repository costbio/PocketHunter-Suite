#!/usr/bin/env bash
# Phase C deployment smoke: assert every hardening flag is actually applied
# to a live orchestrator-spawned worker container, AND that the worker can't
# reach the public internet from inside the pockethunter_internal network.
#
# Run AFTER `docker compose up -d` brings the stack online, BEFORE pointing
# DNS at the box. Exit code 0 means the worker hardening is intact end-to-end.
#
# Usage:
#     ./scripts/verify_hardening.sh
#
# What gets checked (per docs/security.md § Per-worker hardening flags):
#     ReadonlyRootfs == true
#     CapDrop        == [ALL]
#     SecurityOpt    contains no-new-privileges:true
#     User           == 1000:1000
#     NetworkMode    == pockethunter_internal
#     Privileged     == false
#     Init           == true
# Plus a runtime check: socket.create_connection(('1.1.1.1', 443)) must fail.

set -euo pipefail

# ── colors / output helpers ─────────────────────────────────────────────
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }

failures=0
pass() { green "  PASS  $*"; }
fail() { red   "  FAIL  $*"; failures=$((failures + 1)); }

# ── find one live worker container ──────────────────────────────────────
WORKER=$(docker ps -q --filter "label=pockethunter.role=worker" | head -1)
if [[ -z "$WORKER" ]]; then
    red "No orchestrator-spawned worker is running (no container with label pockethunter.role=worker)."
    red "Bring the stack up first: docker compose up -d"
    exit 2
fi

short=$(docker inspect --format '{{.Name}}' "$WORKER" | sed 's|^/||')
yellow "Inspecting worker container: $short  ($WORKER)"
echo

# ── flag-by-flag asserts ────────────────────────────────────────────────
yellow "== Container flags (docker inspect) =="

check() {
    local label="$1" expected="$2" got="$3"
    if [[ "$got" == "$expected" ]]; then
        pass "$label = $got"
    else
        fail "$label expected '$expected', got '$got'"
    fi
}

check "ReadonlyRootfs" "true"  "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$WORKER")"
check "CapDrop"        "[ALL]" "$(docker inspect --format '{{.HostConfig.CapDrop}}' "$WORKER")"
check "User"           "1000:1000" "$(docker inspect --format '{{.Config.User}}' "$WORKER")"
check "NetworkMode"    "pockethunter_internal" "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$WORKER")"
check "Privileged"     "false" "$(docker inspect --format '{{.HostConfig.Privileged}}' "$WORKER")"
check "Init"           "true"  "$(docker inspect --format '{{.HostConfig.Init}}' "$WORKER")"

# SecurityOpt is a list; just grep for the required entry.
secopt=$(docker inspect --format '{{.HostConfig.SecurityOpt}}' "$WORKER")
if [[ "$secopt" == *"no-new-privileges:true"* ]]; then
    pass "SecurityOpt contains no-new-privileges:true  (full: $secopt)"
else
    fail "SecurityOpt missing no-new-privileges:true  (got: $secopt)"
fi

# tmpfs mounts: docker inspect surfaces them under HostConfig.Tmpfs.
tmpfs=$(docker inspect --format '{{.HostConfig.Tmpfs}}' "$WORKER")
if [[ "$tmpfs" == *"/tmp:"*"exec"* ]]; then
    pass "/tmp tmpfs is exec  (full: $tmpfs)"
else
    fail "/tmp tmpfs missing or not exec  (got: $tmpfs)"
fi

echo
yellow "== Runtime egress test =="

# The crucial production check: the worker MUST NOT be able to reach
# the public internet. We try a 2-second TCP connect to 1.1.1.1:443
# and assert it fails (timeout OR refused OR DNS failure all count).
egress_output=$(docker exec "$WORKER" python -c '
import socket, sys
try:
    socket.create_connection(("1.1.1.1", 443), timeout=2)
    print("REACHABLE")
    sys.exit(0)
except Exception as e:
    print(f"BLOCKED: {type(e).__name__}: {e}")
    sys.exit(1)
' 2>&1 || true)

if [[ "$egress_output" == BLOCKED:* ]]; then
    pass "Public egress blocked  ($egress_output)"
else
    fail "Public egress NOT blocked — pockethunter_internal isn't isolating the worker"
    fail "  output: $egress_output"
fi

# ── summary ─────────────────────────────────────────────────────────────
echo
if [[ $failures -eq 0 ]]; then
    green "All hardening checks passed."
    exit 0
else
    red "$failures hardening check(s) FAILED — do not point DNS at this box."
    exit 1
fi
