"""Stubs for the loader tests: a fake embedder that needs no model, a small raw folder,
and a small validated resolution that matches it.
"""

import hashlib
import re
from pathlib import Path

import numpy as np

from app.embed import DIMENSIONS
from app.ingest.normalize import chunk_id, document_id
from app.ingest.types import (
    Chunk,
    EvidenceDraft,
    Extraction,
    ResolvedEdge,
    ResolvedNode,
)
from app.ingest.validate import Validated

HANDOVER = "handovers/handover_2026-08-22_B.txt"
LOG = "logs/HP01.log"
NOTE = "maintenance/note_from_arun.txt"

HANDOVER_TEXT = (
    "Shift handover 22 Aug B\n"
    "HP1 stopped with E217, oil under the ram. Replaced the main seals.\n"
)
LOG_TEXT = "2026-08-22T01:00:00Z HP-01 ALARM E217 Oil pressure low\n"
NOTE_TEXT = "Arun: the CNC2 spindle bearing is noisy again.\n"


class FakeEmbedder:
    """A simple embedder that needs no model. Texts that share words land close together.
    It records the size of each batch."""

    def __init__(self, fail: bool = False):
        self.batches: list[int] = []
        self.fail = fail

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        vector = np.zeros(DIMENSIONS, dtype=np.float32)
        for word in re.findall(r"\w+", text.lower()):
            bucket = int(hashlib.sha256(word.encode()).hexdigest(), 16) % DIMENSIONS
            vector[bucket] += 1.0
        norm = np.linalg.norm(vector)
        return vector / norm if norm else vector

    def embed_passages(self, texts) -> np.ndarray:
        texts = list(texts)
        if self.fail:
            raise RuntimeError("the embedder is down")
        self.batches.append(len(texts))
        return np.vstack([self._vector(text) for text in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._vector(text)


def write_raw(raw: Path) -> Path:
    """A raw folder with a handover, a log, a loose note and a hidden file."""
    for path, text in ((HANDOVER, HANDOVER_TEXT), (LOG, LOG_TEXT), (NOTE, NOTE_TEXT)):
        target = raw / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (raw / "logs" / ".DS_Store").write_bytes(b"\x00junk")
    return raw


def ev(doc: str, quote: str, page: int | None = None) -> list[EvidenceDraft]:
    return [
        EvidenceDraft(
            doc_path=doc, page=page, quote=quote, extractor="sol-text", confidence=0.8
        )
    ]


def chunk(doc: str, ord: int, text: str, page: int | None = None) -> Chunk:
    return Chunk(
        id=chunk_id(document_id(doc), page, ord),
        doc_path=doc,
        page=page,
        ord=ord,
        text=text,
    )


def extraction(doc: str, doc_type: str, extractor: str, *chunks: Chunk) -> Extraction:
    return Extraction(
        doc_path=doc, doc_type=doc_type, extractor=extractor, chunks=list(chunks)
    )


def node(
    node_id: str, node_type: str, props: dict, doc: str, quote: str
) -> ResolvedNode:
    return ResolvedNode(
        id=node_id,
        type=node_type,
        label=node_id,
        props={**props},
        evidence=ev(doc, quote),
        sources=[doc],
    )


def asset_node(doc: str = HANDOVER, quote: str = "HP1 stopped") -> ResolvedNode:
    return node(
        "HP-01",
        "Asset",
        {
            "id": "HP-01",
            "name": "Press",
            "machine_type": "press",
            "vendor": "Bramwell Hydraulics",
            "model": "BH-250",
        },
        doc,
        quote,
    )


def alarm_node(
    doc: str = LOG, quote: str = "ALARM E217 Oil pressure low"
) -> ResolvedNode:
    return node(
        "E-217",
        "AlarmCode",
        {"code": "E-217", "text": "Oil pressure low", "severity": "warning"},
        doc,
        quote,
    )


def incident_node(
    doc: str = HANDOVER, quote: str = "oil under the ram", **props
) -> ResolvedNode:
    return node(
        "INC-2026-001",
        "Incident",
        {
            "id": "INC-2026-001",
            "date": "2026-08-22",
            "shift": "B",
            "downtime_min": 90,
            "symptom_text": "oil under the ram",
            **props,
        },
        doc,
        quote,
    )


def edge(src: str, rel: str, dst: str, doc: str, quote: str, **props) -> ResolvedEdge:
    return ResolvedEdge(src=src, rel=rel, dst=dst, props=props, evidence=ev(doc, quote))


def small_validated() -> Validated:
    """A press, its alarm, and an incident on the press that began with the alarm."""
    return Validated(
        nodes=[asset_node(), alarm_node(), incident_node()],
        edges=[
            edge("INC-2026-001", "OCCURRED_ON", "HP-01", HANDOVER, "HP1 stopped"),
            edge("INC-2026-001", "TRIGGERED_BY", "E-217", HANDOVER, "with E217, oil"),
        ],
    )


def small_extractions() -> list[Extraction]:
    return [
        extraction(
            LOG,
            "log",
            "log-parser",
            chunk(LOG, 0, "2026-08-22T01:00:00Z HP-01 ALARM E217 Oil pressure low"),
        ),
        extraction(
            HANDOVER,
            "handover",
            "sol-text",
            chunk(HANDOVER, 0, "Shift handover 22 Aug B"),
            chunk(
                HANDOVER,
                1,
                "HP1 stopped with E217, oil under the ram. Replaced the main seals.",
            ),
        ),
    ]
