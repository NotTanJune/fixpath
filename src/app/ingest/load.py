"""Writes the checked facts into the graph database.

load_full builds a new database beside the old one and swaps it in only when it is
complete, so a failure leaves the old database untouched. load_file adds the results
of one file to the existing database and deletes nothing. If it stops part way,
running it again finishes the job without creating duplicates.

registry_from_store reads the stored facts back, so a single new file can be matched
against what the graph already holds.
"""

import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app import store
from app.config import RAW_DIR
from app.embed import Embedder
from app.ingest import router
from app.ingest.normalize import document_id, doc_path_for
from app.ingest.resolve import Registry
from app.ingest.types import (
    EvidenceDraft,
    Extraction,
    ResolvedEdge,
    ResolvedNode,
    ReviewItem,
)
from app.ingest.validate import Validated, edge_subject
from app.ontology import Evidence

# How many text chunks are embedded at once.
BATCH_SIZE = 64


class PassageEmbedder(Protocol):
    """What the loader needs from an embedder: a way to turn texts into vectors."""

    def embed_passages(self, texts: Iterable[str]) -> np.ndarray: ...


def raw_files(raw_dir: Path) -> list[Path]:
    """Every file in the raw folder in path order, skipping hidden files and folders."""
    raw_dir = Path(raw_dir)
    return sorted(
        path
        for path in raw_dir.rglob("*")
        if path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(raw_dir).parts)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class _Writer:
    """Does the writing for one load into one open database."""

    def __init__(self, conn: sqlite3.Connection, embedder: PassageEmbedder) -> None:
        self.conn = conn
        self.embedder = embedder
        self._chunks: dict[str, list[tuple[str, int | None, str]]] = {}
        self.counts = dict.fromkeys(
            ("documents", "chunks", "nodes", "edges", "evidence", "review"), 0
        )

    def add_documents(
        self, paths: Sequence[Path], raw_dir: Path, extractions: list[Extraction]
    ) -> None:
        types = {e.doc_path: e.doc_type for e in extractions}
        for path in paths:
            doc_path = doc_path_for(path, raw_dir)
            store.upsert_document(
                self.conn,
                document_id(doc_path),
                doc_path,
                types.get(doc_path) or router.classify(path),
                _sha256(path),
            )
            self.counts["documents"] += 1

    def doc_id(self, doc_path: str) -> str:
        row = self.conn.execute(
            "SELECT id FROM documents WHERE path = ?", (doc_path,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"{doc_path!r} is not a file that was loaded as a document"
            )
        return row["id"]

    def check_evidence(
        self, validated: Validated, paths: Sequence[Path], raw_dir: Path
    ) -> None:
        """Stop before writing anything if some evidence points at a file that is not
        stored and is not part of this run."""
        known = {row["path"] for row in self.conn.execute("SELECT path FROM documents")}
        known |= {doc_path_for(path, raw_dir) for path in paths}
        cited = {
            item.doc_path
            for fact in (*validated.nodes, *validated.edges)
            for item in fact.evidence
        }
        missing = sorted(cited - known)
        if missing:
            raise ValueError(
                "evidence cites files that are not loaded as documents: "
                + ", ".join(missing)
            )

    def add_chunks(self, extractions: list[Extraction]) -> None:
        chunks = [chunk for extraction in extractions for chunk in extraction.chunks]
        for start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[start : start + BATCH_SIZE]
            doc_ids = [self.doc_id(chunk.doc_path) for chunk in batch]
            vectors = self.embedder.embed_passages([chunk.text for chunk in batch])
            if len(vectors) != len(batch):
                raise ValueError(
                    f"the embedder gave {len(vectors)} vectors for {len(batch)} chunks"
                )
            for chunk, doc_id, vector in zip(batch, doc_ids, vectors, strict=True):
                store.add_chunk(
                    self.conn,
                    chunk.id,
                    doc_id,
                    chunk.page,
                    chunk.ord,
                    chunk.text,
                    vector,
                )
                self.counts["chunks"] += 1

    def add_graph(self, validated: Validated) -> None:
        for node in validated.nodes:
            store.upsert_node(self.conn, node.id, node.type, node.label, node.props)
            self.counts["nodes"] += 1
        for edge in validated.edges:
            store.upsert_edge(self.conn, edge.src, edge.rel, edge.dst, edge.props)
            self.counts["edges"] += 1
        for node in validated.nodes:
            self.add_evidence("node", node.id, node.evidence)
        for edge in validated.edges:
            self.add_evidence("edge", edge_subject(edge), edge.evidence)

    def chunks_of(self, doc_id: str) -> list[tuple[str, int | None, str]]:
        if doc_id not in self._chunks:
            rows = self.conn.execute(
                "SELECT id, page, text FROM chunks WHERE doc_id = ?"
                " ORDER BY COALESCE(page, 0), ord, id",
                (doc_id,),
            )
            self._chunks[doc_id] = [(r["id"], r["page"], r["text"]) for r in rows]
        return self._chunks[doc_id]

    def chunk_for(self, doc_id: str, item: EvidenceDraft) -> str | None:
        """The first chunk of the document that contains the quote, preferring the page
        the evidence names."""
        found = [c for c in self.chunks_of(doc_id) if item.quote in c[2]]
        if item.page is not None:
            on_page = [c for c in found if c[1] == item.page]
            found = on_page or found
        return found[0][0] if found else None

    def add_evidence(
        self, kind: str, fact_id: str, items: Iterable[EvidenceDraft]
    ) -> None:
        for item in items:
            doc_id = self.doc_id(item.doc_path)
            seen = self.conn.execute(
                "SELECT 1 FROM evidence WHERE fact_kind = ? AND fact_id = ?"
                " AND doc_id = ? AND page IS ? AND quote = ? AND extractor = ?",
                (kind, fact_id, doc_id, item.page, item.quote, item.extractor),
            ).fetchone()
            if seen:
                continue
            store.add_evidence(
                self.conn,
                Evidence(
                    fact_kind=kind,
                    fact_id=fact_id,
                    doc_id=doc_id,
                    chunk_id=self.chunk_for(doc_id, item),
                    page=item.page,
                    quote=item.quote,
                    extractor=item.extractor,
                    confidence=item.confidence,
                ),
            )
            self.counts["evidence"] += 1

    def add_review(
        self, review: Iterable[ReviewItem], validated: Validated, skip_known: bool
    ) -> None:
        rows = [
            (
                item.kind,
                {"doc_paths": item.doc_paths, **item.payload},
                item.confidence,
            )
            for item in review
        ]
        rows += [
            ("validator", flag.model_dump(mode="json"), None)
            for flag in validated.flags
        ]
        known = self._known_review() if skip_known else []
        for kind, payload, confidence in rows:
            normal = json.loads(json.dumps(payload, default=str))
            if (kind, normal, confidence) in known:
                continue
            store.queue_review(self.conn, kind, payload, confidence)
            known.append((kind, normal, confidence))
            self.counts["review"] += 1

    def _known_review(self) -> list[tuple[str, Any, float | None]]:
        return [
            (r["kind"], json.loads(r["payload"]), r["confidence"])
            for r in self.conn.execute(
                "SELECT kind, payload, confidence FROM review_queue"
            )
        ]


def _cleanup(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def load_full(
    validated: Validated,
    extractions: list[Extraction],
    raw_dir: Path,
    kg_path: Path,
    *,
    review: Iterable[ReviewItem] = (),
    embedder: PassageEmbedder | None = None,
) -> dict[str, int]:
    """Build a new database and swap it in place of the old one when it is complete.

    Returns how many rows of each kind were written. A failure leaves the old database
    as it was.
    """
    raw_dir, kg_path = Path(raw_dir), Path(kg_path)
    embedder = embedder or Embedder()
    building = kg_path.with_name(f".{kg_path.name}.building")
    _cleanup(building)
    try:
        with closing(store.connect(building)) as conn:
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            store.init_schema(conn)
            writer = _Writer(conn, embedder)
            writer.add_documents(raw_files(raw_dir), raw_dir, extractions)
            writer.add_chunks(extractions)
            writer.add_graph(validated)
            writer.add_review(review, validated, skip_known=False)
        os.replace(building, kg_path)
    except BaseException:
        _cleanup(building)
        raise
    return writer.counts


def load_file(
    validated: Validated,
    extractions: list[Extraction],
    paths: Sequence[Path],
    kg_path: Path,
    *,
    raw_dir: Path = RAW_DIR,
    review: Iterable[ReviewItem] = (),
    embedder: PassageEmbedder | None = None,
) -> dict[str, int]:
    """Add the results of the given files to the existing database.

    Nothing is deleted, and evidence or review items already stored are not added
    again. Fails if there is no database, or if some evidence points at an unknown
    file, in which case nothing is written.
    """
    kg_path = Path(kg_path)
    if not kg_path.is_file():
        raise FileNotFoundError(
            f"no graph database at {kg_path}; run a full ingest first"
        )
    embedder = embedder or Embedder()
    with closing(store.connect(kg_path)) as conn:
        store.init_schema(conn)
        writer = _Writer(conn, embedder)
        writer.check_evidence(validated, paths, Path(raw_dir))
        writer.add_documents(paths, Path(raw_dir), extractions)
        writer.add_chunks(extractions)
        writer.add_graph(validated)
        writer.add_review(review, validated, skip_known=True)
    return writer.counts


def registry_from_store(kg_path: Path) -> Registry:
    """The stored nodes and edges with their evidence, used when ingesting one file."""
    kg_path = Path(kg_path)
    if not kg_path.is_file():
        raise FileNotFoundError(f"no graph database at {kg_path}")
    with closing(store.connect(kg_path)) as conn:
        evidence: dict[tuple[str, str], list[EvidenceDraft]] = defaultdict(list)
        for row in conn.execute(
            "SELECT e.fact_kind, e.fact_id, d.path, e.page, e.quote, e.extractor,"
            " e.confidence FROM evidence e JOIN documents d ON d.id = e.doc_id"
            " ORDER BY e.id"
        ):
            evidence[row["fact_kind"], row["fact_id"]].append(
                EvidenceDraft(
                    doc_path=row["path"],
                    page=row["page"],
                    quote=row["quote"],
                    extractor=row["extractor"],
                    confidence=row["confidence"],
                )
            )
        nodes = [
            ResolvedNode(
                id=row["id"],
                type=row["type"],
                label=row["label"],
                props=json.loads(row["props"]),
                evidence=evidence[("node", row["id"])],
                sources=sorted({e.doc_path for e in evidence[("node", row["id"])]}),
            )
            for row in conn.execute(
                "SELECT id, type, label, props FROM nodes ORDER BY id, type"
            )
        ]
        edges = [
            ResolvedEdge(
                src=row["src"],
                rel=row["rel"],
                dst=row["dst"],
                props=json.loads(row["props"]),
                evidence=evidence[("edge", row["id"])],
            )
            for row in conn.execute(
                "SELECT id, src, rel, dst, props FROM edges ORDER BY src, rel, dst"
            )
        ]
    return Registry(nodes=nodes, edges=edges)
