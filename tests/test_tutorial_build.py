"""Tests for scripts/build_tutorial.py.

A broken in-page anchor fails silently in a browser — the page simply
does not move — so anchor integrity is the property most worth pinning
down here.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_builder():
    path = REPO / "scripts" / "build_tutorial.py"
    spec = importlib.util.spec_from_file_location("build_tutorial", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TEMPLATE = """<html><body>
<nav>{{ TOC }}</nav>
<main>{{ CONTENT }}</main>
<footer>{{ BUILT_AT }}</footer>
</body></html>"""

MARKDOWN = """# Tutorial

## Welcome

Text.

## Before You Start

More text.
"""


class TestRender:
    def test_headings_get_ids(self):
        html = _load_builder().render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        assert 'id="welcome"' in html
        assert 'id="before-you-start"' in html

    def test_toc_is_substituted(self):
        html = _load_builder().render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        assert "{{ TOC }}" not in html
        assert "{{ CONTENT }}" not in html
        assert "{{ BUILT_AT }}" not in html
        assert 'href="#welcome"' in html

    def test_every_anchor_resolves_to_an_id(self):
        html = _load_builder().render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        ids = set(re.findall(r'id="([^"]+)"', html))
        hrefs = re.findall(r'href="#([^"]+)"', html)
        assert hrefs, "no in-page anchors found at all"
        assert [h for h in hrefs if h not in ids] == []

    def test_render_is_deterministic(self):
        builder = _load_builder()
        first = builder.render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        second = builder.render(MARKDOWN, TEMPLATE, built_at="2026-08-05")
        assert first == second


class TestShippedPage:
    """The committed page must satisfy the same invariant as a synthetic one."""

    def test_built_page_anchors_all_resolve(self):
        built = REPO / "static" / "tutorial" / "index.html"
        if not built.exists():
            import pytest
            pytest.skip("page not built yet")
        html = built.read_text(encoding="utf-8")
        ids = set(re.findall(r'id="([^"]+)"', html))
        hrefs = re.findall(r'href="#([^"]+)"', html)
        assert [h for h in hrefs if h not in ids] == []


class TestIntroBlock:
    """The schematic doubles as navigation, so its links are load-bearing."""

    def _built(self):
        built = REPO / "static" / "tutorial" / "index.html"
        if not built.exists():
            import pytest
            pytest.skip("page not built yet")
        return built.read_text(encoding="utf-8")

    def test_schematic_is_inline_svg(self):
        html = self._built()
        assert "<svg" in html
        assert 'class="schematic"' in html

    def test_every_schematic_stage_links_to_a_real_section(self):
        html = self._built()
        svg = html[html.index('class="schematic"'):]
        svg = svg[:svg.index("</svg>")]
        targets = re.findall(r'href="#([^"]+)"', svg)
        assert sorted(targets) == ["cluster", "dock", "find-pockets", "upload"]
        ids = set(re.findall(r'id="([^"]+)"', html))
        assert [t for t in targets if t not in ids] == []

    def test_scope_box_states_both_directions(self):
        """A substring check alone can't tell markdown-parsed content from
        raw un-parsed markdown text (both contain the same words), so this
        asserts on real markup: labeled <p> elements and, more importantly,
        real <ul><li> bullets — bullets only render as markup if the
        surrounding markdown="1" div was actually parsed as markdown
        (requires the `md_in_html` extension in scripts/build_tutorial.py).
        """
        html = self._built()
        scope = html[html.index('class="scope"'):]
        scope = scope[:scope.index("</div>") + len("</div>")]
        assert re.search(r'<p class="scope-label">\s*FOR YOU IF\s*</p>', scope)
        assert re.search(
            r'<p class="scope-label negative">\s*NOT FOR YOU IF\s*</p>', scope
        )
        assert re.search(r'<ul>\s*<li>', scope), (
            "bullets under the scope labels must be real <ul><li> markup, "
            "not raw '- ...' markdown text"
        )
        assert scope.count("<li>") >= 6
        assert "###" not in scope, "labels must not be raw unparsed headings"
