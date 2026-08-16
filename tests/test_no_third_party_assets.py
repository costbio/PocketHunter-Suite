"""No page may fetch anything from a third-party host.

The NAR Web Server Issue forbids third-party and tracking cookies, and a
service whose privacy claim is "your submitted data stays private" should
not be announcing every visitor to an ad company either. Google Fonts set
no cookie, but it did receive the IP and user-agent of everyone who
opened the tutorial, before any of our own content rendered.

These tests pin that closed. They scan for external hosts rather than for
the specific string "googleapis", so pulling in some other CDN later
fails here too.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Hosts we are allowed to point a browser at. Links the user chooses to
# click are fine — what must not happen is the page fetching a subresource
# from somewhere else on its own.
_SUBRESOURCE_ATTRS = re.compile(
    r"""<(?:link|script|img|iframe)\b[^>]*?\b(?:href|src)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_EXTERNAL = re.compile(r"^(?:https?:)?//", re.IGNORECASE)


def _external_subresources(html: str) -> list[str]:
    return [u for u in _SUBRESOURCE_ATTRS.findall(html) if _EXTERNAL.match(u)]


class TestTutorialPage:
    def test_built_page_fetches_nothing_external(self):
        page = REPO / "static" / "tutorial" / "index.html"
        if not page.exists():
            pytest.skip("tutorial not built in this checkout")
        found = _external_subresources(page.read_text(encoding="utf-8"))
        assert found == [], f"external subresources on the tutorial page: {found}"

    def test_template_fetches_nothing_external(self):
        tpl = REPO / "docs" / "tutorial" / "template.html"
        found = _external_subresources(tpl.read_text(encoding="utf-8"))
        assert found == [], f"external subresources in the template: {found}"


class TestStreamlitTheme:
    def test_theme_fonts_are_self_hosted(self):
        cfg = (REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        for line in cfg.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(r"^(font|headingFont|codeFont)\s*=", stripped):
                assert "http://" not in stripped and "https://" not in stripped, (
                    f"theme font points at a remote host: {stripped}"
                )

    def test_usage_telemetry_is_off(self):
        """Streamlit posts to webhooks.fivetran.com unless told not to.

        Default is on, so its absence from config.toml is not neutral — it
        is a third-party request from every visitor's browser. Static
        scanning cannot catch this one (Streamlit's own bundle issues it),
        which is why it is asserted here against the config instead.
        """
        cfg = (REPO / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        assert re.search(r"^\s*gatherUsageStats\s*=\s*false\s*$", cfg, re.MULTILINE), (
            "gatherUsageStats is not explicitly false"
        )


class TestInlineFontFaces:
    """The app's faces are declared in main.py, not linked from config.toml.

    Streamlit silently ignored a root-relative stylesheet URL in the
    theme's `font = "Family:url"` form: nothing fetched, no face
    registered, and a fallback to Source Sans that still looked correct on
    any machine with JetBrains Mono installed locally.
    """

    def test_main_declares_every_weight_the_css_does(self):
        main = (REPO / "main.py").read_text(encoding="utf-8")
        declared = set(re.findall(r"font-weight:\s*(\d+);\s*\n?\s*font-display", main))
        assert {"400", "500", "600", "700", "800"} <= declared, (
            f"main.py declares only {sorted(declared)}"
        )

    def test_every_inline_src_resolves_to_a_shipped_file(self):
        main = (REPO / "main.py").read_text(encoding="utf-8")
        srcs = re.findall(r"url\('/app/static/fonts/([^']+)'\)", main)
        assert srcs, "no inline @font-face src found in main.py"
        for name in srcs:
            assert (REPO / "static" / "fonts" / name).exists(), f"missing: {name}"


class TestVendoredFont:
    def test_every_declared_face_file_exists(self):
        """A @font-face pointing at a missing file fails silently in the browser."""
        fonts = REPO / "static" / "fonts"
        css = (fonts / "jetbrains-mono.css").read_text(encoding="utf-8")
        urls = re.findall(r"url\(['\"]([^'\"]+)['\"]\)", css)
        assert urls, "no @font-face src found"
        for u in urls:
            assert (fonts / u).exists(), f"declared but missing: {u}"

    def test_all_weights_used_by_the_css_are_declared(self):
        """main.py and the tutorial ask for 400-800; each needs a real face."""
        css = (REPO / "static" / "fonts" / "jetbrains-mono.css").read_text(encoding="utf-8")
        declared = set(re.findall(r"font-weight:\s*(\d+)", css))
        assert {"400", "500", "600", "700", "800"} <= declared

    def test_open_font_licence_ships_beside_the_font(self):
        """OFL 1.1 requires the licence to travel with redistributed faces."""
        ofl = REPO / "static" / "fonts" / "OFL.txt"
        assert ofl.exists()
        assert "SIL OPEN FONT LICENSE" in ofl.read_text(encoding="utf-8").upper()
