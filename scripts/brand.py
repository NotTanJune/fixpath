"""Render the brand templates and check that the name stays in config/brand.json.

`render` fills the deck template from config/brand.json and writes the result.
`check` scans the text files and fails if the name or a spelling of it appears
anywhere except config/brand.json, the generated files and README.md. Run either one with
`uv run python scripts/brand.py`.
"""

import argparse
import dataclasses
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from app.brand import Brand, load_brand
from app.config import ROOT

TEMPLATE_PAIRS = (("docs/deck/deck.template.md", "docs/deck/deck.md"),)
# Written by hand, so render never touches it. It may carry the name.
HAND_WRITTEN = frozenset({"README.md"})
HEADER = (
    "<!-- Generated from {template} by scripts/brand.py. "
    "Edit the template, not this file. -->"
)
PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".cache",
    }
)
# Folders skipped by their path, not their name. docs/private holds private files that
# git does not track.
SKIP_PATHS = frozenset({"docs/private"})
SKIP_FILES = frozenset(
    {
        "config/brand.json",
        *(output for _, output in TEMPLATE_PAIRS),
        *HAND_WRITTEN,
        "docs/deck/deck.pdf",
    }
)
MAX_BYTES = 5 * 1024 * 1024
BINARY_SNIFF_BYTES = 8 * 1024


def render_text(text: str, brand: Brand) -> tuple[str, list[tuple[int, str]]]:
    """Fill in the placeholders. Also return any placeholder that has no value."""
    values = dataclasses.asdict(brand)
    unknown: list[tuple[int, str]] = []

    def substitute(match: re.Match[str]) -> str:
        token = match.group(0)
        if token[2:-2] in values:
            return values[token[2:-2]]
        unknown.append((text.count("\n", 0, match.start()) + 1, token))
        return token

    return PLACEHOLDER.sub(substitute, text), unknown


def render(root: Path) -> int:
    brand = load_brand()
    failed = False
    for template_path, output_path in TEMPLATE_PAIRS:
        template = root / template_path
        if not template.is_file():
            continue
        rendered, unknown = render_text(template.read_text(encoding="utf-8"), brand)
        if unknown:
            failed = True
            for line, token in unknown:
                print(
                    f"{template_path}:{line}: unknown placeholder {token}",
                    file=sys.stderr,
                )
            continue
        header = HEADER.format(template=template_path)
        (root / output_path).write_text(
            f"{header}\n\n{rendered}", encoding="utf-8", newline="\n"
        )
        print(f"wrote {output_path}")
    return 1 if failed else 0


def brand_pattern(brand: Brand) -> re.Pattern[str]:
    """Build a pattern for the name, its short form, and a spaced spelling.

    The spaced spelling cannot start right after a letter, so two ordinary words
    that happen to join into it do not count."""
    parts = re.split(r"\s+|(?<=[a-z0-9])(?=[A-Z])", brand.name.strip())
    spaced = r"(?<![A-Za-z])" + r"\s*".join(re.escape(part) for part in parts if part)
    variants = [re.escape(brand.name), re.escape(brand.slug), spaced]
    return re.compile("|".join(variants), re.IGNORECASE)


def is_secret_env_file(filename: str) -> bool:
    """Say whether a file is an .env file that holds secrets. .env.example is safe."""
    return filename == ".env" or (
        filename.startswith(".env.") and filename != ".env.example"
    )


def scannable_files(root: Path) -> Iterator[tuple[Path, str]]:
    """List the files the check reads.

    It skips links, so it never reads outside the root, and it skips secret files."""
    for directory, dirnames, filenames in os.walk(root):
        here = Path(directory).relative_to(root).as_posix()
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIRS
            and (f"{here}/{name}" if here != "." else name) not in SKIP_PATHS
        )
        for filename in sorted(filenames):
            path = Path(directory, filename)
            relative = path.relative_to(root).as_posix()
            if (
                relative in SKIP_FILES
                or is_secret_env_file(filename)
                or path.is_symlink()
            ):
                continue
            yield path, relative


def read_text_file(path: Path) -> str | None:
    """Return the file text, or None for a binary or very large file."""
    if path.stat().st_size > MAX_BYTES:
        return None
    data = path.read_bytes()
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", errors="replace")


def check(root: Path) -> int:
    pattern = brand_pattern(load_brand())
    hits: list[str] = []
    for path, relative in scannable_files(root):
        text = read_text_file(path)
        if text is None:
            continue
        for number, line in enumerate(text.split("\n"), start=1):
            hits.extend(
                f"{relative}:{number}: {match.group(0)}"
                for match in pattern.finditer(line)
            )
    if hits:
        print("\n".join(hits))
        return 1
    print("brand check passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brand.py", description=__doc__.split("\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("render", "write docs/deck/deck.md from its template"),
        ("check", "fail if the brand appears outside config/brand.json"),
    ):
        command = commands.add_parser(name, help=text, description=text)
        command.add_argument(
            "--root", type=Path, default=ROOT, help="repo root (default: this repo)"
        )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"--root {args.root} is not a directory")
    return render(root) if args.command == "render" else check(root)


if __name__ == "__main__":
    sys.exit(main())
