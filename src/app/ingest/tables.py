"""Reads a spreadsheet or CSV file as rows, and finds the table the pipeline wants.

read_sheets loads every sheet of a workbook, or the single table of a CSV. find_table
and guess_table locate the right table and its header row. Each row keeps its cells
and its original text, which is later quoted as evidence.
"""

import csv
import datetime as dt
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from app.ingest import headers
from app.ingest.headers import ColumnMap, Table
from app.ingest.layouts import Layout

WORKBOOK_SUFFIXES = (".xlsx", ".xlsm")


@dataclass(frozen=True)
class Row:
    """One row of a table: its cells, and its original text for quoting."""

    cells: list[Any]
    text: str


def cell_text(value: object) -> str:
    """Return a cell as the text a person sees. A whole number loses its '.0'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return str(value).strip()


def row_text(cells: Sequence[object]) -> str:
    """Join the filled cells of a row with " | ", which is how a spreadsheet row is quoted."""
    return " | ".join(text for text in map(cell_text, cells) if text)


def cell(row: Row, columns: ColumnMap, name: str) -> object:
    """Return the cell for a field, or None if the column or cell is missing."""
    index = columns.get(name)
    if index is None or index >= len(row.cells):
        return None
    return row.cells[index]


def _delimited_rows(path: Path) -> list[Row]:
    """Read a delimited text file into rows, guessing the separator.

    Each row keeps its original line text, unchanged.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines(keepends=True)
    found = headers.sniff(lines)
    if found:
        delimiter = found[1]
    else:
        sample = [line for line in lines[: headers.HEADER_SCAN_ROWS] if line.strip()]
        delimiter = max(
            headers.DELIMITERS, key=lambda d: sum(line.count(d) for line in sample)
        )
    reader = csv.reader(iter(lines), delimiter=delimiter)
    rows: list[Row] = []
    consumed = 0
    for cells in reader:
        raw = "".join(lines[consumed : reader.line_num]).rstrip("\r\n")
        consumed = reader.line_num
        rows.append(Row(cells, raw))
    return rows


def read_sheets(path: Path) -> dict[str, list[Row]]:
    """Return the tables of a file by name: each workbook sheet, or the one text table."""
    if path.suffix.lower() not in WORKBOOK_SUFFIXES:
        return {path.name: _delimited_rows(path)}
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return {
            sheet.title: [
                Row(list(values), row_text(values))
                for values in sheet.iter_rows(values_only=True)
            ]
            for sheet in workbook.worksheets
        }
    finally:
        workbook.close()


def _is_blank(row: Row) -> bool:
    return not any(cell_text(value) for value in row.cells)


@dataclass(frozen=True)
class Located:
    """A table found in a file, with the position of its header row."""

    name: str
    rows: list[Row]
    header: int
    layout: Layout

    @property
    def data(self) -> list[Row]:
        """Return the rows under the header that are not blank."""
        return [row for row in self.rows[self.header + 1 :] if not _is_blank(row)]

    @property
    def above(self) -> str:
        """Return the text above the header, such as a title or a comment line."""
        return " ".join(
            text
            for row in self.rows[: self.header]
            for text in map(cell_text, row.cells)
            if text
        )

    def table(self) -> Table:
        """Return the header and a few sample rows, for matching columns."""
        samples = [row.cells for row in self.data[: headers.SAMPLE_ROWS]]
        return Table(self.layout, self.rows[self.header].cells, samples)


def find_table(
    sheets: dict[str, list[Row]], layout: Layout, skip: Collection[str] = ()
) -> Located | None:
    """Return the first sheet whose header matches the layout, or None."""
    for name, rows in sheets.items():
        if name in skip:
            continue
        index = headers.header_row(layout, [row.cells for row in rows])
        if index is not None:
            return Located(name, rows, index, layout)
    return None


def guess_table(
    sheets: dict[str, list[Row]], layout: Layout, skip: Collection[str] = ()
) -> Located | None:
    """Return the first sheet with a row that looks like a header, when the names are unknown."""
    for name, rows in sheets.items():
        if name in skip:
            continue
        index = headers.guess_header_row([row.cells for row in rows])
        if index is not None:
            return Located(name, rows, index, layout)
    return None
