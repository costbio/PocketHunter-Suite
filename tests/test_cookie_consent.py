"""Consent must actually gate the cookie, not merely be displayed.

NAR requires a consent form whenever permanent cookies are used. A banner
that appears while the cookie is written anyway satisfies nobody, so the
tests that matter here are the ones asserting record_session stays silent
until an explicit yes.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def clean_state():
    """Streamlit session_state is a module global; reset between tests."""
    import streamlit as st

    for k in ("_recent_cm", "_consent_decision", "_recent_cm_ready"):
        st.session_state.pop(k, None)
    yield
    for k in ("_recent_cm", "_consent_decision", "_recent_cm_ready"):
        st.session_state.pop(k, None)


def _mount(cookie_value=None):
    """Pretend recent_sessions.init() ran, with the given consent cookie."""
    import streamlit as st

    cm = MagicMock()
    cm.get.side_effect = lambda name, **kw: (
        cookie_value if name == "ph_cookie_consent" else None
    )
    st.session_state["_recent_cm"] = cm
    return cm


class TestDecision:
    def test_none_before_any_answer(self):
        from cookie_consent import decision

        _mount(cookie_value=None)
        assert decision() is None

    def test_reads_a_previous_answer_from_the_cookie(self):
        from cookie_consent import DENIED, GRANTED, decision

        _mount(cookie_value=GRANTED)
        assert decision() == GRANTED
        _mount(cookie_value=DENIED)
        assert decision() == DENIED

    def test_ignores_a_garbage_cookie_value(self):
        from cookie_consent import decision

        _mount(cookie_value="yes-please")
        assert decision() is None

    def test_none_when_the_cookie_component_never_mounted(self):
        from cookie_consent import decision

        assert decision() is None

    def test_absence_of_an_answer_is_not_consent(self):
        from cookie_consent import has_consent

        _mount(cookie_value=None)
        assert has_consent() is False

    def test_decline_is_not_consent(self):
        from cookie_consent import DENIED, has_consent

        _mount(cookie_value=DENIED)
        assert has_consent() is False

    def test_answer_given_this_run_wins_over_the_stale_cookie(self):
        """The cookie needs a rerun to become visible; the latch bridges it."""
        import streamlit as st
        from cookie_consent import GRANTED, has_consent

        _mount(cookie_value=None)
        st.session_state["_consent_decision"] = GRANTED
        assert has_consent() is True


class TestRecordSessionIsGated:
    def _record(self):
        import recent_sessions

        recent_sessions.record_session("abc123", display_name="x", edit_secret="s")

    def test_writes_nothing_without_consent(self):
        import streamlit as st

        cm = _mount(cookie_value=None)
        st.session_state["_recent_cm_ready"] = True
        self._record()
        cm.set.assert_not_called()

    def test_writes_nothing_after_decline(self):
        import streamlit as st
        from cookie_consent import DENIED

        cm = _mount(cookie_value=DENIED)
        st.session_state["_recent_cm_ready"] = True
        self._record()
        cm.set.assert_not_called()

    def test_writes_once_consent_is_granted(self):
        import streamlit as st
        from cookie_consent import GRANTED

        cm = _mount(cookie_value=GRANTED)
        st.session_state["_recent_cm_ready"] = True
        self._record()
        cm.set.assert_called_once()
        assert cm.set.call_args.args[0] == "ph_recent_sessions"


class TestBanner:
    def test_hidden_once_answered(self):
        from cookie_consent import GRANTED, render_banner

        _mount(cookie_value=GRANTED)
        with patch("streamlit.container") as container:
            render_banner()
        container.assert_not_called()

    def test_shown_before_any_answer(self):
        import contextlib

        from cookie_consent import render_banner

        @contextlib.contextmanager
        def _noop(*a, **k):
            yield

        _mount(cookie_value=None)
        captured = []
        with patch("streamlit.container", _noop), \
             patch("streamlit.columns", return_value=(_noop(), _noop(), _noop())), \
             patch("streamlit.markdown", lambda h, *a, **k: captured.append(h)), \
             patch("streamlit.button", return_value=False):
            render_banner()

        html = "\n".join(captured)
        assert "ph_recent_sessions" in html, "must name the cookie it asks about"
        assert "third-party" in html.lower(), "must state no third-party cookies"
        assert "decline" in html.lower(), "must say declining is workable"
