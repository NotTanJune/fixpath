"""Reads the spares inventory: the stores workbook and the old stock list.

parse_inventory reads the spare parts, and the machines and production lines from the
Machines sheet. build_alias_table reads the same sheet into a list of the names people
use for each machine, which the other extractors rely on.
"""

import datetime as dt
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ingest import headers
from app.ingest.evidence import INVENTORY_CONFIDENCE, counted, note_missing, quote
from app.ingest.headers import ColumnMap
from app.ingest.layouts import MACHINES, SPARES
from app.ingest.normalize import (
    AliasTable,
    MachineEntry,
    dashed_part,
    doc_path_for,
    find_date,
    lead_time_days,
    line_id,
    normalize_bin,
    parse_cost,
    parse_quantity,
)
from app.ingest.tables import (
    WORKBOOK_SUFFIXES,
    Located,
    Row,
    cell,
    cell_text,
    find_table,
    guess_table,
    read_sheets,
)
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    Extraction,
    NodeRef,
)
from app.ontology import validate_id

# --- The machines sheet and the alias table -------------------------------------


@dataclass(frozen=True)
class _MachineRow:
    tag: str
    description: str
    make: str
    model: str
    serial: str
    location: str
    aliases: list[str]
    text: str


def _machine_rows(located: Located, columns: ColumnMap) -> list[_MachineRow]:
    machines = []
    for row in located.data:
        text = {name: cell_text(cell(row, columns, name)) for name in columns}
        if not text.get("tag"):
            continue
        aliases = [a.strip() for a in text.get("aliases", "").split(",") if a.strip()]
        machines.append(
            _MachineRow(
                tag=text["tag"],
                description=text.get("description", ""),
                make=text.get("make", ""),
                model=text.get("model", ""),
                serial=text.get("serial", ""),
                location=text.get("location", ""),
                aliases=aliases,
                text=row.text,
            )
        )
    return machines


def _locate(sheets: dict[str, list[Row]]) -> tuple[Located | None, Located | None]:
    """Find the Machines table and the spares table of a file. Known column names are
    tried first, then a guess is made for tables that do not match."""
    machines = find_table(sheets, MACHINES)
    taken = [machines.name] if machines else []
    spares = find_table(sheets, SPARES, taken) or guess_table(sheets, SPARES, taken)
    if machines is None and spares is not None:
        machines = guess_table(sheets, MACHINES, [spares.name])
    return machines, spares


@dataclass(frozen=True)
class _Tables:
    """The tables of one inventory file and which column holds what."""

    machines: Located | None
    spares: Located | None
    columns: dict[str, ColumnMap]


@lru_cache(maxsize=8)
def _mapped(file: tuple[str, int, int], settings: Settings | None) -> _Tables:
    sheets = read_sheets(Path(file[0]))
    machines, spares = _locate(sheets)
    found = {"spares": spares, "machines": machines}
    tables = {name: located.table() for name, located in found.items() if located}
    columns = headers.map_columns(tables, settings=settings) if tables else {}
    return _Tables(machines, spares, columns)


def _tables(path: Path, settings: Settings | None = None) -> _Tables:
    """The tables of a file with their columns matched. The result is remembered while
    the file stays unchanged, so an unfamiliar layout costs at most one model call."""
    resolved = path.resolve()
    status = resolved.stat()
    return _mapped((str(resolved), status.st_size, status.st_mtime_ns), settings)


def build_alias_table(xlsx_path: Path, settings: Settings | None = None) -> AliasTable:
    """The names people use for each machine, from the Machines sheet. A workbook with
    no such sheet gives an empty list, and a machine with an invalid tag is left out."""
    tables = _tables(Path(xlsx_path), settings)
    located = tables.machines
    if located is None:
        return AliasTable([])
    columns = tables.columns["machines"]
    return AliasTable(
        MachineEntry(
            tag=machine.tag,
            serial=machine.serial,
            model=machine.model,
            aliases=tuple(machine.aliases),
        )
        for machine in _machine_rows(located, columns)
        if validate_id("Asset", machine.tag)
    )


# --- The inventory --------------------------------------------------------------


def _machine_nodes(
    machines: list[_MachineRow], doc_path: str
) -> tuple[list[CandidateNode], list[CandidateNode], list[CandidateEdge]]:
    """The production lines, the machines and the links between them."""
    lines: dict[str, CandidateNode] = {}
    assets: list[CandidateNode] = []
    edges: list[CandidateEdge] = []
    for machine in machines:
        evidence = (
            doc_path,
            machine.text,
            "inventory-normalizer",
            INVENTORY_CONFIDENCE,
        )
        name, _, machine_type = machine.description.partition(", ")
        asset = NodeRef(type="Asset", key=machine.tag)
        assets.append(
            CandidateNode(
                ref=asset,
                label=name or machine.tag,
                props={
                    "id": machine.tag,
                    "name": name or None,
                    "machine_type": machine_type or None,
                    "vendor": machine.make or None,
                    "model": machine.model or None,
                    "aliases": machine.aliases,
                },
                evidence=quote(*evidence),
                canonical=validate_id("Asset", machine.tag),
            )
        )
        line = line_id(machine.location)
        if line is None:
            continue
        if line not in lines:
            title = f"Line {int(line.removeprefix('LINE-'))}"
            lines[line] = CandidateNode(
                ref=NodeRef(type="Line", key=line),
                label=title,
                props={"id": line, "name": title},
                evidence=quote(*evidence),
                canonical=True,
            )
        edges.append(
            CandidateEdge(
                src=NodeRef(type="Line", key=line),
                rel="HAS_MACHINE",
                dst=asset,
                evidence=quote(*evidence),
            )
        )
    return list(lines.values()), assets, edges


def _part_nodes(
    located: Located,
    columns: ColumnMap,
    doc_path: str,
    stock_date: dt.date | None,
    notes: list[str],
) -> list[CandidateNode]:
    parts: list[CandidateNode] = []
    unreadable = skipped = 0
    for row in located.data:
        raw_no = cell_text(cell(row, columns, "part_no"))
        if not raw_no:
            skipped += 1
            continue
        dashed = dashed_part(raw_no)
        key = dashed or raw_no
        description = cell_text(cell(row, columns, "description"))
        props: dict[str, Any] = {"part_no": key}
        if "description" in columns:
            props["description"] = description or None
        if "qty" in columns:
            raw_qty = cell(row, columns, "qty")
            props["qty_on_hand"] = parse_quantity(raw_qty)
            if props["qty_on_hand"] is None and cell_text(raw_qty):
                unreadable += 1
        if "bin" in columns:
            props["bin"] = normalize_bin(cell(row, columns, "bin"))
        if "lead_time" in columns:
            props["lead_time_days"] = lead_time_days(cell(row, columns, "lead_time"))
        if "cost" in columns:
            props["unit_cost_sgd"] = parse_cost(cell(row, columns, "cost"))
        props["stock_as_of"] = stock_date.isoformat() if stock_date else None
        parts.append(
            CandidateNode(
                ref=NodeRef(type="Part", key=key),
                label=(description or key) if dashed else raw_no,
                props=props,
                evidence=quote(
                    doc_path, row.text, "inventory-normalizer", INVENTORY_CONFIDENCE
                ),
                canonical=dashed is not None,
            )
        )
    if unreadable:
        notes.append(
            counted(
                unreadable,
                "{n} quantity could not be read as a count",
                "{n} quantities could not be read as a count",
            )
        )
    if skipped:
        notes.append(
            counted(
                skipped,
                "{n} row without a part number was skipped",
                "{n} rows without a part number were skipped",
            )
        )
    return parts


def parse_inventory(
    path: Path, aliases: AliasTable | None = None, settings: Settings | None = None
) -> Extraction:
    """Read the spares workbook or old stock list into parts, machines and lines.

    Quantities, bins, lead times and costs are cleaned up. The stock date is the date
    written above the table. The aliases argument is unused, since this file is where
    the aliases come from.
    """
    path = Path(path)
    doc_path = doc_path_for(path)
    extraction = Extraction(
        doc_path=doc_path, doc_type="inventory", extractor="inventory-normalizer"
    )
    notes = extraction.notes
    tables = _tables(path, settings)
    machines, spares, columns = tables.machines, tables.spares, tables.columns

    if machines:
        note_missing(machines, columns["machines"], notes)
        rows = _machine_rows(machines, columns["machines"])
        notes += [
            f"machine tag {row.tag} is not a valid asset id, so it is not in the "
            "alias table"
            for row in rows
            if not validate_id("Asset", row.tag)
        ]
        lines, assets, edges = _machine_nodes(rows, doc_path)
        extraction.nodes += lines + assets
        extraction.edges += edges
    elif path.suffix.lower() in WORKBOOK_SUFFIXES:
        notes.append("no Machines table found")
    if spares is None:
        notes.append("no table with a part number column was found")
        return extraction
    note_missing(spares, columns["spares"], notes)
    if "part_no" not in columns["spares"]:
        return extraction
    stock_date = find_date(spares.above)
    if stock_date is None:
        notes.append("no stock count date found above the table")
    extraction.nodes += _part_nodes(
        spares, columns["spares"], doc_path, stock_date, notes
    )
    return extraction
