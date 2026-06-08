# Deployment

This file used to be the v1 single-worker deployment guide. It is no longer
maintained — the architecture moved to Phase C (Postgres + orchestrator-managed
hardened worker pools + abuse limits) and this doc lagged.

The current sources of truth are:

- [`README.md`](README.md) — local development quick start.
- [`docs/deployment.md`](docs/deployment.md) — **authoritative production
  deployment guide** for the single-box / 64-core / 128 GB target (TLS reverse
  proxy, production env overrides, smoke checklist, rollback, backup, log
  rotation).
- [`docs/security.md`](docs/security.md) — hardening flag-by-flag checklist that
  the orchestrator applies to every spawned worker container.
- [`CLAUDE.md`](CLAUDE.md) — contributor reference covering architecture,
  conventions, and the worker write contract.
