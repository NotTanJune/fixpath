"""Finds the exact lines in a manual that back up a fact, to use as evidence.

The Pages class holds every line of the manual and can look up a value and return
the lines around it as a quote. Quotes come from the page text, never from the model.
"""

import re
from dataclasses import dataclass
from typing import Any

from app.ingest.manual_chunks import CODE_LINE, PAGE_NUMBER
from app.ingest.manual_schema import CONFIDENCE
from app.ingest.types import MAX_QUOTE, EvidenceDraft

_PART_LINE = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)+")


@dataclass(frozen=True)
class _Line:
    page: int
    text: str  # the line exactly as the page has it
    footer: bool = False  # true for the repeated footer line above the page number


def _tagged(text: str) -> list[tuple[str, bool]]:
    """Return the lines of a page, each flagged if it is the footer line."""
    lines = text.splitlines()
    filled = [i for i, line in enumerate(lines) if line.strip()]
    footer = None
    if len(filled) >= 2 and PAGE_NUMBER.fullmatch(lines[filled[-1]].strip()):
        footer = filled[-2]
    return [(line, i == footer) for i, line in enumerate(lines)]


class Pages:
    """Every line of every page in order, for finding the line that holds a value."""

    def __init__(self, pages: dict[int, str]) -> None:
        self.lines = [
            _Line(number, line, footer)
            for number, text in pages.items()
            for line, footer in _tagged(text)
        ]

    def find(
        self,
        needle: str,
        *,
        start: int = 0,
        stop: int | None = None,
        exact: bool = False,
    ) -> int | None:
        """Return the index of the first matching line, or None. Case is ignored."""
        wanted = needle.strip().casefold()
        if not wanted:
            return None
        for index in range(start, len(self.lines) if stop is None else stop):
            line = self.lines[index].text.strip().casefold()
            if (line == wanted) if exact else (wanted in line):
                return index
        return None

    def find_after(self, needle: str, start: int | None, **kwargs: Any) -> int | None:
        """Look for a line equal to the text first, then a line that contains it."""
        start = start or 0
        found = self.find(needle, start=start, exact=True, **kwargs)
        return found if found is not None else self.find(needle, start=start, **kwargs)

    def evidence(
        self, index: int | None, count: int = 1, doc_path: str = ""
    ) -> list[EvidenceDraft]:
        """Build evidence from a run of lines, stopping at the page end or quote limit."""
        if index is None:
            return []
        first = self.lines[index]
        taken = [first.text]
        for line in self.lines[index + 1 : index + count]:
            if line.page != first.page or line.footer:
                break
            taken.append(line.text)
        text = "\n".join(taken).strip()[:MAX_QUOTE].strip()
        if not text:
            return []
        return [
            EvidenceDraft(
                doc_path=doc_path,
                page=first.page,
                quote=text,
                extractor="sol-manual",
                confidence=CONFIDENCE,
            )
        ]

    def next_code_line(self, after: int) -> int:
        """Return the index of the next line that is only an alarm code, or the end."""
        for index in range(after + 1, len(self.lines)):
            if CODE_LINE.fullmatch(self.lines[index].text.strip()):
                return index
        return len(self.lines)

    def part_row_length(self, index: int, limit: int = 4) -> int:
        """Return how many lines one part row takes in a parts table."""
        count = 1
        while count < limit and index + count < len(self.lines):
            line = self.lines[index + count]
            if (
                line.page != self.lines[index].page
                or line.footer
                or _PART_LINE.fullmatch(line.text.strip())
            ):
                break
            count += 1
        return count
