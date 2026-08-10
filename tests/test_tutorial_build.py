"""Tests for scripts/build_tutorial.py.

A broken in-page anchor fails silently in a browser — the page simply
does not move — so anchor integrity is the property most worth pinning
down here.

The other silent failure is staleness: the rendered page is committed
alongside its markdown so the render is reviewable in a diff, which
means the two can disagree. ``TestShippedPage`` re-renders from the
committed sources and demands the bytes match.
"""
from __future__ import annotations

import importlib.util
import itertools
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

    def test_committed_page_is_a_current_render_of_its_sources(self):
        """Both the source and the artefact are committed, so they can
        drift: edit ``tutorial.md``, forget to run the build script, and
        nothing else in this file notices — the anchors still resolve
        and a stale page ships. Re-render from the committed sources and
        demand byte-equality.

        ``--built-at`` is the one input that is not in the repository, so
        it is scraped back out of the footer of the page under test
        rather than taken from today's date. That keeps the test stable
        on any day and still fails the moment the markdown, the template
        or the builder moves without a rebuild.
        """
        built_path = REPO / "static" / "tutorial" / "index.html"
        if not built_path.exists():
            import pytest
            pytest.skip("page not built yet")

        shipped = built_path.read_text(encoding="utf-8")
        stamp = re.search(r"<footer>Built\s+(.+?)\s+·", shipped)
        assert stamp, (
            "no 'Built <date> ·' stamp in the page footer — this test "
            "reads the build date back out of the artefact, so the "
            "footer format in docs/tutorial/template.html and this "
            "pattern have to stay in step"
        )

        rebuilt = _load_builder().render(
            (REPO / "docs" / "tutorial" / "tutorial.md").read_text(encoding="utf-8"),
            (REPO / "docs" / "tutorial" / "template.html").read_text(encoding="utf-8"),
            built_at=stamp.group(1),
        )

        if rebuilt != shipped:
            import difflib
            diff = "\n".join(
                itertools.islice(
                    difflib.unified_diff(
                        shipped.splitlines(), rebuilt.splitlines(),
                        fromfile="static/tutorial/index.html (committed)",
                        tofile="rebuilt from docs/tutorial/",
                        lineterm="",
                    ),
                    40,
                )
            )
            raise AssertionError(
                "static/tutorial/index.html is stale — it is not what the "
                "committed docs/tutorial/ sources render to. Re-run "
                "`python scripts/build_tutorial.py --built-at "
                f"{stamp.group(1)}` and commit the result.\n\n{diff}"
            )


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

    def test_scope_label_keeps_bold_weight(self):
        """The labels used to be real <h3> elements, which picked up
        font-weight: 700 from `.content h3` by cascade even though the old
        `.scope h3` rule never set it directly. Now that the labels are
        `<p class="scope-label">`, `.content h3` no longer selects them, so
        the weight must be set explicitly on `.scope .scope-label` or the
        labels silently render at the browser's regular <p> default (400).
        """
        html = self._built()
        css = html[html.index("<style>"):html.index("</style>")]
        m = re.search(r'\.scope \.scope-label\s*\{([^}]*)\}', css)
        assert m, "expected a .scope .scope-label rule in the stylesheet"
        assert re.search(r'font-weight:\s*700', m.group(1)), (
            ".scope .scope-label must set font-weight: 700 explicitly"
        )


class TestMarkdownEscaping:
    """A markdown metacharacter inside prose corrupts the rendered page
    silently: the build succeeds, every anchor still resolves, and only a
    human reading the live HTML notices. ``Mol*`` is the standing example
    — the trailing ``*`` opens an emphasis span, so the product name
    renders as "Mol", an arbitrary run of following text is italicised
    instead of the words that were marked up, and a stray ``*`` is left
    behind wherever the span happens to close.
    """

    def _content(self):
        built = REPO / "static" / "tutorial" / "index.html"
        if not built.exists():
            import pytest
            pytest.skip("page not built yet")
        html = built.read_text(encoding="utf-8")
        # Only the rendered markdown, not the template's <style> block
        # (which legitimately contains a `*` universal selector).
        start = html.index('<main')
        return html[start:html.index("</main>", start)]

    def test_molstar_product_name_survives_rendering(self):
        content = self._content()
        assert "Mol*" in content, (
            "the Mol* product name is missing from the built page — an "
            "unescaped `Mol*` in the markdown eats its own asterisk"
        )
        assert "Mol<em>" not in content, (
            "`Mol*` opened an emphasis span; escape it as `Mol\\*` or "
            "wrap it in backticks"
        )

    def test_no_stray_asterisks_in_rendered_prose(self):
        """An unbalanced emphasis marker leaves a literal `*` in the text.

        Escaped asterisks reach the HTML as a bare `*` too, so this
        allows the ones we mean (currently only `Mol*`) and fails on any
        other — which is exactly the residue an eaten emphasis span
        leaves behind.
        """
        content = self._content()
        # Code spans may contain anything; markdown does not parse them.
        prose = re.sub(r"<code>.*?</code>", "", content, flags=re.S)
        prose = re.sub(r"<pre>.*?</pre>", "", prose, flags=re.S)
        leftover = prose.replace("Mol*", "")
        assert "*" not in leftover, (
            "stray '*' in rendered prose — an emphasis span is unbalanced "
            "or a metacharacter needs escaping"
        )
