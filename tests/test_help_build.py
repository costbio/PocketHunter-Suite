"""Tests for the help page — the properties that make it NAR-compliant.

Two requirements do the work here. The help page must "include
information on how to interpret the results returned by the
applications", and it must carry "links to sample output that performs
interactively in the same way as real output".

The second is the one worth guarding hardest, and in two directions: the
demo link has to exist, and it must NOT carry an edit token. Publishing
the editable URL would hand every reader the ability to launch jobs in,
and mutate, the session a paper cites.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "docs" / "tutorial" / "help.md"
BUILT = REPO / "static" / "help" / "index.html"


@pytest.fixture(scope="module")
def md() -> str:
    return SRC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def prose() -> str:
    """Source with runs of whitespace collapsed.

    Assertions about phrasing use this, because the source is hard-wrapped
    and a sentence that happens to break across two lines is the same
    sentence. Assertions about markup (anchors, emphasis markers) keep
    using the raw text.
    """
    return re.sub(r"\s+", " ", SRC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def html() -> str:
    if not BUILT.exists():
        pytest.skip("help page not built in this checkout")
    return BUILT.read_text(encoding="utf-8")


class TestSampleOutputLink:
    def test_links_to_a_live_session(self, md):
        assert re.search(r"https://pockethunter\.bio-cloud\.site/\?s=[\w-]+", md), (
            "no live sample-output session linked"
        )

    def test_the_demo_link_is_view_only(self, md):
        """An edit token in a published URL hands out write access."""
        for url in re.findall(r"https://pockethunter\.bio-cloud\.site/\?[^\s)\"']+", md):
            assert "edit=" not in url, f"published URL carries an edit token: {url}"

    def test_says_the_demo_is_interactive_rather_than_a_recording(self, prose):
        lowered = prose.lower()
        assert "not a recording" in lowered or "interactiv" in lowered

    def test_links_both_sample_input_files(self, md):
        assert "/app/static/example/topology.pdb" in md
        assert "/app/static/example/trajectory.xtc" in md


class TestInterpretationContent:
    """Every stage's output needs a "what does this number mean" section."""

    @pytest.mark.parametrize("anchor", ["pockets", "clusters", "docking", "viewer"])
    def test_has_a_section_per_result_type(self, md, anchor):
        assert f"{{: #{anchor} }}" in md

    def test_explains_the_probability_column(self, prose):
        assert "probability" in prose
        assert "not a probability of anything" in prose, (
            "p2rank's score is routinely misread as a likelihood; say so"
        )

    def test_states_the_confidence_thresholds(self, prose):
        assert "0.70" in prose and "0.40" in prose

    def test_names_all_four_ranking_metrics(self, md):
        for metric in ("Mean", "Median", "Best", "ECR"):
            assert f"**{metric}**" in md

    def test_states_the_sign_convention_for_affinities(self, prose):
        assert "more negative is better" in prose

    def test_warns_against_quoting_a_score_as_an_affinity(self, prose):
        assert "not a binding affinity you can quote" in prose


class TestPolicySections:
    def test_states_the_licence(self, prose):
        assert "MIT licence" in prose

    def test_states_the_privacy_position(self, prose):
        lowered = prose.lower()
        assert "no accounts" in lowered
        assert "no tracking cookies" in lowered

    def test_names_both_cookies(self, md):
        assert "ph_recent_sessions" in md
        assert "ph_cookie_consent" in md

    def test_gives_a_contact_route(self, md):
        assert "{: #contact }" in md
        assert "issues" in md


class TestBuiltPage:
    def test_is_titled_help_not_tutorial(self, html):
        assert "<title>Help — PocketHunter Suite</title>" in html
        assert "POCKETHUNTER/SUITE — HELP" in html

    def test_placeholders_are_all_substituted(self, html):
        for token in ("{{ TOC }}", "{{ CONTENT }}", "{{ BUILT_AT }}",
                      "{{ PAGE_TITLE }}", "{{ PAGE_LABEL }}"):
            assert token not in html, f"unsubstituted placeholder: {token}"

    def test_every_escaped_asterisk_survives_as_a_literal(self, md, html):
        """Mol* must be written Mol\\* — unescaped, it opens an emphasis span.

        Counting rather than merely checking presence: one unescaped
        occurrence among several would silently italicise a clause and
        render the product name as "Mol", which is exactly the bug this
        guards and exactly the bug a presence check misses.
        """
        text = re.sub(r"<[^>]+>", "", html)
        assert r"Mol\*" not in text, "an escaped asterisk leaked into the output"
        assert md.count(r"Mol\*") == text.count("Mol*") > 0, (
            f"source has {md.count(r'Mol\\*')} escaped Mol*, "
            f"output has {text.count('Mol*')} literal ones — an unescaped "
            "asterisk was consumed as emphasis"
        )

    def test_is_a_current_render_of_its_source(self, html):
        """Catches an edited help.md that was never rebuilt."""
        from docs.tutorial.build import TEMPLATE, render

        stamp = re.search(r"Built (\d{4}-\d{2}-\d{2}) ·", html)
        assert stamp, "no build stamp found"
        expected = render(
            SRC.read_text(encoding="utf-8"),
            TEMPLATE.read_text(encoding="utf-8"),
            built_at=stamp.group(1),
            title="Help",
            label="HELP",
        )
        assert html == expected, (
            "static/help/index.html is stale — rerun docs/tutorial/build.py"
        )
