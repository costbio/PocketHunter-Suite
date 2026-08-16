"""Consent gate for the one persistent cookie this service sets.

The NAR Web Server Issue requires a cookie consent form whenever
permanent cookies are used, and forbids third-party and tracking cookies
being set by default. This service sets no third-party or tracking
cookies at all. It sets exactly one persistent first-party cookie,
``ph_recent_sessions`` (``recent_sessions.py``), which holds a list of
the sessions this browser has visited, lives 365 days, and never reaches
the server.

Convenience, in other words — which is precisely the category that needs
asking first. Until a visitor accepts, ``recent_sessions.record_session``
writes nothing and the service works exactly as before, minus the
"recently visited" list.

**On storing the answer.** The decision itself goes in
``ph_cookie_consent``. Writing that one without prior consent is not a
contradiction: a cookie whose only purpose is to remember the user's own
choice is strictly necessary to honour that choice, and is exempt under
every regulator's guidance on the subject. The alternative — re-asking on
every page load — would be worse for the user and would still require a
cookie to avoid.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

CONSENT_COOKIE = "ph_cookie_consent"
GRANTED = "granted"
DENIED = "denied"

# Same horizon as the cookie it authorises, so the two expire together
# rather than leaving a consent record for a cookie that is already gone.
_CONSENT_DAYS = 365


def _cm():
    """The CookieManager mounted by ``recent_sessions.init()``.

    Deliberately reuses that single component instance. Mounting a second
    CookieManager renders a second (invisible) Streamlit component and the
    two race on ``document.cookie``.
    """
    return st.session_state.get("_recent_cm")


def decision() -> str | None:
    """``GRANTED``, ``DENIED``, or ``None`` when the visitor has not answered.

    ``None`` also covers "the cookie component has not delivered yet",
    which is the safe reading: callers treat anything other than
    ``GRANTED`` as "do not write".
    """
    # An answer given this run is authoritative — the cookie round-trip
    # needs a rerun to become visible, and until then the banner would
    # otherwise reappear directly after being dismissed.
    pending = st.session_state.get("_consent_decision")
    if pending in (GRANTED, DENIED):
        return pending
    cm = _cm()
    if cm is None:
        return None
    try:
        raw = cm.get(CONSENT_COOKIE)
    except Exception:
        return None
    return raw if raw in (GRANTED, DENIED) else None


def has_consent() -> bool:
    """True only on an explicit yes. Absence of an answer is not consent."""
    return decision() == GRANTED


def _record(answer: str) -> None:
    st.session_state["_consent_decision"] = answer
    cm = _cm()
    if cm is None:
        return
    try:
        cm.set(
            CONSENT_COOKIE,
            answer,
            key="cookie_consent_set",
            expires_at=datetime.now(timezone.utc) + timedelta(days=_CONSENT_DAYS),
        )
    except Exception:
        # Cookies blocked entirely — the session-state latch still keeps
        # the banner down for this visit, and nothing persistent is
        # written, which is the outcome a blocking visitor wanted anyway.
        pass


def render_banner() -> None:
    """Show the consent form, once, until the visitor answers.

    Renders nothing after a decision either way. Safe to call on every
    page; call it before anything that might write a persistent cookie.
    """
    if decision() is not None:
        return

    with st.container(border=True):
        st.markdown(
            '<div class="bh-consent">'
            '<strong>One optional cookie.</strong> '
            'PocketHunter Suite can remember the analyses you have opened in '
            'this browser, so the landing page can list them for you. That '
            'needs one first-party cookie, <code>ph_recent_sessions</code>, '
            'kept for a year and never sent to our server. '
            'We set no tracking cookies and no third-party cookies. '
            'Decline and everything still works — you will just need to keep '
            'your own session links.'
            '</div>',
            unsafe_allow_html=True,
        )
        accept_col, decline_col, _rest = st.columns([1, 1, 4])
        with accept_col:
            if st.button("Accept", key="consent_accept", use_container_width=True):
                _record(GRANTED)
                st.rerun()
        with decline_col:
            if st.button("Decline", key="consent_decline", use_container_width=True):
                _record(DENIED)
                st.rerun()
