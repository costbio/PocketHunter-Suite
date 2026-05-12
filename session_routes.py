"""URL routing for the v2 anonymous-session model.

A session URL is shaped like::

    /                          → landing page (no session loaded)
    /?s=<short_code>           → view session as read-only
    /?s=<short_code>&edit=<edit_secret>  → view session as editor

This module is the bridge between ``st.query_params`` and the DB:

* :func:`resolve_session_from_query` parses the query string, looks the
  session up, and returns ``(session, is_editor)`` (with ``session=None``
  on miss / expired).

* :func:`build_session_url` produces the share URL the landing-page
  "create" flow displays to the user.

* :func:`set_session_in_state` stashes the resolved session into
  ``st.session_state`` under canonical keys so downstream pages can read
  them without re-validating.

The reason this is a separate module is that ``main.py`` is hot-loaded by
every Streamlit rerun; keeping the routing logic here keeps the rerun
overhead small and the code unit-testable without Streamlit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

from db.models import Session as SessionRow
from db.sessions import is_editor as _is_editor
from db.sessions import is_expired, load_session


@dataclass(frozen=True)
class ResolvedSession:
    """Result of resolving a query string against the DB.

    Attributes:
        session: The DB row, or ``None`` if no short_code was provided or it
            did not resolve.
        is_editor: ``True`` when a valid ``edit_secret`` was supplied.
        is_expired: ``True`` when the session is in soft-expiry state.
        short_code: The short_code we tried to resolve, even if it missed.
    """

    session: Optional[SessionRow]
    is_editor: bool
    is_expired: bool
    short_code: Optional[str]


def resolve_session(
    short_code: Optional[str],
    edit_secret: Optional[str],
) -> ResolvedSession:
    """Pure resolver — no Streamlit imports. Easy to unit-test.

    Visiting ``/?s=<bad>`` returns ``ResolvedSession(session=None,
    is_editor=False, is_expired=False, short_code="<bad>")`` so the caller
    can render a "session not found" page.
    """
    if not short_code:
        return ResolvedSession(session=None, is_editor=False, is_expired=False, short_code=None)

    row = load_session(short_code)
    if row is None:
        return ResolvedSession(session=None, is_editor=False, is_expired=False, short_code=short_code)

    expired = is_expired(row)
    editor = _is_editor(row, edit_secret) and not expired
    return ResolvedSession(
        session=row,
        is_editor=editor,
        is_expired=expired,
        short_code=short_code,
    )


def resolve_session_from_query() -> ResolvedSession:
    """Streamlit-coupled wrapper. Reads ``st.query_params``."""
    import streamlit as st

    qp = st.query_params
    short = qp.get("s") or None
    secret = qp.get("edit") or None
    return resolve_session(short, secret)


def set_session_in_state(resolved: ResolvedSession) -> None:
    """Stash the resolved session under canonical session_state keys.

    Downstream pages read these instead of re-validating on every rerun:

    * ``st.session_state.current_session`` — ``SessionRow`` or ``None``
    * ``st.session_state.is_editor`` — bool
    * ``st.session_state.is_session_expired`` — bool
    """
    import streamlit as st

    st.session_state.current_session = resolved.session
    st.session_state.is_editor = resolved.is_editor
    st.session_state.is_session_expired = resolved.is_expired


def base_url() -> str:
    """Return ``BASE_URL`` from the env. Raises if not set.

    Centralised so commit A4's pydantic ``Settings`` can swap in without
    touching every call site.
    """
    url = os.environ.get("BASE_URL")
    if not url:
        raise RuntimeError(
            "BASE_URL is not set. Configure it in .env (see .env.example) — "
            "it's required so share-URL generation has a canonical origin."
        )
    return url.rstrip("/")


def build_session_url(
    short_code: str,
    *,
    edit_secret: Optional[str] = None,
) -> str:
    """Compose the public share URL for a session.

    With ``edit_secret``: full editor URL the creator copies and shares.
    Without: read-only URL for collaborators.
    """
    url = f"{base_url()}/?s={short_code}"
    if edit_secret:
        url += f"&edit={edit_secret}"
    return url


def navigate_to_session(short_code: str, *, edit_secret: Optional[str] = None) -> None:
    """Set ``st.query_params`` to point at this session, then rerun.

    Streamlit's recommended way to "navigate" without a real router.
    """
    import streamlit as st

    st.query_params["s"] = short_code
    if edit_secret:
        st.query_params["edit"] = edit_secret
    elif "edit" in st.query_params:
        del st.query_params["edit"]
    st.rerun()


def clear_session_query() -> None:
    """Remove ``s`` and ``edit`` from the query string, then rerun.

    Used by "back to landing" affordances.
    """
    import streamlit as st

    for key in ("s", "edit"):
        if key in st.query_params:
            del st.query_params[key]
    st.rerun()
