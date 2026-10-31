"""Reads the maintenance log export, a spreadsheet with one work order per row.

parse_maintenance_csv turns each row into a draft incident and links it to the machine,
the alarm and the parts named in that row. It leaves root causes for a later step.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ingest import headers
from app.ingest.evidence import CSV_CONFIDENCE, counted, note_missing, quote
from app.ingest.headers import ColumnMap
from app.ingest.layouts import MAINTENANCE
from app.ingest.normalize import (
    AliasTable,
    dashed_part,
    doc_path_for,
    normalize_alarm_code,
    note_date,
    parse_downtime,
)
from app.ingest.tables import Row, cell, cell_text, find_table, guess_table, read_sheets
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    EvidenceDraft,
    Extraction,
    NodeRef,
)

_PART_ENTRY = re.compile(r"(?P<name>.+?)\s*x\s*(?P<qty>\d+)", re.IGNORECASE)

# Matches an alarm code at the start of the text with no colon after it, such as
# "Err:252 relief valve" or "RB-560 axis 2". Without an ALM, MSG or ERR word the code
# must be strict, so a machine name like "CNC-01" is not mistaken for an alarm.
_LEADING_CODE = re.compile(
    r"(?:(?:ALM|MSG|ERR)[\s:_.-]*[A-Z]{0,3}-?\d{2,3}|(?-i:[A-Z]{1,3}-?\d{3}))(?=\s)",
    re.IGNORECASE,
)


def _split_alarm(problem: str) -> tuple[str | None, str]:
    """Split a problem text into its leading alarm code (if any) and the symptom."""
    head, colon, tail = problem.partition(": ")
    if colon:
        code = normalize_alarm_code(head)
        if code:
            return code, tail.strip()
    leading = _LEADING_CODE.match(problem.strip())
    if leading:
        code = normalize_alarm_code(leading.group(0))
        if code:
            return code, problem.strip()[leading.end() :].strip()
    return None, problem.strip()


def _parts_used(text: str) -> tuple[list[tuple[str, int]], int]:
    """Read a Parts cell into (part, quantity) pairs. An entry with no quantity counts as one."""
    used: list[tuple[str, int]] = []
    unquantified = 0
    for entry in (part.strip() for part in text.split(";")):
        if not entry:
            continue
        match = _PART_ENTRY.fullmatch(entry)
        if match:
            used.append((match["name"].strip(), int(match["qty"])))
        else:
            used.append((entry, 1))
            unquantified += 1
    return used, unquantified


@dataclass
class _Issues:
    """Counts the rows with a problem, so they can be reported as notes."""

    no_date: int = 0
    no_machine: int = 0
    unknown_machine: int = 0
    no_ref: int = 0
    unquantified: int = 0

    def notes(self) -> list[str]:
        found = [
            (
                self.no_date,
                "{n} row has no readable date",
                "{n} rows have no readable date",
            ),
            (self.no_machine, "{n} row has no machine", "{n} rows have no machine"),
            (
                self.unknown_machine,
                "{n} machine name is not in the alias table",
                "{n} machine names are not in the alias table",
            ),
            (
                self.no_ref,
                "{n} row has no work order ref and is keyed by its position",
                "{n} rows have no work order ref and are keyed by their positions",
            ),
            (
                self.unquantified,
                "{n} part entry has no quantity, so 1 is assumed",
                "{n} part entries have no quantity, so 1 is assumed",
            ),
        ]
        return [counted(count, one, many) for count, one, many in found if count]


def _draft(
    row: Row,
    position: int,
    columns: ColumnMap,
    doc_path: str,
    aliases: AliasTable,
    issues: _Issues,
) -> tuple[CandidateNode, list[CandidateEdge]]:
    """Build the draft incident for one row, with its links to machine, alarm and parts."""

    def text(name: str) -> str:
        return cell_text(cell(row, columns, name))

    def row_evidence() -> list[EvidenceDraft]:
        return quote(doc_path, row.text, "csv-parser", CSV_CONFIDENCE)

    ref = text("ref")
    if not ref:
        ref = f"{doc_path}#{position}"
        issues.no_ref += 1
    incident = NodeRef(type="Incident", key=ref)
    alarm, symptom = _split_alarm(text("problem"))

    props: dict[str, Any] = {}
    if "date" in columns:
        when = note_date(text("date"))
        props["date"] = when.isoformat() if when else None
        issues.no_date += when is None
    props["shift"] = None  # the export has a date but no time of day
    if "downtime" in columns:
        props["downtime_min"] = parse_downtime(text("downtime"))
    props["symptom_text"] = symptom or None
    if "tech" in columns:
        props["technician"] = text("tech") or None
    if "action" in columns:
        props["resolution_text"] = text("action") or None
    node = CandidateNode(
        ref=incident,
        label=symptom or ref,
        props=props,
        evidence=row_evidence(),
        canonical=False,
    )

    edges: list[CandidateEdge] = []
    machine = text("machine")
    if machine:
        tag = aliases.asset_for(machine)
        issues.unknown_machine += tag is None
        edges.append(
            CandidateEdge(
                src=incident,
                rel="OCCURRED_ON",
                dst=NodeRef(type="Asset", key=tag or machine),
                evidence=row_evidence(),
            )
        )
    elif "machine" in columns:
        issues.no_machine += 1
    if alarm:
        edges.append(
            CandidateEdge(
                src=incident,
                rel="TRIGGERED_BY",
                dst=NodeRef(type="AlarmCode", key=alarm),
                evidence=row_evidence(),
            )
        )
    used, unquantified = _parts_used(text("parts"))
    issues.unquantified += unquantified
    for name, qty in used:
        edges.append(
            CandidateEdge(
                src=incident,
                rel="USED",
                dst=NodeRef(type="Part", key=dashed_part(name) or name),
                props={"qty": qty},
                evidence=row_evidence(),
            )
        )
    return node, edges


def parse_maintenance_csv(
    path: Path, aliases: AliasTable, settings: Settings | None = None
) -> Extraction:
    """Read the maintenance export into one draft incident per row.

    Machine names are matched through the alias table and kept as written when no
    match is found. Rows that repeat a work order ref stay separate drafts.
    """
    path = Path(path)
    doc_path = doc_path_for(path)
    extraction = Extraction(
        doc_path=doc_path, doc_type="maintenance_note", extractor="csv-parser"
    )
    sheets = read_sheets(path)
    located = find_table(sheets, MAINTENANCE) or guess_table(sheets, MAINTENANCE)
    if located is None:
        extraction.notes.append("no table with a date column was found")
        return extraction
    columns = headers.map_columns({"maintenance": located.table()}, settings=settings)[
        "maintenance"
    ]
    note_missing(located, columns, extraction.notes)

    issues = _Issues()
    for position, row in enumerate(located.data, start=1):
        node, edges = _draft(row, position, columns, doc_path, aliases, issues)
        extraction.nodes.append(node)
        extraction.edges += edges
    extraction.notes += issues.notes()
    return extraction
