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
