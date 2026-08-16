"""The published sample data must be the data the demo actually runs.

NAR requires sample data to be accessible so users can confirm the format
their own uploads need. That guarantee only holds if the downloadable
copy under static/ is the same bytes as examples/tem1/, which is what the
"Try with example trajectory" button feeds to the pipeline. A drifted
copy would document a format the service does not accept — worse than
publishing nothing, because it looks authoritative.
"""
from __future__ import annotations

import hashlib
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
CANONICAL = REPO / "examples" / "tem1"
PUBLISHED = REPO / "static" / "example"

FILES = ("topology.pdb", "trajectory.xtc")


def _digest(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


class TestPublishedCopyMatches:
    def test_both_files_are_published(self):
        for name in FILES:
            assert (PUBLISHED / name).exists(), f"not published: {name}"

    def test_published_bytes_are_identical_to_the_canonical_example(self):
        for name in FILES:
            assert _digest(PUBLISHED / name) == _digest(CANONICAL / name), (
                f"static/example/{name} has drifted from examples/tem1/{name}; "
                "re-copy it, or the sample download documents a format the "
                "demo does not run"
            )


class TestLandingLinksThem:
    def test_urls_point_at_the_published_copies(self):
        from landing import EXAMPLE_TOPOLOGY_URL, EXAMPLE_TRAJECTORY_URL

        assert EXAMPLE_TOPOLOGY_URL.endswith("/static/example/topology.pdb")
        assert EXAMPLE_TRAJECTORY_URL.endswith("/static/example/trajectory.xtc")

    def test_landing_offers_both_downloads_when_the_example_exists(self):
        """The links are inside the example branch, so they follow its gating."""
        import contextlib
        from unittest.mock import patch

        captured = []

        @contextlib.contextmanager
        def _noop():
            yield

        with patch("landing.render_masthead_fragment"), \
             patch("landing._render_recent_sessions"), \
             patch("landing.render_landing_footer"), \
             patch("landing._example_data_available", return_value=True), \
             patch("streamlit.columns", return_value=(_noop(), _noop())), \
             patch("streamlit.markdown", lambda h, *a, **k: captured.append(h)), \
             patch("streamlit.caption"), \
             patch("streamlit.button", return_value=False), \
             patch("streamlit.text_input", return_value=""):
            from landing import EXAMPLE_TOPOLOGY_URL, EXAMPLE_TRAJECTORY_URL, render_landing

            render_landing()

        html = "\n".join(captured)
        assert EXAMPLE_TOPOLOGY_URL in html
        assert EXAMPLE_TRAJECTORY_URL in html

    def test_no_sample_links_when_the_example_is_absent(self):
        """Operators opt out by unsetting EXAMPLE_TRAJECTORY_DIR; links must follow."""
        import contextlib
        from unittest.mock import patch

        captured = []

        @contextlib.contextmanager
        def _noop():
            yield

        with patch("landing.render_masthead_fragment"), \
             patch("landing._render_recent_sessions"), \
             patch("landing.render_landing_footer"), \
             patch("landing._example_data_available", return_value=False), \
             patch("streamlit.columns", return_value=(_noop(), _noop())), \
             patch("streamlit.markdown", lambda h, *a, **k: captured.append(h)), \
             patch("streamlit.caption"), \
             patch("streamlit.button", return_value=False), \
             patch("streamlit.text_input", return_value=""):
            from landing import EXAMPLE_TOPOLOGY_URL, render_landing

            render_landing()

        assert EXAMPLE_TOPOLOGY_URL not in "\n".join(captured)
