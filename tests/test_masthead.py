"""Tests for landing.render_masthead — the unified brand + nav strip.

render_masthead is split between (a) a top-row HTML markdown, (b) a
nav row that uses st.columns containing 3 st.buttons + 2 st.markdowns
(chips + costbio), and (c) the surrounding st.container. We capture
all relevant calls so tests can assert on the concatenated HTML AND
on which buttons / widget keys were created.

Shape (v3, post nav-buttonisation):

  Top row : [logo] POCKETHUNTER/SUITE  [session-inline OR intro]  [v.2.0]
  Rule line
  Nav row : ⌂ NEW SESSION | TUTORIAL | HELP | <chips> | costbio link
"""
from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import patch


def _render_to_capture(resolved):
    """Patch every st.* call render_masthead makes and run it. Returns a
    dict with concatenated 'html' (all markdowns joined) and 'buttons'
    (list of {label, key, kwargs}). Suppresses the side-effects of
    st.container / st.columns / st.button so the function body runs
    end-to-end in bare mode."""
    captured = {"markdown": [], "buttons": []}

    def fake_markdown(html, *args, **kwargs):
        captured["markdown"].append(html)

    def fake_button(label, *args, key=None, **kwargs):
        captured["buttons"].append({"label": label, "key": key,
                                    "kwargs": dict(kwargs)})
        return False  # not clicked

    @contextlib.contextmanager
    def fake_container(**kwargs):
        yield

    @contextlib.contextmanager
    def fake_col():
        yield

    def fake_columns(spec, **kwargs):
        n = spec if isinstance(spec, int) else len(spec)
        return [fake_col() for _ in range(n)]

    from landing import render_masthead

    with patch.multiple(
        "landing.st",
        markdown=fake_markdown,
        button=fake_button,
        container=fake_container,
        columns=fake_columns,
    ):
        render_masthead(resolved=resolved)

    captured["html"] = "".join(captured["markdown"])
    return captured


def _capture_markdown():
    """Backward-compat shim — older tests in this file call
    ``with patch("landing.st.markdown", side_effect=fake): …``. Returns
    a dict + fake callable; only the LAST markdown wins, but the helper
    is still useful for tests that only need the top-row content."""
    captured = {"html": None}

    def _fake_markdown(html, *args, **kwargs):
        captured["html"] = html

    return captured, _fake_markdown


def _make_resolved(*, is_editor=True, display_name=None, short="SHORT_CODE_42",
                   edit_secret="EDIT_SECRET_99"):
    """Build a minimal ResolvedSession-shaped stub."""
    sess = SimpleNamespace(
        short_code=short,
        edit_secret=edit_secret,
        display_name=display_name,
    )
    return SimpleNamespace(session=sess, is_editor=is_editor, short_code=short)


# ── No-session branch (landing / not-found / expired) ───────────────────


class TestRenderMastheadNoSession:
    def test_no_session_omits_session_inline(self):
        cap = _render_to_capture(resolved=None)
        html = cap["html"]
        # Brand always present.
        assert "POCKETHUNTER/SUITE" in html
        assert 'class="bh-logo"' in html
        assert "<title>PocketHunter</title>" in html
        # No session inline content on landing — intro sentence renders
        # in its place.
        assert "bh-session-inline" not in html
        assert "bh-url" not in html
        # Stage indicators stay dropped (masthead-v2).
        assert "POCKETS" not in html
        assert "CLUSTER" not in html
        assert "●" not in html
        assert "○" not in html
        # costbio link still renders on landing (its own st.markdown).
        assert "bh-group-link" in html
        # NEW SESSION + TUTORIAL + HELP buttons render on EVERY page
        # (v3 — previously NEW SESSION was suppressed on landing).
        keys = [b["key"] for b in cap["buttons"]]
        assert "nav_new_session" in keys
        assert "nav_tutorial" in keys
        assert "nav_help" in keys

    def test_intro_sentence_present_when_no_session(self):
        """Brand row's session-info slot is reused for a one-sentence
        product description when no session is loaded."""
        cap = _render_to_capture(resolved=None)
        html = cap["html"]
        assert "bh-intro" in html
        # Substring from the configured copy — change here if the
        # tagline changes (also in landing.render_masthead).
        assert "Pocket detection" in html
        assert "molecular-dynamics" in html

    def test_intro_sentence_absent_when_session_loaded(self):
        cap = _render_to_capture(resolved=_make_resolved())
        html = cap["html"]
        assert "bh-intro" not in html
        assert "bh-session-inline" in html


# ── Editor branch ───────────────────────────────────────────────────────


class TestRenderMastheadEditor:
    def test_editor_includes_full_url_with_edit_token_in_top_row(self):
        resolved = _make_resolved(is_editor=True, display_name="my run",
                                  short="ABC123", edit_secret="EDIT_TOK")
        cap = _render_to_capture(resolved=resolved)
        html = cap["html"]
        # Session info hoisted INLINE into the top row.
        assert "bh-session-inline" in html
        assert "bh-session-row" not in html  # old class is gone
        assert "📁" in html
        assert "<strong>my run</strong>" in html
        # URL with edit token visible + copy-button JS payload.
        assert "ABC123" in html
        assert "EDIT_TOK" in html
        assert "edit=" in html
        assert "✏️" in html
        assert ">Editor" in html or "Editor<" in html
        assert "bh-copy" in html
        assert "navigator.clipboard.writeText" in html
        # Session inline must appear BEFORE the right cluster (version
        # pill) in DOM order — nested in the top row, not below.
        inline_pos = html.index("bh-session-inline")
        right_pos = html.index("bh-right")
        assert inline_pos < right_pos

    def test_editor_falls_back_to_short_code_when_no_display_name(self):
        resolved = _make_resolved(is_editor=True, display_name=None,
                                  short="XYZ999")
        cap = _render_to_capture(resolved=resolved)
        assert "session XYZ999" in cap["html"]


# ── Viewer branch ───────────────────────────────────────────────────────


class TestRenderMastheadViewer:
    def test_viewer_excludes_edit_token(self):
        resolved = _make_resolved(is_editor=False, display_name="vis",
                                  short="VIEWER_S", edit_secret="SHOULD_NOT_LEAK")
        cap = _render_to_capture(resolved=resolved)
        html = cap["html"]
        assert "VIEWER_S" in html
        assert "SHOULD_NOT_LEAK" not in html
        assert "edit=" not in html
        assert "👁" in html
        assert ">Viewer" in html or "Viewer<" in html


# ── Bottom nav row ──────────────────────────────────────────────────────


class TestRenderMastheadNav:
    def test_nav_row_with_session_has_all_three_buttons_and_costbio(self):
        """Nav row holds 3 Streamlit buttons (NEW SESSION / TUTORIAL /
        HELP) plus pool-load chips plus the costbio link, regardless of
        whether a session is loaded."""
        cap = _render_to_capture(resolved=_make_resolved())
        keys = [b["key"] for b in cap["buttons"]]
        assert "nav_new_session" in keys
        assert "nav_tutorial" in keys
        assert "nav_help" in keys
        # Costbio link still rendered (its own st.markdown).
        assert "bh-group-link" in cap["html"]

    def test_nav_row_on_landing_has_all_three_buttons_too(self):
        """v3: NEW SESSION / TUTORIAL / HELP all visible on landing
        too — the masthead nav serves as persistent app navigation."""
        cap = _render_to_capture(resolved=None)
        keys = [b["key"] for b in cap["buttons"]]
        assert "nav_new_session" in keys
        assert "nav_tutorial" in keys
        assert "nav_help" in keys
        assert "bh-group-link" in cap["html"]

    def test_new_session_button_has_uppercase_label(self):
        """The button label sets visitor expectation — UPPERCASE matches
        the brutalist nav-row visual register."""
        cap = _render_to_capture(resolved=_make_resolved())
        new_btn = next(b for b in cap["buttons"] if b["key"] == "nav_new_session")
        assert "NEW SESSION" in new_btn["label"]
        assert "⌂" in new_btn["label"]


# ── Costbio affiliation link ────────────────────────────────────────────


class TestCostbioLink:
    def _render(self, resolved):
        return _render_to_capture(resolved=resolved)["html"]

    def test_link_present_on_landing(self):
        html = self._render(resolved=None)
        assert "costbio@gtubeng" in html
        assert "https://costbio.github.io" in html

    def test_link_present_on_session_page(self):
        html = self._render(resolved=_make_resolved())
        assert "costbio@gtubeng" in html
        assert "https://costbio.github.io" in html

    def test_link_opens_in_new_tab(self):
        html = self._render(resolved=None)
        # Find the costbio anchor and confirm target="_blank" + rel hygiene.
        import re
        tag = re.search(r'<a class="bh-group-link"[^>]*>', html)
        assert tag is not None, "bh-group-link anchor not found"
        attrs = tag.group(0)
        assert 'target="_blank"' in attrs
        assert 'rel="noopener noreferrer"' in attrs


# ── XSS hardening ───────────────────────────────────────────────────────


class TestRenderMastheadXSS:
    def test_display_name_with_script_is_escaped(self):
        """An adversarial display_name must not produce literal <script>."""
        evil = "<script>alert(1)</script>"
        resolved = _make_resolved(display_name=evil)
        cap = _render_to_capture(resolved=resolved)
        html = cap["html"]
        assert "&lt;script&gt;" in html
        assert "<script>alert(1)</script>" not in html


# ── Logo presence + size ────────────────────────────────────────────────


class TestLogoMarkup:
    def test_logo_svg_always_present_at_64_square(self):
        html = _render_to_capture(resolved=None)["html"]
        assert 'class="bh-logo"' in html
        assert 'viewBox="0 0 64 64"' in html
        assert 'width="64"' in html
        assert 'height="64"' in html
        assert 'class="surface"' in html
        assert 'class="magnifier"' in html
        # tint disc dropped in masthead-v3 (would distort coloured cells).
        assert 'class="tint"' not in html
        assert 'role="img"' in html
        assert "aria-label=" in html

    def test_brand_mark_is_home_link(self):
        """Logo + wordmark are wrapped in <a href="/"> so a click on
        either takes the user to the landing page — standard 'logo
        goes home' affordance."""
        import re
        html = _render_to_capture(resolved=None)["html"]
        # The bh-mark element must be an anchor pointing at root.
        tag = re.search(r'<a class="bh-mark"[^>]*>', html)
        assert tag is not None, "bh-mark must be an <a> tag"
        assert 'href="/"' in tag.group(0)
        assert 'target="_self"' in tag.group(0)
        # The SVG + wordmark sit INSIDE the anchor (clickable region
        # covers both).
        anchor_end_pos = html.index("</a>")
        svg_start_pos = html.index('class="bh-logo"')
        wordmark_pos = html.index("POCKETHUNTER/SUITE")
        assert svg_start_pos < anchor_end_pos
        assert wordmark_pos < anchor_end_pos

    def test_magnifier_anchored_at_origin(self):
        """Magnifier is anchored at SVG (0,0); the raster animation
        drives its actual position. Anchoring elsewhere would offset
        every keyframe coordinate — we test the contract."""
        html = _render_to_capture(resolved=None)["html"]
        # The lens-frame circle in <g class="magnifier"> has cx=cy=0.
        assert 'cx="0" cy="0"' in html

    def test_surface_uses_brand_palette(self):
        """Masthead-v5: pastel patchwork — pale yellow body with three
        pastel patches (pink / mint / lavender) over white rounded corners.
        No central pocket. Regression guards: old electrostatic blue/red
        and the v4 black-pocket / acid-yellow rim must not reappear."""
        html = _render_to_capture(resolved=None)["html"]
        # Off-white (corner rounding) — matches .bh-url / .bh-newsession bg
        assert "#f0f0f0" in html
        # Pale yellow (protein body) — matches primary-button bg
        assert "#f5ffa3" in html
        # Pastel patches
        assert "#ffd6e0" in html  # pink
        assert "#c8e8d0" in html  # mint
        assert "#d8d0f0" in html  # lavender
        # Regression guards: previous palettes gone.
        assert "#3050b8" not in html  # v3 electrostatic blue
        assert "#c83838" not in html  # v3 electrostatic red
        assert "#1a1a1a" not in html.split('class="magnifier"')[0], (
            "Pre-magnifier section must not contain #1a1a1a — the v4 "
            "central black pocket was removed in v5."
        )
        assert "#d4ff00" not in html  # v4 acid-yellow rim highlight

    def test_surface_fills_scan_region_with_110_cells(self):
        """The 11×10 cell grid covers the magnifier's raster scan path
        with no gaps. Tests the count + that every expected (x, y) is
        present so future edits don't accidentally orphan a cell."""
        import re

        html = _render_to_capture(resolved=None)["html"]
        coords = re.findall(r'<rect x="(\d+)" y="(\d+)"', html)
        cells = {(int(x), int(y)) for x, y in coords}
        assert len(coords) == 110, f"expected 110 rects, got {len(coords)}"
        assert len(cells) == 110, "duplicate cell coordinates found"
        expected = {(x, y) for x in range(12, 56, 4) for y in range(12, 52, 4)}
        assert cells == expected, f"missing: {expected - cells}, extra: {cells - expected}"


# ── Pool-load chip tooltip ───────────────────────────────────────────────


class TestPoolLoadChipTooltips:
    def test_chip_labels_carry_data_tooltip_attribute(self):
        """The FAST / DOCK labels in the pool-load chips get a
        data-tooltip attribute that the CSS uses to render a brutalist
        hover-popover. Catches drift where a future change drops the
        attribute and silently breaks the tooltip."""
        from landing import _build_pool_load_chips_html

        html = _build_pool_load_chips_html()
        # Both labels present + each carries a data-tooltip attribute.
        assert html.count('class="bh-load-label"') == 2
        assert html.count('data-tooltip="') == 2
        # The tooltip copy itself, per pool.
        assert "Find Pockets and Cluster" in html
        assert "Smina docking" in html
