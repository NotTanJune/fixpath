"""The knowledge graph database.

This file creates and fills the database. It holds the source documents and their text
chunks, the graph nodes and links, the evidence behind each fact and a review queue.
Every write is checked against the ontology.

`upsert_node`, `upsert_edge` and `add_evidence` write the graph. `neighbors` and
`load_networkx` read it. `keyword_search` and `vector_search` find text chunks.
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
from pydantic import BaseModel, ValidationError

from app.config import KG_PATH
from app.embed import DIMENSIONS
from app.ontology import NODE_TYPES, Evidence, is_allowed, props_model, validate_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
    id TEXT PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    doc_type TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks(
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(id),
    page INTEGER,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS nodes(
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    label TEXT NOT NULL,
    props TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edges(
    id TEXT PRIMARY KEY,
    src TEXT NOT NULL REFERENCES nodes(id),
    rel TEXT NOT NULL,
    dst TEXT NOT NULL REFERENCES nodes(id),
    props TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence(
    id INTEGER PRIMARY KEY,
    fact_kind TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    chunk_id TEXT,
    page INTEGER,
    quote TEXT NOT NULL,
    extractor TEXT NOT NULL,
    confidence REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'open'
);
"""

DIRECTIONS = ("out", "in", "both")


# --- connection and schema ----------------------------------------------------


def connect(path: Path | str = KG_PATH) -> sqlite3.Connection:
    """Open the database, creating its folder if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create any missing tables. This is safe on a database that already has data."""
    conn.executescript(SCHEMA)


# --- JSON helpers -------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Turn dates, models and sets into values that JSON can hold."""
    if isinstance(value, date):  # datetime is a date too
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_jsonable, sort_keys=True, ensure_ascii=False)


# --- documents and chunks -----------------------------------------------------


def upsert_document(
    conn: sqlite3.Connection, id: str, path: str, doc_type: str, sha256: str
) -> None:
    """Add a document, or update it if the ID exists. A path used by another document is an error."""
    with conn:
        conn.execute(
            "INSERT INTO documents(id, path, doc_type, sha256) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET path = excluded.path,"
            " doc_type = excluded.doc_type, sha256 = excluded.sha256",
            (id, path, doc_type, sha256),
        )


def _embedding_blob(embedding: Any) -> bytes:
    """Turn a vector into bytes, and reject one of the wrong size right away."""
    if isinstance(embedding, bytes):
        blob = embedding
    else:
        blob = np.asarray(embedding, dtype=np.float32).reshape(-1).tobytes()
    if len(blob) != DIMENSIONS * 4:
        raise ValueError(
            f"an embedding needs {DIMENSIONS} float32 values ({DIMENSIONS * 4} "
            f"bytes); got {len(blob)} bytes"
        )
    return blob


def add_chunk(
    conn: sqlite3.Connection,
    chunk_id: str,
    doc_id: str,
    page: int | None,
    ord: int,
    text: str,
    embedding: np.ndarray | Sequence[float] | bytes | None = None,
) -> None:
    """Save a chunk of text and add it to the search index. An existing chunk is replaced.

    A wrong-size embedding is rejected before anything is written. The document must exist."""
    blob = None if embedding is None else _embedding_blob(embedding)
    exists = conn.execute("SELECT 1 FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO chunks(id, doc_id, page, ord, text, embedding)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, doc_id, page, ord, text, blob),
        )
        if exists:  # this is slow, so do it only when there is an old row to drop
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
        conn.execute(
            "INSERT INTO chunks_fts(chunk_id, text) VALUES (?, ?)", (chunk_id, text)
        )


# --- nodes and edges ----------------------------------------------------------


def _node(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "label": row["label"],
        "props": json.loads(row["props"]),
    }


def upsert_node(
    conn: sqlite3.Connection,
    id: str,
    type: str,
    label: str,
    props: dict[str, Any],
) -> None:
    """Add a node, or merge new values into the node with this ID.

    New values win over stored ones, but an empty value never replaces a stored one.
    The result must be valid for the node type. Raise ValueError for an unknown type,
    a bad ID, bad values, or a type that differs from the stored node."""
    if type not in NODE_TYPES:
        raise ValueError(f"unknown node type {type!r}")
    if not validate_id(type, id):
        raise ValueError(f"{id!r} is not a valid {type} id")
    row = conn.execute("SELECT type, props FROM nodes WHERE id = ?", (id,)).fetchone()
    if row is not None and row["type"] != type:
        raise ValueError(f"node {id!r} is already a {row['type']}, not a {type}")
    known = {key: value for key, value in (props or {}).items() if value is not None}
    merged = {**(json.loads(row["props"]) if row else {}), **known}
    try:
        parsed = props_model(type).model_validate(merged)
    except ValidationError as error:
        raise ValueError(f"invalid props for {type} {id!r}: {error}") from error
    with conn:
        conn.execute(
            "INSERT INTO nodes(id, type, label, props) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET label = excluded.label,"
            " props = excluded.props",
            (id, type, label, _dumps(parsed.model_dump(mode="json"))),
        )


def upsert_edge(
    conn: sqlite3.Connection,
    src: str,
    rel: str,
    dst: str,
    props: dict[str, Any] | None = None,
) -> str:
    """Add an edge, or merge new values into the existing one, and return its ID.

    The same merge rule applies as for nodes. Raise ValueError if an end is missing from
    the graph or the link is not allowed between those types."""
    types = {
        row["id"]: row["type"]
        for row in conn.execute(
            "SELECT id, type FROM nodes WHERE id IN (?, ?)", (src, dst)
        )
    }
    for node_id in (src, dst):
        if node_id not in types:
            raise ValueError(f"cannot add {rel}: node {node_id!r} is not in the graph")
    if not is_allowed(types[src], rel, types[dst]):
        raise ValueError(
            f"{rel} cannot run from {types[src]} {src!r} to {types[dst]} {dst!r}"
        )
    edge_id = f"{src}|{rel}|{dst}"
    row = conn.execute("SELECT props FROM edges WHERE id = ?", (edge_id,)).fetchone()
    known = {key: value for key, value in (props or {}).items() if value is not None}
    merged = {**(json.loads(row["props"]) if row else {}), **known}
    with conn:
        conn.execute(
            "INSERT INTO edges(id, src, rel, dst, props) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET props = excluded.props",
            (edge_id, src, rel, dst, _dumps(merged)),
        )
    return edge_id


def add_evidence(conn: sqlite3.Connection, evidence: Evidence) -> int:
    """Record where a fact came from and return the new row number."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO evidence(fact_kind, fact_id, doc_id, chunk_id, page, quote,"
            " extractor, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evidence.fact_kind,
                evidence.fact_id,
                evidence.doc_id,
                evidence.chunk_id,
                evidence.page,
                evidence.quote,
                evidence.extractor,
                evidence.confidence,
            ),
        )
    return cursor.lastrowid


def queue_review(
    conn: sqlite3.Connection, kind: str, payload: Any, confidence: float | None = None
) -> int:
    """Add an open item for a person to review and return its row number."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO review_queue(kind, payload, confidence) VALUES (?, ?, ?)",
            (kind, _dumps(payload), confidence),
        )
    return cursor.lastrowid


# --- graph reads --------------------------------------------------------------


def get_node(conn: sqlite3.Connection, id: str) -> dict[str, Any] | None:
    """Return the node, or None if there is no such node."""
    row = conn.execute(
        "SELECT id, type, label, props FROM nodes WHERE id = ?", (id,)
    ).fetchone()
    return None if row is None else _node(row)


def neighbors(
    conn: sqlite3.Connection,
    node_id: str,
    rel: str | None = None,
    direction: str = "out",
) -> list[dict[str, Any]]:
    """List the nodes linked to a node, with the link that joins each.

    The direction can be outgoing, incoming or both. A relation can narrow the list."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}; got {direction!r}")
    found = []
    for side in ("out", "in") if direction == "both" else (direction,):
        near, far = ("src", "dst") if side == "out" else ("dst", "src")
        query = (
            "SELECT e.id AS edge_id, e.rel, e.props AS edge_props,"
            " n.id, n.type, n.label, n.props"
            f" FROM edges e JOIN nodes n ON n.id = e.{far} WHERE e.{near} = ?"
        )
        params: list[Any] = [node_id]
        if rel is not None:
            query += " AND e.rel = ?"
            params.append(rel)
        for row in conn.execute(query + " ORDER BY e.rel, n.id", params):
            found.append(
                {
                    **_node(row),
                    "edge_id": row["edge_id"],
                    "rel": row["rel"],
                    "direction": side,
                    "edge_props": json.loads(row["edge_props"]),
                }
            )
    return found


def load_networkx(conn: sqlite3.Connection) -> nx.MultiDiGraph:
    """Load the whole graph into memory for graph algorithms."""
    graph = nx.MultiDiGraph()
    for row in conn.execute("SELECT id, type, label, props FROM nodes ORDER BY id"):
        graph.add_node(
            row["id"],
            type=row["type"],
            label=row["label"],
            props=json.loads(row["props"]),
        )
    for row in conn.execute("SELECT id, src, rel, dst, props FROM edges ORDER BY id"):
        graph.add_edge(
            row["src"],
            row["dst"],
            key=row["id"],
            rel=row["rel"],
            props=json.loads(row["props"]),
        )
    return graph


# --- search -------------------------------------------------------------------


def _fts_query(text: str) -> str | None:
    """Turn user text into a search query that can never be a syntax error.

    Every word is treated as plain text. Chunks that match more words rank higher.
    Return None if the text has no letters or digits."""
    text = text.encode("utf-8", "ignore").decode("utf-8").replace("\x00", " ")
    phrases = [
        '"' + piece.replace('"', '""') + '"'
        for piece in text.split()
        if re.search(r"[^\W_]", piece)
    ]
    return " OR ".join(phrases) or None


def keyword_search(conn: sqlite3.Connection, query: str, k: int) -> list[dict]:
    """Return the k best chunks for the words in the query, best first."""
    match = _fts_query(query)
    if match is None or k <= 0:
        return []
    rows = conn.execute(
        "SELECT c.id AS chunk_id, c.doc_id, c.page, c.ord, c.text,"
        " bm25(chunks_fts) AS score"
        " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.chunk_id"
        " WHERE chunks_fts MATCH ? ORDER BY score, c.id LIMIT ?",
        (match, k),
    )
    # The search gives lower numbers for better matches, so flip the sign.
    return [{**dict(row), "score": -row["score"]} for row in rows]


def vector_search(
    conn: sqlite3.Connection, query_vec: np.ndarray | Sequence[float], k: int
) -> list[dict]:
    """Return the k chunks whose meaning is closest to the query vector, best first.

    Raise ValueError if the vector sizes do not match."""
    rows = conn.execute(
        "SELECT id AS chunk_id, doc_id, page, ord, text, embedding FROM chunks"
        " WHERE embedding IS NOT NULL ORDER BY id"
    ).fetchall()
    if not rows or k <= 0:
        return []
    query = np.asarray(query_vec, dtype=np.float64).reshape(-1)
    sizes = {len(row["embedding"]) // 4 for row in rows}
    if sizes != {query.size}:
        raise ValueError(
            f"query has {query.size} dimensions but the stored embeddings have "
            f"{', '.join(map(str, sorted(sizes)))}"
        )
    matrix = np.vstack(
        [np.frombuffer(row["embedding"], dtype=np.float32) for row in rows]
    ).astype(np.float64)
    norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
    scores = np.divide(matrix @ query, norms, out=np.zeros(len(rows)), where=norms > 0)
    best = np.argsort(-scores, kind="stable")[:k]
    return [
        {
            "chunk_id": rows[i]["chunk_id"],
            "doc_id": rows[i]["doc_id"],
            "page": rows[i]["page"],
            "ord": rows[i]["ord"],
            "text": rows[i]["text"],
            "score": float(scores[i]),
        }
        for i in best
    ]
