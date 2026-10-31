"""Support code for the eval tests: small hand-built answer keys and graphs.

write_truth makes a folder shaped like the real answer key, and write_kg makes a graph
database with the same tables as the real one. The node and edge builders keep the tests
short. Nothing here reads the real answer key or the real graph.
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from app.evals.truth import Edge, Node


def node(id: str, type: str, **props: Any) -> Node:
    return Node(id=id, type=type, props=props, label=str(props.get("name", id)))


def edge(src: str, rel: str, dst: str, **props: Any) -> Edge:
    return Edge(src=src, rel=rel, dst=dst, props=props)


def world_json(
    nodes: list[Node], edges: list[Edge], incidents_detail=(), technicians=()
) -> dict[str, Any]:
    return {
        "alarm_events": [],
        "edges": [
            {"src": e.src, "rel": e.rel, "dst": e.dst, "props": e.props} for e in edges
        ],
        "incidents_detail": list(incidents_detail),
        "nodes": [{"id": n.id, "type": n.type, "props": n.props} for n in nodes],
        "seed": 7,
        "stock_old": {},
        "technicians": list(technicians),
    }


def write_truth(
    folder: Path,
    nodes: list[Node],
    edges: list[Edge],
    mentions: dict[str, dict[str, Any]] | None = None,
    incidents_detail=(),
    technicians=(),
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "world.json").write_text(
        json.dumps(world_json(nodes, edges, incidents_detail, technicians))
    )
    (folder / "mentions.json").write_text(json.dumps(mentions or {}))
    return folder


KG_SCHEMA = """
CREATE TABLE documents(id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL,
    doc_type TEXT NOT NULL, sha256 TEXT NOT NULL);
CREATE TABLE nodes(id TEXT PRIMARY KEY, type TEXT NOT NULL, label TEXT NOT NULL,
    props TEXT NOT NULL);
CREATE TABLE edges(id TEXT PRIMARY KEY, src TEXT NOT NULL, rel TEXT NOT NULL,
    dst TEXT NOT NULL, props TEXT NOT NULL);
CREATE TABLE evidence(id INTEGER PRIMARY KEY, fact_kind TEXT NOT NULL,
    fact_id TEXT NOT NULL, doc_id TEXT NOT NULL, chunk_id TEXT, page INTEGER,
    quote TEXT NOT NULL, extractor TEXT NOT NULL, confidence REAL NOT NULL);
"""


def write_kg(
    path: Path,
    nodes: list[Node],
    edges: list[Edge],
    documents: dict[str, str] | None = None,
    node_evidence: dict[str, list[tuple[str, str]]] | None = None,
    edge_evidence: dict[tuple[str, str, str], list[tuple[str, str, float]]]
    | None = None,
) -> Path:
    """Write a graph database from documents, node evidence and edge evidence."""
    documents = dict(documents or {})
    node_evidence = node_evidence or {}
    edge_evidence = edge_evidence or {}
    for items in [*node_evidence.values(), *edge_evidence.values()]:
        for item in items:
            documents.setdefault(item[0], "log")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(KG_SCHEMA)
        for number, (doc, kind) in enumerate(sorted(documents.items())):
            conn.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?)",
                (f"DOC-{number}", doc, kind, "x"),
            )
        doc_id = {doc: f"DOC-{n}" for n, doc in enumerate(sorted(documents))}
        for n in nodes:
            conn.execute(
                "INSERT INTO nodes VALUES (?, ?, ?, ?)",
                (n.id, n.type, n.label, json.dumps(n.props)),
            )
        for e in edges:
            conn.execute(
                "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
                (f"{e.src}|{e.rel}|{e.dst}", e.src, e.rel, e.dst, json.dumps(e.props)),
            )
        for fact_id, items in node_evidence.items():
            for doc, quote in items:
                conn.execute(
                    "INSERT INTO evidence(fact_kind, fact_id, doc_id, quote,"
                    " extractor, confidence) VALUES ('node', ?, ?, ?, 'csv-parser', 0.9)",
                    (fact_id, doc_id[doc], quote),
                )
        for (src, rel, dst), items in edge_evidence.items():
            for doc, quote, confidence in items:
                conn.execute(
                    "INSERT INTO evidence(fact_kind, fact_id, doc_id, quote,"
                    " extractor, confidence) VALUES ('edge', ?, ?, ?, 'csv-parser', ?)",
                    (f"{src}|{rel}|{dst}", doc_id[doc], quote, confidence),
                )
    return path


# --- in-memory truth and graph ---

from app.evals.truth import (  # noqa: E402
    DocMentions,
    Graph,
    IncidentDetail,
    Technician,
    Truth,
)


def mentions(
    assets=(),
    alarm_codes=(),
    parts=(),
    incidents=(),
    failure_modes=None,
    procedures=None,
) -> DocMentions:
    return DocMentions(
        tuple(assets),
        tuple(alarm_codes),
        tuple(parts),
        tuple(incidents),
        None if failure_modes is None else tuple(failure_modes),
        None if procedures is None else tuple(procedures),
    )


def detail(id: str, start: str, technician: str = "Nur Aisyah") -> IncidentDetail:
    return IncidentDetail(id, start, "PROC-X", (), technician)


def make_truth(
    nodes: list[Node],
    edges: list[Edge],
    *,
    details: list[IncidentDetail] = (),
    technicians: list[Technician] = (),
    doc_mentions: dict[str, DocMentions] | None = None,
) -> Truth:
    return Truth(
        nodes={n.id: n for n in nodes},
        edges=tuple(edges),
        incident_details={d.id: d for d in details},
        technicians=tuple(technicians),
        mentions=dict(doc_mentions or {}),
    )


def make_graph(
    nodes: list[Node],
    edges: list[Edge],
    *,
    documents: dict[str, str] | None = None,
    node_evidence=None,
    edge_evidence=None,
) -> Graph:
    from app.evals.truth import Evidence

    def evidence(items):
        return tuple(
            Evidence(doc, quote, (extra or ["csv-parser"])[0], confidence)
            for doc, quote, confidence, *extra in items
        )

    return Graph(
        nodes={n.id: n for n in nodes},
        edges=tuple(edges),
        documents=dict(documents or {}),
        node_evidence={
            k: evidence((doc, quote, 0.9) for doc, quote in v)
            for k, v in (node_evidence or {}).items()
        },
        edge_evidence={k: evidence(v) for k, v in (edge_evidence or {}).items()},
    )


# --- incidents ---

TECHNICIANS = [
    Technician("Chen Jun Hao", "C", ("Jun Hao", "JHC")),
    Technician("Nur Aisyah", "B", ("Nur", "NA")),
]


def incident_truth() -> Truth:
    """Five incidents, plus document entries for an export and two handovers."""
    incidents = [
        node("T-1", "Incident", date="2026-04-14", downtime_min=100),
        node("T-2", "Incident", date="2026-04-14", downtime_min=300),
        node("T-3", "Incident", date="2026-04-15", downtime_min=200),
        node("T-4", "Incident", date="2026-04-14", downtime_min=100),
        node("T-5", "Incident", date="2026-04-14", downtime_min=100),
    ]
    edges = [
        edge("T-1", "OCCURRED_ON", "HP-01"),
        edge("T-2", "OCCURRED_ON", "HP-01"),
        edge("T-3", "OCCURRED_ON", "HP-01"),
        edge("T-4", "OCCURRED_ON", "CNC-01"),
        edge("T-5", "OCCURRED_ON", "CNC-01"),
    ]
    return make_truth(
        incidents,
        edges,
        details=[
            detail("T-1", "2026-04-14T09:00:00+08:00", "Chen Jun Hao"),
            detail("T-2", "2026-04-14T15:00:00+08:00", "Nur Aisyah"),
            detail("T-3", "2026-04-15T09:00:00+08:00", "Nur Aisyah"),
            detail("T-4", "2026-04-14T09:00:00+08:00", "Chen Jun Hao"),
            detail("T-5", "2026-04-14T15:00:00+08:00", "Chen Jun Hao"),
        ],
        technicians=TECHNICIANS,
        doc_mentions={
            "maintenance/export.csv": mentions(
                incidents=["T-1", "T-2", "T-3", "T-4", "T-5"]
            ),
            "handovers/one.txt": mentions(incidents=["T-3"]),
            "handovers/zero.txt": mentions(),
        },
    )


def record(doc, key="r", **fields):
    from app.evals.labels import Record

    base = {"date": None, "machine": None, "technician": None, "downtime_min": None}
    return Record(doc, key, **{**base, **fields})
