"""Tests for the stage events of a one-file ingest: chunked, facts, matched and placed.
Fake extractors, a scripted decider and a fake embedder replace the network and the
real model."""

import json
from contextlib import closing

import numpy as np
import pytest
from llm_stubs import SETTINGS, StubChatModel, isolated  # noqa: F401
from load_stubs import FakeEmbedder, chunk
from resolve_stubs import ScriptedDecider
from test_ingest_graph import (
    FILES,
    NEW_NOTE,
    edge,
    fake_handover,
    fake_log,
    fake_manual,
    fake_note,
    node,
    query,
    script,
)

from app import factmap, llm, store
from app.ingest import events, graph, router
from app.ingest.normalize import chunk_id, document_id
from app.ingest.types import (
    CandidateEdge,
    Extraction,
    NodeRef,
    Resolution,
    ResolvedNode,
    MergeDecision,
)

DOC = "maintenance/note.txt"


def first_three(vectors):
    return np.asarray(vectors, dtype=np.float64)[:, :3]


# --- the payloads, as pure functions --------------------------------------------------


def test_chunked_lists_the_chunks_with_ids_pages_orders_and_the_first_200_characters():
    long = "x" * 250
    extraction = Extraction(
        doc_path=DOC,
        doc_type="maintenance_note",
        extractor="sol-text",
        chunks=[chunk(DOC, 0, "short"), chunk(DOC, 1, long), chunk(DOC, 2, "p", 4)],
    )
    shown = events.chunked(extraction)
    assert shown["doc_path"] == DOC
    assert [c["id"] for c in shown["chunks"]] == [
        chunk_id(document_id(DOC), None, 0),
        chunk_id(document_id(DOC), None, 1),
        chunk_id(document_id(DOC), 4, 2),
    ]
    assert [c["page"] for c in shown["chunks"]] == [None, None, 4]
    assert [c["ord"] for c in shown["chunks"]] == [0, 1, 2]
    assert [len(c["text"]) for c in shown["chunks"]] == [5, 200, 1]
    assert set(shown["chunks"][0]) == {"id", "page", "ord", "text"}


def test_facts_lists_the_candidates_with_the_ids_they_name(tmp_path):
    note = tmp_path / "note_from_weiming.txt"
    note.write_text("Weiming: HP1 leaked oil.\n")
    extraction = fake_note(note, None)
    shown = events.facts(extraction)
    assert shown["nodes"] == [
        {"id": "WO-9", "type": "Incident", "label": "oil under the ram"}
    ]
    assert shown["edges"] == [
        {
            "id": "WO-9|OCCURRED_ON|HP-01",
            "src": "WO-9",
            "rel": "OCCURRED_ON",
            "dst": "HP-01",
        },
        {
            "id": "WO-9|TRIGGERED_BY|E-217",
            "src": "WO-9",
            "rel": "TRIGGERED_BY",
            "dst": "E-217",
        },
    ]


def resolved(node_id, type_="Asset"):
    return ResolvedNode(id=node_id, type=type_, label=node_id)


def test_matched_has_the_candidates_that_landed_on_known_nodes_and_the_new_ids():
    extraction = Extraction(
        doc_path=DOC,
        doc_type="maintenance_note",
        extractor="sol-text",
        nodes=[node(DOC, "Asset", "HP-01", "Press", {}, "sol-text", "HP-01")],
        edges=[
            edge(
                DOC,
                ("Incident", "WO-9"),
                "OCCURRED_ON",
                ("Asset", "the press"),
                "sol-text",
                "q",
            ),
            edge(
                DOC,
                ("Incident", "WO-9"),
                "TRIGGERED_BY",
                ("AlarmCode", "E-999"),
                "sol-text",
                "q",
            ),
        ],
    )
    resolution = Resolution(
        nodes=[
            resolved("HP-01"),
            resolved("E-999", "AlarmCode"),
            resolved("INC-1", "Incident"),
        ],
        merges=[
            MergeDecision(
                doc_path=DOC,
                type="Asset",
                mention="the press",
                candidate="HP-01",
                p=0.9,
                decision="merge",
            ),
            MergeDecision(
                doc_path=DOC,
                type="AlarmCode",
                mention="E-999",
                candidate="E-999",
                p=1.0,
                decision="rule",
            ),
            MergeDecision(
                doc_path=DOC,
                type="Part",
                mention="widget",
                candidate="SEAL-KIT-50",
                p=0.4,
                decision="queue",
            ),
        ],
    )
    shown = events.matched([extraction], {"HP-01", "SEAL-KIT-50"}, resolution)
    assert shown == {
        "merged": [
            {"from": "HP-01", "to": "HP-01"},
            {"from": "the press", "to": "HP-01"},
        ],
        "new": ["E-999", "INC-1"],
    }


def test_matched_with_nothing_known_is_all_new():
    shown = events.matched([], set(), Resolution(nodes=[resolved("A"), resolved("B")]))
    assert shown == {"merged": [], "new": ["A", "B"]}


# --- the events of a run ---------------------------------------------------------------


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / "raw"
    for path, data in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


@pytest.fixture
def kg(tmp_path):
    return tmp_path / "data" / "kg.sqlite"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(
        router,
        "REGISTRY",
        {
            ("manual", ".pdf"): fake_manual,
            ("log", ".log"): fake_log,
            ("handover", ".txt"): fake_handover,
            ("maintenance_note", ".txt"): fake_note,
        },
    )
    stub = StubChatModel(callbacks=[llm.CostTracker(SETTINGS)])
    monkeypatch.setattr(
        llm, "chat_model", lambda role, effort="medium", *, settings=None: stub
    )


@pytest.fixture
def built(raw, kg):
    """A graph from the full run and a map of it, in tmp_path/data."""
    graph.run_full(
        raw_dir=raw,
        kg_path=kg,
        settings=SETTINGS,
        decider=ScriptedDecider(script),
        embedder=FakeEmbedder(),
    )
    map_path = kg.parent / "map" / "fact_map.json"
    factmap.write_map(kg, map_path, FakeEmbedder(), first_three)
    return kg, map_path


def ingest(raw, kg, map_path, name=NEW_NOTE, text=b"Weiming: oil again.\n"):
    target = raw / name
    target.write_bytes(text)
    return list(
        graph.stream_ingest(
            target,
            raw_dir=raw,
            kg_path=kg,
            settings=SETTINGS,
            decider=ScriptedDecider(script),
            embedder=FakeEmbedder(),
            map_path=map_path,
        )
    )


def customs(chunks):
    return [c["data"] for c in chunks if c["type"] == "custom"]


def test_a_one_file_ingest_emits_the_stage_events_in_order(raw, built):
    kg, map_path = built
    names = [e["event"] for e in customs(ingest(raw, kg, map_path))]
    assert names == ["extracted", "chunked", "facts", "matched", "placed"]


def test_the_chunked_ids_are_the_ids_that_load_stores(raw, built):
    kg, map_path = built
    stream = ingest(raw, kg, map_path)
    chunked = next(e for e in customs(stream) if e["event"] == "chunked")
    stored = query(
        kg,
        "SELECT id, page, ord, text FROM chunks WHERE doc_id = ?",
        document_id(NEW_NOTE),
    )
    assert chunked["doc_path"] == NEW_NOTE
    assert chunked["chunks"] == [
        {"id": r["id"], "page": r["page"], "ord": r["ord"], "text": r["text"][:200]}
        for r in stored
    ]
    assert len(stored) == 1


def test_the_facts_event_carries_the_candidates_before_matching(raw, built):
    kg, map_path = built
    facts = next(e for e in customs(ingest(raw, kg, map_path)) if e["event"] == "facts")
    assert [n["id"] for n in facts["nodes"]] == ["WO-9"]  # the draft key, not INC-...
    assert [e["id"] for e in facts["edges"]] == [
        "WO-9|OCCURRED_ON|HP-01",
        "WO-9|TRIGGERED_BY|E-217",
    ]


def test_the_matched_event_names_the_nodes_it_joined_and_the_new_one(raw, built):
    kg, map_path = built
    matched = next(
        e for e in customs(ingest(raw, kg, map_path)) if e["event"] == "matched"
    )
    assert matched["new"] == ["INC-2026-002"]
    assert {"from": "HP-01", "to": "HP-01"} in matched["merged"]
    assert {"from": "E-217", "to": "E-217"} in matched["merged"]


def test_the_placed_event_has_the_new_points_with_positions(raw, built):
    kg, map_path = built
    before = map_path.read_bytes()
    stream = ingest(raw, kg, map_path, text=b"Weiming: HP1 leaked oil under the ram.\n")
    placed = next(e for e in customs(stream) if e["event"] == "placed")
    passage = chunk_id(document_id(NEW_NOTE), None, 0)
    assert [p["id"] for p in placed["passages"]] == [passage]
    assert set(placed["passages"][0]) == {"id", "x", "y", "z"}
    ids = {f["id"] for f in placed["facts"]}
    assert "INC-2026-002" in ids and "INC-2026-002|OCCURRED_ON|HP-01" in ids
    incident = next(f for f in placed["facts"] if f["id"] == "INC-2026-002")
    assert (incident["kind"], incident["type"]) == ("node", "Incident")
    assert incident["passages"] == [passage]
    assert all(
        set(f) >= {"id", "kind", "type", "label", "x", "y", "z", "passages"}
        for f in placed["facts"]
    )
    for point in (*placed["passages"], *placed["facts"]):
        assert all(-1.0 <= point[a] <= 1.0 for a in "xyz")
    assert map_path.read_bytes() == before  # the graph only reports; the API appends


def test_the_placed_update_holds_the_vectors_for_the_map(raw, built):
    kg, map_path = built
    stream = ingest(raw, kg, map_path)
    update = next(
        c["data"]["load"]
        for c in stream
        if c["type"] == "updates"
        if "load" in c["data"]
    )
    placement = update["placed"]
    assert isinstance(placement, factmap.Placement)
    rows = len(placement.passages) + len(placement.facts)
    assert (
        placement.vectors.shape == (rows, 384) and placement.vectors.dtype == np.float32
    )


def test_without_a_map_there_is_no_placed_event_and_the_ingest_still_loads(raw, built):
    kg, _ = built
    stream = ingest(raw, kg, None)
    assert [e["event"] for e in customs(stream)][-1] == "matched"
    assert query(kg, "SELECT id FROM nodes WHERE id = 'INC-2026-002'")


def test_a_map_that_no_longer_matches_its_vectors_is_skipped_not_fatal(raw, built):
    kg, map_path = built
    np.save(factmap.vectors_path(map_path), np.zeros((3, 384), dtype=np.float32))
    stream = ingest(raw, kg, map_path)
    assert "placed" not in [e["event"] for e in customs(stream)]
    assert query(kg, "SELECT id FROM nodes WHERE id = 'INC-2026-002'")


def test_a_full_run_emits_none_of_the_stage_events(raw, kg):
    chunks = list(
        graph.stream_ingest(
            raw_dir=raw,
            kg_path=kg,
            settings=SETTINGS,
            decider=ScriptedDecider(script),
            embedder=FakeEmbedder(),
        )
    )
    assert {e["event"] for e in customs(chunks)} == {"extracted"}
