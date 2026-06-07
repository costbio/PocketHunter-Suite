"""Cloudflare Turnstile CAPTCHA integration for session creation.

Turnstile is rendered as a small Streamlit Components v2 widget on the
landing page. Most users see an invisible challenge that auto-completes
in <1 s; the few that don't pass automatically get a click-to-verify
challenge. On success the widget sends the token back to Python via
``setStateValue("token", token)`` and the landing page POSTs the token
to Cloudflare's siteverify endpoint with the secret key.

Disabled mode (``TURNSTILE_ENABLED=false`` — the dev default) returns
``True`` from ``verify`` immediately so local development doesn't need
a CF account. Production must set both keys and the flag.

Reference: https://developers.cloudflare.com/turnstile/
"""
from __future__ import annotations

from typing import Optional

import requests
import streamlit as st


TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileVerifier:
    """Server-side verifier for Turnstile tokens.

    Stateless — instantiate per request or cache the singleton; the
    siteverify endpoint is the source of truth.
    """

    def __init__(self, *, site_key: str, secret_key: str, enabled: bool = True):
        self.site_key = site_key
        self.secret_key = secret_key
        self.enabled = enabled

    def verify(self, token: Optional[str], remote_ip: Optional[str] = None) -> bool:
        """Return True iff ``token`` is a valid, unused Turnstile response.

        Fail-closed: an empty token, a network error, or a non-success
        response all return False. The single exception is
        ``self.enabled=False`` (dev mode), which always returns True.
        """
        if not self.enabled:
            return True
        if not token:
            return False
        try:
            resp = requests.post(
                TURNSTILE_VERIFY_URL,
                data={
                    "secret": self.secret_key,
                    "response": token,
                    **({"remoteip": remote_ip} if remote_ip else {}),
                },
                timeout=5,
            )
            return bool(resp.json().get("success"))
        except Exception:
            return False


def build_verifier_from_settings() -> TurnstileVerifier:
    """Construct the verifier from the project's ``settings`` singleton."""
    from settings import settings

    return TurnstileVerifier(
        site_key=settings.TURNSTILE_SITE_KEY,
        secret_key=settings.TURNSTILE_SECRET_KEY,
        enabled=settings.TURNSTILE_ENABLED,
    )


# ── Streamlit widget ────────────────────────────────────────────────────

_WIDGET_HTML = """
<div id="turnstile-root" style="display:flex;justify-content:center;padding:8px 0;"></div>
"""

_WIDGET_JS = """
async function render({data, setStateValue}) {
  const root = document.getElementById("turnstile-root");
  if (!root) return;

  // Inject the Turnstile script once. The Cloudflare loader script reads
  // ``data-sitekey`` from each ``.cf-turnstile`` div and renders the
  // widget into it.
  if (!document.querySelector('script[src*="turnstile/v0/api.js"]')) {
    const s = document.createElement('script');
    s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js";
    s.async = true;
    s.defer = true;
    document.head.appendChild(s);
  }

  // Idempotent: only insert the widget div once per mount.
  if (!root.querySelector('.cf-turnstile')) {
    const widget = document.createElement('div');
    widget.className = "cf-turnstile";
    widget.setAttribute("data-sitekey", data.site_key);
    widget.setAttribute("data-theme", "light");
    root.appendChild(widget);

    // Poll until the global ``turnstile`` object exists, then bind a
    // success callback that ships the token back through CCv2 state.
    const wait = setInterval(() => {
      if (window.turnstile) {
        clearInterval(wait);
        window.turnstile.render(widget, {
          sitekey: data.site_key,
          theme: "light",
          callback: function (token) {
            setStateValue("token", token);
          },
          "error-callback": function () {
            setStateValue("token", "");
          },
          "expired-callback": function () {
            setStateValue("token", "");
          },
        });
      }
    }, 100);
  }
}
"""

_COMPONENT = st.components.v2.component(
    "turnstile_captcha",
    html=_WIDGET_HTML,
    js=_WIDGET_JS,
)


def turnstile_widget(site_key: str, *, key: str) -> Optional[str]:
    """Render the Turnstile widget; return the validated token (or None).

    The widget posts the token back through CCv2 state once the user
    solves the challenge (or instantly on an invisible solve). The
    returned token is one-time-use — verify it server-side ASAP and
    do not store.
    """
    result = _COMPONENT(
        {"site_key": site_key},
        key=key,
        default={"token": ""},
    )
    token = getattr(result, "token", "") if result else ""
    return token or None
