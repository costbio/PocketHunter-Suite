"""Best-effort client-IP resolver for rate-limiting decisions.

Streamlit doesn't expose the inbound request object directly — instead it
proxies a subset of the inbound headers via ``st.context.headers``. The
public deployment will sit behind a TLS reverse proxy (Caddy / nginx)
that injects ``X-Forwarded-For`` and ``X-Real-IP``; we honour those.

When the headers are missing (e.g. unit-testing context, or Streamlit
running directly without a proxy) we fall back to ``"unknown"`` — the
rate limiter buckets every such caller into the same daily counter,
which is the safe default for a public deployment.
"""
from __future__ import annotations

from typing import Optional


def client_ip() -> str:
    """Return the perceived client IP, or ``"unknown"`` if not derivable."""
    try:
        import streamlit as st
    except ImportError:  # pragma: no cover — Streamlit always present in app
        return "unknown"

    headers = _safe_headers(st)
    if headers is None:
        return "unknown"

    xff = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
    if xff:
        # XFF is "client, proxy1, proxy2…" — first hop is the real client.
        first = xff.split(",", 1)[0].strip()
        if first:
            return first

    real = headers.get("X-Real-IP") or headers.get("x-real-ip")
    if real:
        return real.strip()

    return "unknown"


def _safe_headers(st) -> Optional[dict]:
    """``st.context.headers`` is read-only and may be unavailable mid-test."""
    try:
        return dict(st.context.headers or {})
    except Exception:
        return None
