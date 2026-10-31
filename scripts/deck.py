"""Build the deck: diagrams, the PDF, the private copy and the slide images.

Run it with `uv run python scripts/deck.py` and one of four commands. `diagrams` turns
the diagram sources into SVG files. `pdf` exports the deck to a PDF. `private` builds a
second copy of the deck with one extra line on the title slide, read from a file that
git does not track. `png` saves every slide as an image outside the repo. Marp and
Mermaid run through bunx, so nothing is installed in the repo.
"""

import argparse
import base64
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.config import ROOT

DOCS = ROOT / "docs"
DIAGRAMS = DOCS / "diagrams"
THEME = DOCS / "deck" / "shiftboard.css"
DECK = DOCS / "deck" / "deck.md"
PDF = DOCS / "deck" / "deck.pdf"
PRIVATE = DOCS / "private"
AUDIENCE = PRIVATE / "audience.txt"
FONTS = ROOT / "web" / "node_modules" / "@fontsource"
MARP = ["bunx", "@marp-team/marp-cli"]
MERMAID = ["bunx", "@mermaid-js/mermaid-cli"]
MARKER = '<p class="audience"></p>'
MISSING_FONTS = (
    f"the font files are missing; run `cd web && bun install` first (looked in {FONTS})"
)

# The fonts that diagram text may use. Digits come from Barlow, as in the app, so
# Atkinson's slashed zero never shows in a figure.
DIGITS = "U+0030-0039"
DIAGRAM_FONTS = (
    (
        "Atkinson Hyperlegible",
        400,
        "atkinson-hyperlegible/files/atkinson-hyperlegible-latin-400-normal.woff2",
        None,
    ),
    (
        "Atkinson Hyperlegible",
        700,
        "atkinson-hyperlegible/files/atkinson-hyperlegible-latin-700-normal.woff2",
        None,
    ),
    (
        "Figures",
        400,
        "barlow-semi-condensed/files/barlow-semi-condensed-latin-500-normal.woff2",
        DIGITS,
    ),
    (
        "Figures",
        700,
        "barlow-semi-condensed/files/barlow-semi-condensed-latin-700-normal.woff2",
        DIGITS,
    ),
)


def browser_flags() -> list[str]:
    """Point Marp at the browser that Mermaid already downloaded.

    This keeps Marp away from the browser you are using. DECK_BROWSER sets another path."""
    path = os.environ.get("DECK_BROWSER")
    if not path:
        cache = Path.home() / ".cache" / "puppeteer" / "chrome"
        found = sorted(cache.glob("*/*/Google Chrome for Testing.app/Contents/MacOS/*"))
        found += sorted(cache.glob("*/*/chrome"))
        path = str(found[-1]) if found else None
    return ["--browser-path", path] if path else []


def run(command: list[str]) -> None:
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=400
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit(f"command failed: {' '.join(command)}")


def font_css() -> str:
    """Build the font rules for the diagrams, with the font files inside them."""
    rules = []
    for family, weight, relative, digits in DIAGRAM_FONTS:
        path = FONTS / relative
        if not path.is_file():
            raise SystemExit(MISSING_FONTS)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
            f'src: url("data:font/woff2;base64,{data}") format("woff2"); '
            + (f"unicode-range: {digits}; " if digits else "")
            + "}"
        )
    return "\n".join(rules) + "\n"


def embed_fonts(svg: Path, css: str) -> None:
    """Put the fonts inside the SVG so it draws correctly as an image.

    Mermaid sizes its boxes with a plain system font, and the app's font fits inside them."""
    text = svg.read_text(encoding="utf-8")
    opening = text.index(">") + 1
    svg.write_text(
        f"{text[:opening]}<style>{css}</style>{text[opening:]}", encoding="utf-8"
    )


def diagrams() -> int:
    sources = sorted(DIAGRAMS.glob("*.mmd"))
    if not sources:
        raise SystemExit(f"no diagram sources in {DIAGRAMS}")
    with tempfile.TemporaryDirectory() as scratch:
        fonts = font_css()
        for source in sources:
            output = source.with_suffix(".svg")
            run(
                [
                    *MERMAID,
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--configFile",
                    str(DIAGRAMS / "mermaid.config.json"),
                    "--backgroundColor",
                    "transparent",
                    "--quiet",
                ]
            )
            embed_fonts(output, fonts)
            print(f"wrote {output.relative_to(ROOT)}")
    return 0


def local_theme(scratch: Path) -> Path:
    """Copy the theme with full font paths, so Marp finds the fonts."""
    if not FONTS.is_dir():
        raise SystemExit(MISSING_FONTS)
    text = THEME.read_text(encoding="utf-8")
    base = (ROOT / "web" / "node_modules").as_uri()
    text = text.replace("../../web/node_modules", base)
    path = scratch / THEME.name
    path.write_text(text, encoding="utf-8")
    return path


def marp(source: Path, scratch: Path, *flags: str) -> None:
    run(
        [
            *MARP,
            "--theme-set",
            str(local_theme(scratch)),
            "--html",
            "--allow-local-files",
            *browser_flags(),
            *flags,
            str(source),
        ]
    )


def pdf(source: Path = DECK, output: Path = PDF) -> int:
    if not source.is_file():
        raise SystemExit(f"{source.relative_to(ROOT)} is missing; run make brand first")
    with tempfile.TemporaryDirectory() as scratch:
        marp(source, Path(scratch), "--pdf", "--pdf-outlines", "--output", str(output))
    print(f"wrote {output.relative_to(ROOT)}")
    return 0


def audience_line() -> str:
    if not AUDIENCE.is_file():
        raise SystemExit(
            "docs/private/audience.txt is missing; create it with one line for "
            "the title slide, then run make deck-private again"
        )
    lines = [line.strip() for line in AUDIENCE.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) != 1:
        raise SystemExit("docs/private/audience.txt must hold exactly one line")
    return lines[0]


def private() -> int:
    line = audience_line()
    if not DECK.is_file():
        raise SystemExit("docs/deck/deck.md is missing; run make brand first")
    text = DECK.read_text(encoding="utf-8")
    if MARKER not in text:
        raise SystemExit("the title slide has no audience marker")
    text = text.replace(MARKER, f'<p class="audience">{html.escape(line)}</p>', 1)
    # The private copy sits one folder down, so its local paths go up one level.
    text = re.sub(r'(\]\(|src=")(images|diagrams)/', r"\1../\2/", text)
    PRIVATE.mkdir(exist_ok=True)
    target = PRIVATE / "deck.md"
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {target.relative_to(ROOT)}")
    return pdf(target, PRIVATE / "deck.pdf")


def png(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        marp(
            DECK,
            Path(scratch),
            "--images",
            "png",
            "--image-scale",
            "1.5",
            "--output",
            str(out_dir / "slide.png"),
        )
    print(f"wrote slides to {out_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deck.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("diagrams", help="render docs/diagrams/*.mmd to SVG")
    commands.add_parser("pdf", help="export docs/deck/deck.md to docs/deck/deck.pdf")
    commands.add_parser("private", help="build the private copy and its PDF")
    images = commands.add_parser("png", help="render every slide to a PNG")
    images.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)

    if args.command == "diagrams":
        return diagrams()
    if args.command == "pdf":
        return pdf()
    if args.command == "private":
        return private()
    return png(args.out_dir.resolve())


if __name__ == "__main__":
    shutil.which("bunx") or sys.exit("bunx is not on PATH; install bun first")
    sys.exit(main())
