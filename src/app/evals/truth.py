"""Loads the hidden truth and the graph under test as typed objects.

load_truth() reads the answer key in data/truth. Only the eval code may read it. The
document paths are cleaned so they match the paths in the graph. load_graph() reads the
built graph database in read-only mode, with its documents and the evidence behind each
node and edge. Both sides use the same Node and Edge shapes, so the matcher and the
scoring treat them alike.
"""

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import KG_PATH, TRUTH_DIR

RAW_PREFIX = "raw/"


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    props: dict[str, Any]
    label: str = ""


@dataclass(frozen=True)
class Edge:
    src: str
    rel: str
    dst: str
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.src, self.rel, self.dst)


@dataclass(frozen=True)
class Evidence:
    """One quote from a raw file that backs a node or an edge of the graph."""

    doc_path: str
    quote: str
    extractor: str
    confidence: float


@dataclass(frozen=True)
class Technician:
    name: str
    shift: str
    short_forms: tuple[str, ...]


@dataclass(frozen=True)
class IncidentDetail:
    """One incident as the world records it. The start is local time (+08:00)."""

    id: str
    start: str
    procedure_id: str
    parts: tuple[tuple[str, int], ...]
    technician: str


@dataclass(frozen=True)
class DocMentions:
    """What one raw file really mentions. Failure modes and procedures are given only
    for manuals and are None for other files."""

    assets: tuple[str, ...]
    alarm_codes: tuple[str, ...]
    parts: tuple[str, ...]
    incidents: tuple[str, ...]
    failure_modes: tuple[str, ...] | None = None
    procedures: tuple[str, ...] | None = None


class _Typed:
    """Lookups that the truth and the graph share."""

    nodes: dict[str, Node]
    edges: tuple[Edge, ...]

    def of_type(self, node_type: str) -> dict[str, Node]:
        return {i: n for i, n in self.nodes.items() if n.type == node_type}

    def targets(self, src: str, rel: str) -> list[str]:
        return [e.dst for e in self.edges if e.src == src and e.rel == rel]

    def machine_of(self, incident_id: str) -> str | None:
        return next(iter(self.targets(incident_id, "OCCURRED_ON")), None)

    def root_cause_of(self, incident_id: str) -> str | None:
        return next(iter(self.targets(incident_id, "ROOT_CAUSE")), None)


@dataclass
class Truth(_Typed):
    nodes: dict[str, Node]
    edges: tuple[Edge, ...]
    incident_details: dict[str, IncidentDetail]
    technicians: tuple[Technician, ...]
    mentions: dict[str, DocMentions]


@dataclass
class Graph(_Typed):
    nodes: dict[str, Node]
    edges: tuple[Edge, ...]
    documents: dict[str, str]
    node_evidence: dict[str, tuple[Evidence, ...]]
    edge_evidence: dict[tuple[str, str, str], tuple[Evidence, ...]]


def _mentions(entry: dict[str, Any]) -> DocMentions:
    def listed(name: str) -> tuple[str, ...] | None:
        return tuple(entry[name]) if name in entry else None

    return DocMentions(
        assets=tuple(entry.get("assets", ())),
        alarm_codes=tuple(entry.get("alarm_codes", ())),
        parts=tuple(entry.get("parts", ())),
        incidents=tuple(entry.get("incidents", ())),
        failure_modes=listed("failure_modes"),
        procedures=listed("procedures"),
    )


def load_truth(folder: Path | str = TRUTH_DIR) -> Truth:
    """The truth from a folder laid out like data/truth."""
    folder = Path(folder)
    world = json.loads((folder / "world.json").read_text(encoding="utf-8"))
    mentions = json.loads((folder / "mentions.json").read_text(encoding="utf-8"))
    nodes = {
        n["id"]: Node(
            n["id"], n["type"], n["props"], str(n["props"].get("name", n["id"]))
        )
        for n in world["nodes"]
    }
    edges = tuple(
        Edge(e["src"], e["rel"], e["dst"], e.get("props", {})) for e in world["edges"]
    )
    details = {
        d["id"]: IncidentDetail(
            d["id"],
            d["start"],
            d["procedure_id"],
            tuple((p["part_no"], p["qty"]) for p in d["parts"]),
            d["technician"],
        )
        for d in world["incidents_detail"]
    }
    technicians = tuple(
        Technician(t["name"], t["shift"], tuple(t["short_forms"]))
        for t in world["technicians"]
    )
    return Truth(
        nodes=nodes,
        edges=edges,
        incident_details=details,
        technicians=technicians,
        mentions={
            key.removeprefix(RAW_PREFIX): _mentions(entry)
            for key, entry in mentions.items()
        },
    )


def load_graph(path: Path | str = KG_PATH) -> Graph:
    """The graph database at path, opened read-only. A missing file raises an error and
    is not created."""
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        documents = {
            row[0]: row[1]
            for row in conn.execute("SELECT path, doc_type FROM documents")
        }
        nodes = {
            row[0]: Node(row[0], row[1], json.loads(row[3]), row[2])
            for row in conn.execute(
                "SELECT id, type, label, props FROM nodes ORDER BY id"
            )
        }
        edges = tuple(
            Edge(row[0], row[1], row[2], json.loads(row[3]))
            for row in conn.execute(
                "SELECT src, rel, dst, props FROM edges ORDER BY src, rel, dst"
            )
        )
        node_evidence: dict[str, list[Evidence]] = defaultdict(list)
        edge_evidence: dict[tuple[str, str, str], list[Evidence]] = defaultdict(list)
        rows = conn.execute(
            "SELECT e.fact_kind, e.fact_id, d.path, e.quote, e.extractor, e.confidence"
            " FROM evidence e JOIN documents d ON d.id = e.doc_id ORDER BY e.id"
        )
        for kind, fact_id, doc_path, quote, extractor, confidence in rows:
            item = Evidence(doc_path, quote, extractor, confidence)
            if kind == "node":
                node_evidence[fact_id].append(item)
            else:
                src, rel, dst = fact_id.split("|")
                edge_evidence[src, rel, dst].append(item)
    return Graph(
        nodes=nodes,
        edges=edges,
        documents=documents,
        node_evidence={k: tuple(v) for k, v in node_evidence.items()},
        edge_evidence={k: tuple(v) for k, v in edge_evidence.items()},
    )
