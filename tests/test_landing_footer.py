"""Tests for landing.render_landing_footer — the licence / provenance strip.

The NAR Web Server Issue requires a standard licence to be *shown on the
landing page*, not merely shipped in the source tree. That makes the
footer a compliance surface rather than decoration, so it gets tests: a
refactor that drops the licence line would otherwise pass silently and
only surface in review.
"""
from __future__ import annotations

from unittest.mock import patch


def _render_footer():
    """Run render_landing_footer in bare mode, returning the HTML it emitted."""
    captured = []

    def fake_markdown(html, *args, **kwargs):
        captured.append(html)

    with patch("streamlit.markdown", fake_markdown), \
         patch("streamlit.divider", lambda *a, **k: None):
        from landing import render_landing_footer

        render_landing_footer()
    return "\n".join(captured)


class TestLicenceIsVisible:
    def test_names_the_licence(self):
        assert "MIT licence" in _render_footer()

    def test_links_to_the_licence_file(self):
        from landing import LICENSE_URL

        html = _render_footer()
        assert LICENSE_URL in html
        assert LICENSE_URL.endswith("/LICENSE")

    def test_states_the_terms_rather_than_only_naming_them(self):
        """"MIT" alone is jargon; a reader should learn what it permits."""
        html = _render_footer().lower()
        assert "free" in html
        assert "open-source" in html


class TestFooterLinks:
    def test_links_to_both_repositories(self):
        from landing import CORE_REPO_URL, REPO_URL

        html = _render_footer()
        assert REPO_URL in html
        assert CORE_REPO_URL in html

    def test_links_to_tutorial_and_help(self):
        from landing import HELP_URL, TUTORIAL_URL

        html = _render_footer()
        assert TUTORIAL_URL in html
        assert HELP_URL in html

    def test_external_links_are_rel_noopener(self):
        """Every target=_blank needs rel=noopener — reverse-tabnabbing."""
        import re

        html = _render_footer()
        for tag in re.findall(r"<a\b[^>]*>", html):
            if 'target="_blank"' in tag:
                assert "noopener" in tag, f"missing rel=noopener: {tag}"


class TestLandingRendersTheFooter:
    def test_render_landing_calls_the_footer(self):
        """The footer existing is not enough; the landing page must show it."""
        with patch("landing.render_landing_footer") as footer, \
             patch("landing.render_masthead_fragment"), \
             patch("landing._render_recent_sessions"), \
             patch("landing._example_data_available", return_value=False), \
             patch("streamlit.columns") as cols, \
             patch("streamlit.markdown"), patch("streamlit.caption"), \
             patch("streamlit.button", return_value=False), \
             patch("streamlit.text_input", return_value=""):
            import contextlib

            @contextlib.contextmanager
            def _noop():
                yield

            cols.return_value = (_noop(), _noop())
            from landing import render_landing

            render_landing()

        footer.assert_called_once()
