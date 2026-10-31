"""Works out which column of a table holds which piece of information.

People name the same column many ways, so each table type has a list of known names
that is tried first. If a required column is still unknown, the model is asked once
for the whole file. The real files are all matched from the lists, so the model is
not called on them.

map_columns is the main function. map_headers and sniff do the matching from the
known names.
"""

import csv
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from app.config import Settings
from app.ingest.layouts import LAYOUTS, Column, Layout

ColumnMap = dict[str, int]  # field name -> column index

DELIMITERS = (",", ";", "\t", "|")
HEADER_SCAN_ROWS = 30  # how far down to look for the header row
SAMPLE_ROWS = 3  # data rows shown to the model with the headers


# --- Synonym mapping ------------------------------------------------------------


def header_key(text: object) -> str:
    """A header in plain lower case words, so spellings compare equal."""
    if text is None:
        return ""
    return " ".join(re.findall(r"[a-z0-9]+", str(text).casefold()))


def map_headers(layout: Layout, cells: Sequence[object]) -> ColumnMap:
    """The column for each field whose header is a known name. The leftmost match wins
    and a column serves only one field."""
    keys = [header_key(cell) for cell in cells]
    mapping: ColumnMap = {}
    used: set[int] = set()
    for column in layout.columns:
        for index, key in enumerate(keys):
            if index not in used and key in column.synonyms:
                mapping[column.name] = index
                used.add(index)
                break
    return mapping


def missing_required(layout: Layout, mapping: ColumnMap) -> list[Column]:
    """The required fields that still have no column."""
    return [c for c in layout.columns if c.required and c.name not in mapping]


def header_row(
    layout: Layout, rows: Sequence[Sequence[object]], limit: int = HEADER_SCAN_ROWS
) -> int | None:
    """The first row that looks like a header row, or None."""
    for index, row in enumerate(rows[:limit]):
        mapping = map_headers(layout, row)
        if layout.anchor in mapping and len(mapping) >= 2:
            return index
    return None


def _is_number(cell: object) -> bool:
    if isinstance(cell, bool):
        return False
    if isinstance(cell, int | float):
        return True
    try:
        float(str(cell))
    except ValueError:
        return False
    return True


def guess_header_row(
    rows: Sequence[Sequence[object]], limit: int = HEADER_SCAN_ROWS
) -> int | None:
    """A guess at the header row when no known names match: the first row with two or
    more filled cells and no numbers."""
    for index, row in enumerate(rows[:limit]):
        filled = [cell for cell in row if cell not in (None, "")]
        if len(filled) >= 2 and not any(_is_number(cell) for cell in filled):
            return index
    return None


def sniff(
    lines: Sequence[str], layouts: Sequence[Layout] = LAYOUTS
) -> tuple[Layout, str] | None:
    """The table type and separator of the first line that reads as a header row, or
    None. Only the first lines of a text file are checked."""
    for line in lines[:HEADER_SCAN_ROWS]:
        for delimiter in DELIMITERS:
            cells = next(csv.reader([line], delimiter=delimiter), [])
            for layout in layouts:
                mapping = map_headers(layout, cells)
                if layout.anchor in mapping and len(mapping) >= 2:
                    return layout, delimiter
    return None


# --- The model fallback ---------------------------------------------------------


class HeaderChoice(BaseModel):
    """The header that holds one field of one table, or None when none does."""

    table: str
    field: str
    header: str | None


class HeaderMap(BaseModel):
    """The model's answer: one choice for each field it was asked about."""

    choices: list[HeaderChoice]


HEADER_SYSTEM = (
    "You match the column headers of messy spreadsheets and exports from a machine "
    "shop to a fixed list of fields. For each field you are asked about, answer "
    "with the header text exactly as it appears in the file, or null when no header "
    "holds that field. Use each header for at most one field. Never invent a "
    "header. Answer for every table and every field listed."
)
HEADER_PURPOSE = "map headers"
HEADER_EFFORT = "low"

Ask = Callable[..., HeaderMap]


@dataclass(frozen=True)
class Table:
    """The header cells of one table, with a few data rows to show the model."""

    layout: Layout
    headers: Sequence[object]
    samples: Sequence[Sequence[object]] = ()


def _structured(*args: object, **kwargs: object) -> HeaderMap:
    # Imported here so matching from the known names does not load the model client.
    from app import llm

    return llm.structured(*args, **kwargs)


def _text(cell: object) -> str:
    return "" if cell is None else str(cell)


def _prompt(tables: Mapping[str, Table], mapped: Mapping[str, ColumnMap]) -> str:
    """The question for the model: the headers, a few rows and the missing fields of
    each table with a gap. The order is fixed so a repeat gives the same question."""
    lines: list[str] = []
    for name in sorted(tables):
        table = tables[name]
        if not missing_required(table.layout, mapped[name]):
            continue
        shown = [_text(cell) for cell in table.headers if _text(cell)]
        lines += [f"Table {name}: {table.layout.meaning}", f"Headers: {_dump(shown)}"]
        for row in table.samples[:SAMPLE_ROWS]:
            lines.append(f"Row: {_dump([_text(cell) for cell in row])}")
        lines.append("Fields to place:")
        for column in table.layout.columns:
            if column.name not in mapped[name]:
                lines.append(f"- {column.name}: {column.meaning}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _dump(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _find_header(cells: Sequence[object], wanted: str) -> int | None:
    """The column with this header, ignoring case and spacing if needed."""
    texts = [_text(cell).strip() for cell in cells]
    if wanted.strip() in texts:
        return texts.index(wanted.strip())
    keys = [header_key(text) for text in texts]
    key = header_key(wanted)
    return keys.index(key) if key and key in keys else None


def map_columns(
    tables: Mapping[str, Table],
    *,
    ask: Ask | None = None,
    settings: Settings | None = None,
) -> dict[str, ColumnMap]:
    """The column for each field of each table.

    Known names come first. If a required column is still missing, one model call
    covers every table in the file. Answers that make no sense are ignored.
    """
    mapped = {name: map_headers(t.layout, t.headers) for name, t in tables.items()}
    gaps = [
        name
        for name, table in tables.items()
        if missing_required(table.layout, mapped[name])
    ]
    if not gaps:
        return mapped
    arguments = (
        "sol",
        HeaderMap,
        HEADER_SYSTEM,
        _prompt(tables, mapped),
        HEADER_PURPOSE,
        HEADER_EFFORT,
    )
    # Settings go only to the default model call, since a custom one takes its own.
    answer = ask(*arguments) if ask else _structured(*arguments, settings=settings)
    for choice in answer.choices:
        if choice.table not in gaps or choice.header is None:
            continue
        table, columns = tables[choice.table], mapped[choice.table]
        column = table.layout.column(choice.field)
        if column is None or column.name in columns:
            continue
        index = _find_header(table.headers, choice.header)
        if index is not None and index not in columns.values():
            columns[column.name] = index
    return mapped
