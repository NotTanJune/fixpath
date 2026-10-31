"""Reads the pages of a manual PDF and cuts them into sections for search.

read_pages gets the text of each page, body_lines strips the repeated footer, and
chunk_text cuts the pages into chunks of a useful size. No model is involved.
"""

import math
import re
from itertools import pairwise
from pathlib import Path

from pypdf import PdfReader

MIN_CHUNK = 300
MAX_CHUNK = 1200

PAGE_NUMBER = re.compile(r"Page \d+")
_HEADING = re.compile(r"\d+(?:\.\d+)*\s+[A-Z]")
CODE_LINE = re.compile(r"[A-Z]{1,3}-\d{3}")


def read_pages(path: Path) -> dict[int, str]:
    """Return the text of each page of a PDF, keyed by page number from 1."""
    reader = PdfReader(path)
    return {
        number: page.extract_text() or "" for number, page in enumerate(reader.pages, 1)
    }


def body_lines(text: str) -> list[str]:
    """Return the non-empty lines of a page, without the footer and page number."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2 and PAGE_NUMBER.fullmatch(lines[-1]):
        lines = lines[:-2]
    return lines


def _size(lines: list[str]) -> int:
    return len("\n".join(lines))


# --- Section chunks ---------------------------------------------------------------

# A section is its lines, plus how many characters of it sit on each page. Only a
# section joined from several pages spans more than one page.
_Section = tuple[dict[int, int], list[str]]


def _page_of(section: _Section) -> int:
    """Return the page that holds most of a section, the earlier one on a tie."""
    chars = section[0]
    return min(chars, key=lambda page: (-chars[page], page))


def _sections(pages: dict[int, str]) -> list[_Section]:
    """Cut the text at every heading and every page break."""
    sections: list[_Section] = []
    for number, text in pages.items():
        current: _Section | None = None
        for line in body_lines(text):
            if current is None or _HEADING.match(line):
                current = ({}, [])
                sections.append(current)
            current[0][number] = current[0].get(number, 0) + len(line) + 1
            current[1].append(line)
    return sections


def _join(first: _Section, second: _Section) -> _Section:
    chars = dict(first[0])
    for page, count in second[0].items():
        chars[page] = chars.get(page, 0) + count
    return chars, first[1] + second[1]


def _merge_small(sections: list[_Section]) -> list[_Section]:
    """Join each short section to a neighbour when the two fit in one chunk.

    A neighbour on the same page comes first, and the next one before the previous one.
    """
    sections = list(sections)
    while True:
        for index, section in enumerate(sections):
            lines = section[1]
            if _size(lines) >= MIN_CHUNK:
                continue
            page = _page_of(section)
            neighbours = [j for j in (index + 1, index - 1) if 0 <= j < len(sections)]
            neighbours.sort(
                key=lambda j: _page_of(sections[j]) != page
            )  # a stable sort keeps the next section first
            target = next(
                (
                    j
                    for j in neighbours
                    if _size(lines) + 1 + _size(sections[j][1]) <= MAX_CHUNK
                ),
                None,
            )
            if target is None:
                continue
            first, second = sorted((index, target))
            sections[first : second + 1] = [_join(sections[first], sections[second])]
            break
        else:
            return sections


def _split_long(lines: list[str]) -> list[list[str]]:
    """Cut a long section at line breaks into pieces of similar size.

    A cut prefers to fall before an alarm code or a heading when one is near.
    """
    total = _size(lines)
    if total <= MAX_CHUNK:
        return [lines]
    pieces = math.ceil(total / (MAX_CHUNK * 0.8))
    ends = [0]  # running character count before each line
    for line in lines:
        ends.append(ends[-1] + len(line) + 1)
    window = total / pieces * 0.1
    cuts: list[int] = []
    for k in range(1, pieces):
        want = total * k / pieces
        near = [i for i in range(1, len(lines)) if abs(ends[i] - want) <= window]
        liked = [
            i for i in near if CODE_LINE.fullmatch(lines[i]) or _HEADING.match(lines[i])
        ]
        pool = (
            liked
            or near
            or [min(range(1, len(lines)), key=lambda i: abs(ends[i] - want))]
        )
        cuts.append(min(pool, key=lambda i: abs(ends[i] - want)))
    bounds = [0, *sorted(set(cuts)), len(lines)]
    return [lines[a:b] for a, b in pairwise(bounds)]


def chunk_text(pages: dict[int, str]) -> list[tuple[int, str]]:
    """Cut a manual into chunks, as (page, text) pairs in reading order.

    A chunk starts at a numbered heading or at the top of a page. Very short
    sections join a neighbour and very long ones are split. The footer and page
    number are left out.
    """
    chunks: list[tuple[int, str]] = []
    for section in _merge_small(_sections(pages)):
        page = _page_of(section)
        chunks += [(page, "\n".join(piece)) for piece in _split_long(section[1])]
    return chunks
