"""The read-only views behind the web API.

This file turns the stored graph into what the screens need: the graph itself, the
sources with the places where facts cite them, the recent breakdowns and alarms, the
pipeline report and the evaluation results. Each view opens its own read-only
connection and waits briefly if the database is busy.

`graph_elements`, `activity`, `list_sources` and `get_source` are the main views.
"""

import json
import re
import sqlite3
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any

from app.api import sheet_styles, uploaded
from app.ask.tools import connect_ro
from app.ingest import manual_chunks, tables
from app.ontology import NODE_TYPES, RELATIONS

BUSY_TIMEOUT_MS = 5000
BUSY = "The graph database is busy; try again in a moment."
WORKBOOKS = (".xlsx", ".xlsm")
DOC_ID = re.compile(NODE_TYPES["Document"].id_pattern)
MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}
OCTET_STREAM = "application/octet-stream"


class NotFound(LookupError):
    """Raised when something a view looked for is missing. The message goes to the client."""


def is_busy(error: sqlite3.OperationalError) -> bool:
    """Say whether an error means the database stayed locked too long."""
    return "locked" in str(error).lower() or "busy" in str(error).lower()


def connect(kg_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to the graph that waits up to 5 seconds if it is busy."""
    if not Path(kg_path).is_file():
        raise NotFound("there is no graph database yet; run a full ingest first")
    conn = connect_ro(kg_path)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


# --- the graph ---------------------------------------------------------------------


def _node_data(row: sqlite3.Row) -> dict[str, Any]:
    spec = NODE_TYPES.get(row["type"])
    return {
        "id": row["id"],
        "label": row["label"],
        "type": row["type"],
        "family": spec.family if spec else None,
        "glyph": spec.glyph if spec else None,
    }


def _edge_data(row: sqlite3.Row) -> dict[str, Any]:
    relation = RELATIONS.get(row["rel"])
    return {
        "id": row["id"],
        "source": row["src"],
        "target": row["dst"],
        "rel": row["rel"],
        "label": relation.label if relation else row["rel"].lower().replace("_", " "),
    }


def graph_elements(kg_path: Path, focus: str | None, depth: int) -> dict[str, list]:
    """Return the graph in the form the graph view draws.

    With a focus, return the nodes near it and the edges between them. Without one,
    return the whole graph. Raise NotFound if the focus is not a node."""
    with closing(connect(kg_path)) as conn:
        nodes = {
            row["id"]: row for row in conn.execute("SELECT id, type, label FROM nodes")
        }
        edges = conn.execute(
            "SELECT id, src, rel, dst FROM edges ORDER BY id"
        ).fetchall()
    if focus is None:
        keep = set(nodes)
    else:
        if focus not in nodes:
            raise NotFound(f"there is no node {focus}")
        near: dict[str, list[str]] = {}
        for edge in edges:
            near.setdefault(edge["src"], []).append(edge["dst"])
            near.setdefault(edge["dst"], []).append(edge["src"])
        keep, queue = {focus}, deque([(focus, 0)])
        while queue:
            current, distance = queue.popleft()
            if distance == depth:
                continue
            for other in near.get(current, ()):
                if other not in keep:
                    keep.add(other)
                    queue.append((other, distance + 1))
    return {
        "nodes": [{"data": _node_data(nodes[i])} for i in sorted(keep) if i in nodes],
        "edges": [
            {"data": _edge_data(edge)}
            for edge in edges
            if edge["src"] in keep and edge["dst"] in keep
        ],
    }


# --- the activity of the line --------------------------------------------------------

# The first piece of evidence for a fact tells the client which source to open.
_FIRST_DOC = (
    "(SELECT doc_id FROM evidence WHERE fact_kind = '{kind}' AND fact_id = {id}"
    " ORDER BY id LIMIT 1)"
)
_ONE_TARGET = (
    "(SELECT dst FROM edges WHERE src = n.id AND rel = '{rel}' ORDER BY dst LIMIT 1)"
)

BREAKDOWNS_SQL = f"""
SELECT n.id AS id,
       json_extract(n.props, '$.date') AS date,
       json_extract(n.props, '$.shift') AS shift,
       json_extract(n.props, '$.downtime_min') AS downtime_min,
       machine.id AS machine,
       machine.label AS machine_name,
       alarm.id AS alarm,
       COALESCE(json_extract(alarm.props, '$.text'), alarm.label) AS alarm_message,
       cause.id AS cause_id,
       cause.label AS cause_name,
       json_extract(n.props, '$.symptom_text') AS symptom,
       {_FIRST_DOC.format(kind="node", id="n.id")} AS doc_id
  FROM nodes AS n
  LEFT JOIN nodes AS machine ON machine.id = {_ONE_TARGET.format(rel="OCCURRED_ON")}
  LEFT JOIN nodes AS alarm ON alarm.id = {_ONE_TARGET.format(rel="TRIGGERED_BY")}
  LEFT JOIN nodes AS cause ON cause.id = {_ONE_TARGET.format(rel="ROOT_CAUSE")}
 WHERE n.type = 'Incident'
 ORDER BY date DESC, n.id DESC
 LIMIT ?
"""

ALARMS_SQL = f"""
SELECT machine.id AS machine,
       machine.label AS machine_name,
       alarm.id AS alarm,
       COALESCE(json_extract(alarm.props, '$.text'), alarm.label) AS alarm_message,
       json_extract(alarm.props, '$.severity') AS severity,
       json_extract(e.props, '$.last_seen') AS last_seen,
       json_extract(e.props, '$.count') AS count,
       {_FIRST_DOC.format(kind="edge", id="e.id")} AS doc_id
  FROM edges AS e
  JOIN nodes AS machine ON machine.id = e.src
  JOIN nodes AS alarm ON alarm.id = e.dst
 WHERE e.rel = 'RAISED'
 ORDER BY last_seen DESC, e.id
 LIMIT ?
"""


def activity(kg_path: Path, limit: int, alarm_limit: int) -> dict[str, list]:
    """Return the latest breakdowns and the latest alarms on the line.

    Each row names the document behind it, so the client can open the source."""
    with closing(connect(kg_path)) as conn:
        breakdowns = conn.execute(BREAKDOWNS_SQL, (limit,)).fetchall()
        alarms = conn.execute(ALARMS_SQL, (alarm_limit,)).fetchall()
    return {
        "breakdowns": [dict(row) for row in breakdowns],
        "alarms": [dict(row) for row in alarms],
    }


# --- the sources -------------------------------------------------------------------


def _file_of(raw_dir: Path, doc_path: str) -> Path | None:
    """Find the raw file for a document, or None if it is missing or outside the folder."""
    root = Path(raw_dir).resolve()
    path = (root / doc_path).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def list_sources(kg_path: Path, raw_dir: Path) -> list[dict[str, Any]]:
    """List every document with its type and file size.

    A document uploaded through the API also shows when it was added."""
    with closing(connect(kg_path)) as conn:
        rows = conn.execute(
            "SELECT id, path, doc_type FROM documents ORDER BY path"
        ).fetchall()
    added = uploaded.added_times(kg_path)
    found = []
    for row in rows:
        path = _file_of(raw_dir, row["path"])
        item = {
            "id": row["id"],
            "path": row["path"],
            "doc_type": row["doc_type"],
            "bytes": path.stat().st_size if path else None,
        }
        if row["id"] in added:
            item["added"] = added[row["id"]]
        found.append(item)
    return found


def _read(path: Path) -> dict[str, Any]:
    """Read a raw file as text, plus its pages or sheets and styles where they apply."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages, text, start = [], "", 0
        for number, page_text in manual_chunks.read_pages(path).items():
            if pages:
                text += "\n\n"
                start = len(text)
            pages.append({"page": number, "text": page_text, "start": start})
            text += page_text
        return {"kind": "pdf", "text": text, "pages": pages}
    if suffix in WORKBOOKS:
        sheets = tables.read_sheets(path)
        return {
            "kind": "workbook",
            "text": "\n".join(row.text for rows in sheets.values() for row in rows),
            "sheets": [
                {
                    "name": name,
                    "rows": [[tables.cell_text(c) for c in row.cells] for row in rows],
                }
                for name, rows in sheets.items()
            ],
            "styles": sheet_styles.read_styles(path),
        }
    return {
        "kind": "text",
        "text": path.read_text(encoding="utf-8-sig", errors="replace"),
    }


def _fact(
    conn: sqlite3.Connection, kind: str, fact_id: str
) -> tuple[str | None, str | None]:
    """Return the type and label of the fact that a piece of evidence supports."""
    if kind == "node":
        row = conn.execute(
            "SELECT type, label FROM nodes WHERE id = ?", (fact_id,)
        ).fetchone()
        return (row["type"], row["label"]) if row else (None, None)
    row = conn.execute(
        "SELECT src, rel, dst FROM edges WHERE id = ?", (fact_id,)
    ).fetchone()
    if row is None:
        return None, None
    relation = RELATIONS.get(row["rel"])
    verb = relation.label if relation else row["rel"].lower().replace("_", " ")
    return row["rel"], f"{row['src']} {verb} {row['dst']}"


def _locate(content: dict[str, Any], quote: str, page: int | None):
    """Find where a quote sits in the text. Return empty positions if it is not found."""
    text = content["text"]
    for entry in content.get("pages", []):
        if entry["page"] == page:
            at = entry["text"].find(quote)
            if at >= 0:
                start = entry["start"] + at
                return start, start + len(quote), page
    at = text.find(quote)
    if at < 0:
        return None, None, page
    found = page
    for entry in content.get("pages", []):
        if entry["start"] <= at:
            found = entry["page"]
    return at, at + len(quote), found


def _document(conn: sqlite3.Connection, raw_dir: Path, doc_id: str):
    """Return a document and its file. Raise NotFound if either is missing."""
    doc = conn.execute(
        "SELECT id, path, doc_type FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if doc is None:
        raise NotFound(f"there is no document {doc_id}")
    path = _file_of(raw_dir, doc["path"])
    if path is None:
        raise NotFound(f"the file {doc['path']} is not in the raw folder")
    return doc, path


def source_file(kg_path: Path, raw_dir: Path, doc_id: str) -> tuple[Path, str]:
    """Return the raw file of a document and its media type, to serve as it is."""
    if not DOC_ID.fullmatch(doc_id):
        raise NotFound(f"there is no document {doc_id}")
    with closing(connect(kg_path)) as conn:
        _, path = _document(conn, raw_dir, doc_id)
    return path, MEDIA_TYPES.get(path.suffix.lower(), OCTET_STREAM)


def get_source(kg_path: Path, raw_dir: Path, doc_id: str) -> dict[str, Any]:
    """Return one document with its content and the places where facts cite it."""
    with closing(connect(kg_path)) as conn:
        doc, path = _document(conn, raw_dir, doc_id)
        content = _read(path)
        spans = []
        evidence = conn.execute(
            "SELECT fact_kind, fact_id, page, quote, extractor, confidence"
            " FROM evidence WHERE doc_id = ? ORDER BY id",
            (doc_id,),
        ).fetchall()
        for row in evidence:
            start, end, page = _locate(content, row["quote"], row["page"])
            fact_type, label = _fact(conn, row["fact_kind"], row["fact_id"])
            spans.append(
                {
                    "quote": row["quote"],
                    "start": start,
                    "end": end,
                    "page": page,
                    "fact_kind": row["fact_kind"],
                    "fact_id": row["fact_id"],
                    "type": fact_type,
                    "label": label,
                    "extractor": row["extractor"],
                    "confidence": row["confidence"],
                }
            )
    spans.sort(key=lambda s: (s["start"] is None, s["start"] or 0, s["fact_id"]))
    return {
        "id": doc["id"],
        "path": doc["path"],
        "doc_type": doc["doc_type"],
        **content,
        "spans": spans,
    }


# --- the pipeline and the evals ----------------------------------------------------


def load_json(path: Path, what: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotFound(f"there is no {what} yet") from None


def pipeline_latest(kg_path: Path, report_path: Path) -> dict[str, Any]:
    """Return the report of the last full run and the open review items."""
    report = load_json(report_path, "pipeline report; run a full ingest first")
    with closing(connect(kg_path)) as conn:
        rows = conn.execute(
            "SELECT id, kind, payload, confidence FROM review_queue"
            " WHERE status = 'open' ORDER BY id"
        ).fetchall()
    queue = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "confidence": row["confidence"],
        }
        for row in rows
    ]
    return {**report, "review_queue": queue}


def evals_latest(path: Path) -> Any:
    """Return the latest evaluation results."""
    return load_json(path, "eval results; run the evals first")


def fact_map(path: Path) -> Any:
    """Return the fact map that `make map` builds."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise NotFound("The fact map has not been built. Run make map.") from None
