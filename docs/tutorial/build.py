#!/usr/bin/env python3
"""Compile the documentation markdown into static/<page>/index.html.

Two pages share one template and one build: the tutorial (a walkthrough)
and the help page (reference — how to read what the server gives back,
where the live sample output is, licence and privacy). They are separate
documents because they answer different questions, but a single shell
keeps them visibly one manual.

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
from typing import NamedTuple

import markdown

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS = REPO / "docs" / "tutorial"
TEMPLATE = DOCS / "template.html"


class Page(NamedTuple):
    """One built page: its source, where it lands, and how it is titled."""

    src: pathlib.Path
    out_dir: pathlib.Path
    title: str          # <title>, before the " — PocketHunter Suite" suffix
    label: str          # masthead suffix, e.g. POCKETHUNTER/SUITE — HELP
    img: pathlib.Path | None = None


PAGES = (
    Page(
        src=DOCS / "tutorial.md",
        out_dir=REPO / "static" / "tutorial",
        title="Tutorial",
        label="TUTORIAL",
        img=DOCS / "img",
    ),
    Page(
        src=DOCS / "help.md",
        out_dir=REPO / "static" / "help",
        title="Help",
        label="HELP",
    ),
)

EXTENSIONS = ["toc", "tables", "fenced_code", "attr_list", "sane_lists", "md_in_html"]
EXTENSION_CONFIGS = {"toc": {"permalink": False, "toc_depth": "2-3"}}


def render(md_text: str, template_text: str, *, built_at: str,
           title: str = "Tutorial", label: str = "TUTORIAL") -> str:
    """Render markdown into the template. Pure: no filesystem access."""
    md = markdown.Markdown(extensions=EXTENSIONS,
                           extension_configs=EXTENSION_CONFIGS)
    content = md.convert(md_text)
    return (
        template_text
        .replace("{{ TOC }}", md.toc)
        .replace("{{ CONTENT }}", content)
        .replace("{{ BUILT_AT }}", built_at)
        .replace("{{ PAGE_TITLE }}", title)
        .replace("{{ PAGE_LABEL }}", label)
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

    template_text = TEMPLATE.read_text(encoding="utf-8")
    for page in PAGES:
        html = render(
            page.src.read_text(encoding="utf-8"),
            template_text,
            built_at=args.built_at,
            title=page.title,
            label=page.label,
        )

        page.out_dir.mkdir(parents=True, exist_ok=True)
        (page.out_dir / "index.html").write_text(html, encoding="utf-8")

        out_img = page.out_dir / "img"
        if out_img.exists():
            shutil.rmtree(out_img)
        if page.img is not None and page.img.exists():
            shutil.copytree(page.img, out_img)

        print(f"wrote {page.out_dir / 'index.html'} ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
