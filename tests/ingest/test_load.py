"""Tests for the loader, which writes the extracted graph to the database. It covers
a full load into a fresh database, adding or updating a single file, and the record of
what was loaded that a single-file run reads back. The tests use a fake embedder and
hand-built objects, with no network or model."""

import hashlib
import json
from contextlib import closing

import pytest

from app import store
from app.ingest.load import (
    BATCH_SIZE,
    load_file,
    load_full,
    raw_files,
    registry_from_store,
)
from app.ingest.normalize import document_id
from app.ingest.types import ReviewItem
from app.ingest.validate import Flag, Validated
from load_stubs import (
    HANDOVER,
    LOG,
    NOTE,
    FakeEmbedder,
    asset_node,
    chunk,
    edge,
    ev,
    extraction,
    incident_node,
    small_extractions,
    small_validated,
    write_raw,
)


@pytest.fixture
def raw(tmp_path):
    return write_raw(tmp_path / "raw")


@pytest.fixture
def kg(tmp_path):
    return tmp_path / "out" / "kg.sqlite"


def query(path, sql, *params):
    with closing(store.connect(path)) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def full(raw, kg, validated=None, extractions=None, **options):
    options.setdefault("embedder", FakeEmbedder())
    return load_full(
        validated or small_validated(),
        extractions or small_extractions(),
        raw,
        kg,
        **options,
    )


# --- raw files ----------------------------------------------------------------


def test_raw_files_lists_every_visible_file_sorted_and_skips_hidden_ones(raw):
    assert [p.relative_to(raw).as_posix() for p in raw_files(raw)] == [
        HANDOVER,
        LOG,
        NOTE,
    ]


# --- the full load ------------------------------------------------------------


def test_a_full_load_writes_the_graph_to_a_fresh_database(raw, kg):
    full(raw, kg)
    assert query(kg, "SELECT id, type FROM nodes ORDER BY id") == [
        {"id": "E-217", "type": "AlarmCode"},
        {"id": "HP-01", "type": "Asset"},
        {"id": "INC-2026-001", "type": "Incident"},
    ]
    assert query(kg, "SELECT id FROM edges ORDER BY id") == [
        {"id": "INC-2026-001|OCCURRED_ON|HP-01"},
        {"id": "INC-2026-001|TRIGGERED_BY|E-217"},
    ]
    assert query(kg, "SELECT count(*) AS n FROM chunks") == [{"n": 3}]


def test_a_full_load_returns_the_counts_it_wrote(raw, kg):
    result = full(raw, kg, review=[ReviewItem(kind="merge", payload={"a": 1})])
    assert result == {
        "documents": 3,
        "chunks": 3,
        "nodes": 3,
        "edges": 2,
        "evidence": 5,
        "review": 1,
    }


def test_a_full_load_replaces_the_old_database_and_leaves_no_temporary_file(raw, kg):
    kg.parent.mkdir(parents=True)
    with closing(store.connect(kg)) as conn:
        store.init_schema(conn)
        store.upsert_document(conn, "DOC-00000000", "old/stale.txt", "log", "0" * 64)
    full(raw, kg)
    assert query(kg, "SELECT id FROM documents WHERE id = 'DOC-00000000'") == []
    assert sorted(p.name for p in kg.parent.iterdir()) == ["kg.sqlite"]


def test_a_failed_build_leaves_the_old_database_untouched(raw, kg):
    full(raw, kg)
    before = sha(kg)
    broken = small_validated()
    broken.nodes[0].props = {
        "id": "HP-01"
    }  # upsert_node refuses props that do not parse
    with pytest.raises(ValueError):
        full(raw, kg, validated=broken)
    assert sha(kg) == before
    assert sorted(p.name for p in kg.parent.iterdir()) == ["kg.sqlite"]


def test_a_failing_embedder_leaves_the_old_database_untouched(raw, kg):
    full(raw, kg)
    before = sha(kg)
    with pytest.raises(RuntimeError, match="embedder is down"):
        full(raw, kg, embedder=FakeEmbedder(fail=True))
    assert sha(kg) == before
    assert sorted(p.name for p in kg.parent.iterdir()) == ["kg.sqlite"]


def test_a_failed_first_build_creates_no_database(raw, kg):
    with pytest.raises(RuntimeError):
        full(raw, kg, embedder=FakeEmbedder(fail=True))
    assert not kg.exists()
    assert not kg.parent.exists() or list(kg.parent.iterdir()) == []


# --- documents ----------------------------------------------------------------


def test_every_raw_file_gets_one_document_row_with_its_hash(raw, kg):
    full(raw, kg)
    rows = query(kg, "SELECT id, path, doc_type, sha256 FROM documents ORDER BY path")
    assert rows == [
        {
            "id": document_id(path),
            "path": path,
            "doc_type": doc_type,
            "sha256": hashlib.sha256((raw / path).read_bytes()).hexdigest(),
        }
        for path, doc_type in (
            (HANDOVER, "handover"),
            (LOG, "log"),
            (NOTE, "maintenance_note"),
        )
    ]


def test_a_chunk_of_a_file_outside_the_raw_folder_is_an_error(raw, kg):
    stray = extraction(
        "logs/not_there.log", "log", "log-parser", chunk("logs/not_there.log", 0, "x")
    )
    with pytest.raises(ValueError, match="not_there"):
        full(raw, kg, extractions=[*small_extractions(), stray])


# --- chunks, search ------------------------------------------------------------


def test_chunks_are_stored_under_their_document_and_found_by_keyword_search(raw, kg):
    full(raw, kg)
    with closing(store.connect(kg)) as conn:
        hits = store.keyword_search(conn, "oil under the ram", 3)
    assert hits[0]["chunk_id"] == f"{document_id(HANDOVER)}-0-1"
    assert hits[0]["doc_id"] == document_id(HANDOVER)


def test_chunks_are_found_by_vector_search_with_the_same_embedder(raw, kg):
    embedder = FakeEmbedder()
    full(raw, kg, embedder=embedder)
    with closing(store.connect(kg)) as conn:
        hits = store.vector_search(
            conn, embedder.embed_query("shift handover 22 Aug"), 2
        )
    assert hits[0]["chunk_id"] == f"{document_id(HANDOVER)}-0-0"
    assert hits[0]["score"] > hits[1]["score"]


def test_chunks_are_embedded_in_batches(raw, kg):
    many = [chunk(HANDOVER, i, f"line number {i} of the handover") for i in range(150)]
    embedder = FakeEmbedder()
    full(
        raw,
        kg,
        extractions=[extraction(HANDOVER, "handover", "sol-text", *many)],
        embedder=embedder,
    )
    assert embedder.batches == [BATCH_SIZE, BATCH_SIZE, 150 - 2 * BATCH_SIZE]
    assert query(kg, "SELECT count(*) AS n FROM chunks") == [{"n": 150}]


# --- evidence -----------------------------------------------------------------


def evidence_of(kg, fact_id):
    return query(
        kg,
        "SELECT doc_id, chunk_id, page, quote, extractor, confidence"
        " FROM evidence WHERE fact_id = ? ORDER BY id",
        fact_id,
    )


def test_evidence_gets_the_document_id_and_the_chunk_that_holds_the_quote(raw, kg):
    full(raw, kg)
    assert evidence_of(kg, "HP-01") == [
        {
            "doc_id": document_id(HANDOVER),
            "chunk_id": f"{document_id(HANDOVER)}-0-1",
            "page": None,
            "quote": "HP1 stopped",
            "extractor": "sol-text",
            "confidence": 0.8,
        }
    ]
    assert evidence_of(kg, "E-217")[0]["chunk_id"] == f"{document_id(LOG)}-0-0"


def test_evidence_for_an_edge_is_stored_under_the_edge_id(raw, kg):
    full(raw, kg)
    rows = query(
        kg,
        "SELECT fact_kind, fact_id FROM evidence WHERE fact_kind = 'edge' ORDER BY id",
    )
    assert rows == [
        {"fact_kind": "edge", "fact_id": "INC-2026-001|OCCURRED_ON|HP-01"},
        {"fact_kind": "edge", "fact_id": "INC-2026-001|TRIGGERED_BY|E-217"},
    ]


def test_evidence_whose_quote_is_in_no_chunk_has_no_chunk_id(raw, kg):
    validated = Validated(
        nodes=[asset_node(quote="a spreadsheet row | not in a chunk")]
    )
    full(raw, kg, validated=validated)
    row = evidence_of(kg, "HP-01")[0]
    assert row["chunk_id"] is None
    assert row["doc_id"] == document_id(HANDOVER)


def test_evidence_prefers_the_chunk_on_its_own_page(raw, kg):
    pages = extraction(
        HANDOVER,
        "handover",
        "sol-text",
        chunk(HANDOVER, 0, "the press stopped", page=1),
        chunk(HANDOVER, 0, "the press stopped again", page=2),
    )
    asset = asset_node(quote="the press stopped")
    asset.evidence = ev(HANDOVER, "the press stopped", page=2)
    full(raw, kg, validated=Validated(nodes=[asset]), extractions=[pages])
    assert evidence_of(kg, "HP-01")[0]["chunk_id"] == f"{document_id(HANDOVER)}-2-0"
    assert evidence_of(kg, "HP-01")[0]["page"] == 2


def test_identical_evidence_items_are_stored_once(raw, kg):
    asset = asset_node()
    asset.evidence = asset.evidence * 2
    full(raw, kg, validated=Validated(nodes=[asset]))
    assert len(evidence_of(kg, "HP-01")) == 1


def test_evidence_that_cites_a_file_nobody_loaded_is_an_error(raw, kg):
    full(raw, kg)
    before = sha(kg)
    stray = asset_node(doc="handovers/missing.txt")
    with pytest.raises(ValueError, match="missing.txt"):
        full(raw, kg, validated=Validated(nodes=[stray]))
    assert sha(kg) == before


# --- the review queue -----------------------------------------------------------


def test_the_review_queue_holds_the_review_items_then_the_validator_flags(raw, kg):
    review = [
        ReviewItem(
            kind="merge",
            doc_paths=[HANDOVER],
            payload={"mention": "the press"},
            confidence=0.55,
        ),
        ReviewItem(kind="unresolved", doc_paths=[LOG], payload={"mention": "?"}),
    ]
    validated = small_validated()
    validated.flags = [
        Flag(
            rule="no_edges",
            severity="warning",
            subject="HP-01",
            detail="the node has no edges",
        )
    ]
    full(raw, kg, validated=validated, review=review)
    rows = query(
        kg, "SELECT kind, payload, confidence, status FROM review_queue ORDER BY id"
    )
    assert [(r["kind"], r["confidence"], r["status"]) for r in rows] == [
        ("merge", 0.55, "open"),
        ("unresolved", None, "open"),
        ("validator", None, "open"),
    ]
    assert json.loads(rows[0]["payload"]) == {
        "doc_paths": [HANDOVER],
        "mention": "the press",
    }
    assert json.loads(rows[2]["payload"]) == {
        "rule": "no_edges",
        "severity": "warning",
        "subject": "HP-01",
        "detail": "the node has no edges",
    }


# --- the registry -------------------------------------------------------------


def test_registry_from_store_reads_back_nodes_edges_and_evidence(raw, kg):
    validated = small_validated()
    full(raw, kg, validated=validated)
    registry = registry_from_store(kg)
    assert [n.id for n in registry.nodes] == ["E-217", "HP-01", "INC-2026-001"]
    assert [(e.src, e.rel, e.dst) for e in registry.edges] == [
        ("INC-2026-001", "OCCURRED_ON", "HP-01"),
        ("INC-2026-001", "TRIGGERED_BY", "E-217"),
    ]
    by_id = {n.id: n for n in registry.nodes}
    for original in validated.nodes:
        back = by_id[original.id]
        assert (back.type, back.label) == (original.type, original.label)
        assert back.evidence == original.evidence
        assert back.sources == original.sources
        for key, value in original.props.items():
            assert back.props[key] == value
    assert registry.edges[0].evidence == validated.edges[0].evidence


def test_registry_from_store_sources_are_the_documents_of_the_evidence(raw, kg):
    asset = asset_node()
    asset.evidence = ev(HANDOVER, "HP1 stopped") + ev(LOG, "HP-01 ALARM")
    asset.sources = []
    full(raw, kg, validated=Validated(nodes=[asset]))
    assert registry_from_store(kg).nodes[0].sources == [HANDOVER, LOG]


def test_registry_from_store_of_a_missing_database_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        registry_from_store(tmp_path / "none.sqlite")


# --- the single-file upsert -------------------------------------------------------

NEW = "maintenance/note_from_weiming.txt"
NEW_TEXT = "Weiming: the press seal is weeping again.\n"


def new_note_run(raw):
    """What a run on a loose note that arrived after the full load adds: a second
    incident on the press."""
    (raw / NEW).write_text(NEW_TEXT, encoding="utf-8")
    second = incident_node(doc=NEW, quote="the press seal is weeping")
    second.id = "INC-2026-002"
    second.props["id"] = "INC-2026-002"
    validated = small_validated()
    validated.nodes.append(second)
    validated.edges.append(
        edge("INC-2026-002", "OCCURRED_ON", "HP-01", NEW, "the press seal")
    )
    extractions = [
        extraction(
            NEW,
            "maintenance_note",
            "sol-text",
            chunk(NEW, 0, "Weiming: the press seal is weeping again."),
        )
    ]
    return validated, extractions, [raw / NEW]


def test_load_file_adds_one_files_results_and_deletes_nothing(raw, kg):
    full(raw, kg)
    validated, extractions, paths = new_note_run(raw)
    before = {r["id"] for r in query(kg, "SELECT id FROM nodes")}
    load_file(validated, extractions, paths, kg, raw_dir=raw, embedder=FakeEmbedder())
    after = {r["id"] for r in query(kg, "SELECT id FROM nodes")}
    assert before < after and after - before == {"INC-2026-002"}
    assert query(kg, "SELECT count(*) AS n FROM edges") == [{"n": 3}]
    assert query(kg, "SELECT count(*) AS n FROM chunks") == [{"n": 4}]
    row = evidence_of(kg, "INC-2026-002")[0]
    assert row["chunk_id"] == f"{document_id(NEW)}-0-0"
    assert row["doc_id"] == document_id(NEW)


def test_load_file_writes_the_document_row_of_the_file_it_was_given(raw, kg):
    full(raw, kg)
    validated, extractions, paths = new_note_run(raw)
    load_file(validated, extractions, paths, kg, raw_dir=raw, embedder=FakeEmbedder())
    rows = query(kg, "SELECT id, path, doc_type, sha256 FROM documents ORDER BY path")
    assert sorted(r["path"] for r in rows) == sorted([HANDOVER, LOG, NOTE, NEW])
    new = next(r for r in rows if r["path"] == NEW)
    assert new == {
        "id": document_id(NEW),
        "path": NEW,
        "doc_type": "maintenance_note",
        "sha256": hashlib.sha256(NEW_TEXT.encode()).hexdigest(),
    }


def test_load_file_run_twice_adds_no_duplicate_evidence_or_review_rows(raw, kg):
    full(raw, kg)
    review = [
        ReviewItem(kind="merge", doc_paths=[NEW], payload={"mention": "the press"})
    ]
    validated, extractions, paths = new_note_run(raw)
    validated.flags = [
        Flag(rule="no_edges", severity="warning", subject="E-217", detail="x")
    ]
    for _ in range(2):
        load_file(
            validated,
            extractions,
            paths,
            kg,
            raw_dir=raw,
            review=review,
            embedder=FakeEmbedder(),
        )
    assert query(kg, "SELECT count(*) AS n FROM review_queue") == [{"n": 2}]
    assert len(evidence_of(kg, "INC-2026-002")) == 1
    assert len(evidence_of(kg, "HP-01")) == 1
    assert query(kg, "SELECT count(*) AS n FROM edges") == [{"n": 3}]


def test_load_file_merges_new_props_over_the_stored_ones(raw, kg):
    full(raw, kg)
    validated, extractions, paths = new_note_run(raw)
    next(n for n in validated.nodes if n.id == "HP-01").props["name"] = (
        "Hydraulic press 2"
    )
    load_file(validated, extractions, paths, kg, raw_dir=raw, embedder=FakeEmbedder())
    props = json.loads(
        query(kg, "SELECT props FROM nodes WHERE id = 'HP-01'")[0]["props"]
    )
    assert props["name"] == "Hydraulic press 2"
    assert props["vendor"] == "Bramwell Hydraulics"


def counts_of(kg):
    return {
        table: query(kg, f"SELECT count(*) AS n FROM {table}")[0]["n"]
        for table in (
            "documents",
            "chunks",
            "nodes",
            "edges",
            "evidence",
            "review_queue",
        )
    }


def test_load_file_with_a_bad_evidence_path_raises_before_writing_anything(raw, kg):
    full(raw, kg)
    validated, extractions, paths = new_note_run(raw)
    validated.nodes[0].evidence += asset_node(doc="maintenance/missing_a.txt").evidence
    validated.edges.append(
        edge(
            "INC-2026-002",
            "OCCURRED_ON",
            "HP-01",
            "maintenance/missing_b.txt",
            "the press seal",
        )
    )
    before = counts_of(kg)
    with pytest.raises(ValueError, match="missing_a.txt") as error:
        load_file(
            validated, extractions, paths, kg, raw_dir=raw, embedder=FakeEmbedder()
        )
    assert "missing_b.txt" in str(error.value)
    assert counts_of(kg) == before


def test_load_file_accepts_evidence_for_the_new_file_and_for_stored_documents(raw, kg):
    full(raw, kg)
    validated, extractions, paths = new_note_run(raw)
    load_file(validated, extractions, paths, kg, raw_dir=raw, embedder=FakeEmbedder())
    assert counts_of(kg)["nodes"] == 4


def test_load_file_on_a_missing_database_is_an_error(raw, kg):
    validated, extractions, paths = new_note_run(raw)
    with pytest.raises(FileNotFoundError):
        load_file(
            validated, extractions, paths, kg, raw_dir=raw, embedder=FakeEmbedder()
        )
    assert not kg.exists()


def test_a_registry_round_trip_through_load_file_keeps_the_graph_whole(raw, kg):
    full(raw, kg)
    registry = registry_from_store(kg)
    validated = Validated(nodes=list(registry.nodes), edges=list(registry.edges))
    load_file(validated, [], [], kg, raw_dir=raw, embedder=FakeEmbedder())
    assert query(kg, "SELECT count(*) AS n FROM nodes") == [{"n": 3}]
    assert query(kg, "SELECT count(*) AS n FROM edges") == [{"n": 2}]
    assert query(kg, "SELECT count(*) AS n FROM evidence") == [{"n": 5}]
