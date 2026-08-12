#!/usr/bin/env python3
"""Compile tutorial.md into static/tutorial/index.html.

Streamlit serves ./static/ under /app/static/ (enableStaticServing in
.streamlit/config.toml), and docker-compose bind-mounts the repository
into the container, so the built page is served with no rebuild.

Heading ids and the table of contents both come from python-markdown's
`toc` extension. Deriving both from one pass is deliberate: a hand-kept
table of contents drifts, and a dead in-page anchor produces no error —
the browser just does not scroll — so the drift stays invisible.

`markdown` is a build-time dependency only. It runs here, on an author's
machine, and is never imported by the application or installed into any
container image.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import shutil
import sys

import markdown

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC_MD = REPO / "docs" / "tutorial" / "tutorial.md"
TEMPLATE = REPO / "docs" / "tutorial" / "template.html"
SRC_IMG = REPO / "docs" / "tutorial" / "img"
OUT_DIR = REPO / "static" / "tutorial"

EXTENSIONS = ["toc", "tables", "fenced_code", "attr_list", "sane_lists", "md_in_html"]
EXTENSION_CONFIGS = {"toc": {"permalink": False, "toc_depth": "2-3"}}


def render(md_text: str, template_text: str, *, built_at: str) -> str:
    """Render markdown into the template. Pure: no filesystem access."""
    md = markdown.Markdown(extensions=EXTENSIONS,
                           extension_configs=EXTENSION_CONFIGS)
    content = md.convert(md_text)
    return (
        template_text
        .replace("{{ TOC }}", md.toc)
        .replace("{{ CONTENT }}", content)
        .replace("{{ BUILT_AT }}", built_at)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--built-at",
        default=_dt.date.today().isoformat(),
        help="Build date stamped into the page. Pin it to make builds "
             "byte-reproducible.",
    )
    args = parser.parse_args(argv)

    html = render(
        SRC_MD.read_text(encoding="utf-8"),
        TEMPLATE.read_text(encoding="utf-8"),
        built_at=args.built_at,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")

    out_img = OUT_DIR / "img"
    if out_img.exists():
        shutil.rmtree(out_img)
    if SRC_IMG.exists():
        shutil.copytree(SRC_IMG, out_img)

    print(f"wrote {OUT_DIR / 'index.html'} ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
