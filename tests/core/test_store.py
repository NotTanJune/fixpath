"""Tests for the graph database: its tables, adding and updating nodes and edges,
evidence, reading the graph, keyword and vector search, and the export to a graph
object.

Every database is a temporary file. None touches data/kg.sqlite.
"""

import datetime as dt
import inspect
import json
import sqlite3

import networkx as nx
import numpy as np
import pytest

from app import config, embed, store
from app.ontology import Evidence

DOC = "DOC-0123abcd"
OTHER_DOC = "DOC-89abcdef"

NODES = {
    "LINE-01": ("Line", "Line 1", {"id": "LINE-01", "name": "Line 1"}),
    "CNC-01": (
        "Asset",
        "CNC lathe 1",
        {
            "id": "CNC-01",
            "name": "CNC lathe 1",
            "machine_type": "CNC lathe",
            "vendor": "Kessler Werkzeugmaschinen",
            "model": "KL-300",
        },
    ),
    "CNC-01-SPINDLE": (
        "Component",
        "Spindle",
        {"id": "CNC-01-SPINDLE", "name": "Spindle", "asset_id": "CNC-01"},
    ),
    "E-214": (
        "AlarmCode",
        "E-214",
        {"code": "E-214", "text": "Chiller low coolant", "severity": "warning"},
    ),
    "FM-LOW-COOLANT": (
        "FailureMode",
        "Low coolant",
        {
            "id": "FM-LOW-COOLANT",
            "name": "Low coolant",
            "mechanism": "Coolant level below minimum",
        },
    ),
    "GRP-FNG-12": (
        "Part",
        "Gripper finger",
        {"part_no": "GRP-FNG-12", "description": "Gripper finger"},
    ),
}


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "kg.sqlite")
    store.init_schema(connection)
    yield connection
    connection.close()


def put(conn, *ids):
    for node_id in ids:
        node_type, label, props = NODES[node_id]
        store.upsert_node(conn, node_id, node_type, label, props)


def link(conn, src, rel, dst, props=None):
    put(conn, src, dst)
    return store.upsert_edge(conn, src, rel, dst, props or {})


def graph(conn):
    """LINE-01 -> CNC-01 -> spindle, CNC-01 raised E-214, which points at a cause."""
    link(conn, "LINE-01", "HAS_MACHINE", "CNC-01")
    link(conn, "CNC-01", "HAS_COMPONENT", "CNC-01-SPINDLE")
    link(conn, "CNC-01", "RAISED", "E-214", RAISED)
    link(
        conn, "E-214", "INDICATES", "FM-LOW-COOLANT", {"likelihood": 0.8, "support": 5}
    )


RAISED = {
    "count": 3,
    "first_seen": "2026-04-01T08:00:00",
    "last_seen": "2026-04-09T21:30:00",
}


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def add_document(conn, doc_id=DOC, path="logs/a.log"):
    store.upsert_document(conn, doc_id, path, "log", "ab" * 32)


def table_info(conn, table):
    return [
        (row["name"], row["type"], row["notnull"], row["pk"])
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]


def vec(*leading: float, dtype=np.float32) -> np.ndarray:
    """An embedding of the real size: these leading numbers, then zeros."""
    out = np.zeros(embed.DIMENSIONS, dtype=dtype)
    out[: len(leading)] = leading
    return out


# --- connection and schema --------------------------------------------------


def test_connect_defaults_to_the_graph_path():
    default = inspect.signature(store.connect).parameters["path"].default
    assert default == config.KG_PATH


def test_connect_makes_the_folder_turns_foreign_keys_on_and_returns_rows(tmp_path):
    path = tmp_path / "deep" / "er" / "kg.sqlite"
    connection = store.connect(path)
    try:
        assert path.parent.is_dir()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.row_factory is sqlite3.Row
    finally:
        connection.close()


def test_init_schema_creates_the_specified_tables(conn):
    assert table_info(conn, "documents") == [
        ("id", "TEXT", 0, 1),
        ("path", "TEXT", 1, 0),
        ("doc_type", "TEXT", 1, 0),
        ("sha256", "TEXT", 1, 0),
    ]
    assert table_info(conn, "chunks") == [
        ("id", "TEXT", 0, 1),
        ("doc_id", "TEXT", 1, 0),
        ("page", "INTEGER", 0, 0),
        ("ord", "INTEGER", 1, 0),
        ("text", "TEXT", 1, 0),
        ("embedding", "BLOB", 0, 0),
    ]
    assert table_info(conn, "nodes") == [
        ("id", "TEXT", 0, 1),
        ("type", "TEXT", 1, 0),
        ("label", "TEXT", 1, 0),
        ("props", "TEXT", 1, 0),
    ]
    assert table_info(conn, "edges") == [
        ("id", "TEXT", 0, 1),
        ("src", "TEXT", 1, 0),
        ("rel", "TEXT", 1, 0),
        ("dst", "TEXT", 1, 0),
        ("props", "TEXT", 1, 0),
    ]
    assert table_info(conn, "evidence") == [
        ("id", "INTEGER", 0, 1),
        ("fact_kind", "TEXT", 1, 0),
        ("fact_id", "TEXT", 1, 0),
        ("doc_id", "TEXT", 1, 0),
        ("chunk_id", "TEXT", 0, 0),
        ("page", "INTEGER", 0, 0),
        ("quote", "TEXT", 1, 0),
        ("extractor", "TEXT", 1, 0),
        ("confidence", "REAL", 1, 0),
    ]
    assert table_info(conn, "review_queue") == [
        ("id", "INTEGER", 0, 1),
        ("kind", "TEXT", 1, 0),
        ("payload", "TEXT", 1, 0),
        ("confidence", "REAL", 0, 0),
        ("status", "TEXT", 1, 0),
    ]


def test_review_status_defaults_to_open(conn):
    default = {
        row["name"]: row["dflt_value"]
        for row in conn.execute("PRAGMA table_info(review_queue)")
    }
    assert default["status"] == "'open'"


def test_the_search_index_is_an_fts5_table_with_an_unindexed_chunk_id(conn):
    (sql,) = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
    ).fetchone()
    assert "fts5" in sql.lower()
    assert "chunk_id UNINDEXED" in sql
    assert [row["name"] for row in conn.execute("PRAGMA table_info(chunks_fts)")] == [
        "chunk_id",
        "text",
    ]


def test_foreign_keys_are_declared(conn):
    def references(table):
        return {
            (row["from"], row["table"], row["to"])
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        }

    assert references("chunks") == {("doc_id", "documents", "id")}
    assert references("edges") == {("src", "nodes", "id"), ("dst", "nodes", "id")}


def test_init_schema_can_run_twice_and_keeps_the_rows(conn):
    put(conn, "LINE-01")
    store.init_schema(conn)
    assert count(conn, "nodes") == 1


# --- documents and chunks ---------------------------------------------------


def test_upsert_document_inserts_then_updates_by_id(conn):
    store.upsert_document(conn, DOC, "logs/a.log", "log", "aa" * 32)
    store.upsert_document(conn, DOC, "logs/b.log", "handover", "bb" * 32)
    rows = conn.execute("SELECT * FROM documents").fetchall()
    assert [tuple(row) for row in rows] == [(DOC, "logs/b.log", "handover", "bb" * 32)]


def test_two_documents_cannot_share_a_path(conn):
    add_document(conn, DOC, "logs/a.log")
    with pytest.raises(sqlite3.IntegrityError):
        add_document(conn, OTHER_DOC, "logs/a.log")


def test_add_chunk_writes_the_chunk_and_its_search_index_row(conn):
    add_document(conn)
    store.add_chunk(conn, "c1", DOC, 2, 0, "Spindle chiller alarm", None)
    chunk = conn.execute("SELECT * FROM chunks").fetchone()
    assert (chunk["id"], chunk["doc_id"], chunk["page"], chunk["ord"]) == (
        "c1",
        DOC,
        2,
        0,
    )
    assert chunk["text"] == "Spindle chiller alarm"
    assert chunk["embedding"] is None
    indexed = conn.execute("SELECT chunk_id, text FROM chunks_fts").fetchall()
    assert [tuple(row) for row in indexed] == [("c1", "Spindle chiller alarm")]


def test_add_chunk_stores_the_embedding_as_float32_bytes(conn):
    add_document(conn)
    vector = vec(0.25, -0.5, 1.0, 0.125, dtype=np.float64)
    store.add_chunk(conn, "c1", DOC, None, 0, "text", vector)
    blob = conn.execute("SELECT embedding FROM chunks").fetchone()[0]
    assert isinstance(blob, bytes)
    assert len(blob) == embed.DIMENSIONS * 4
    assert np.frombuffer(blob, dtype=np.float32).tolist() == vector.tolist()


def test_adding_the_same_chunk_again_replaces_it_and_its_index_row(conn):
    add_document(conn)
    store.add_chunk(conn, "c1", DOC, 1, 0, "old words", None)
    store.add_chunk(conn, "c1", DOC, 1, 0, "new words", None)
    assert count(conn, "chunks") == 1
    assert count(conn, "chunks_fts") == 1
    assert conn.execute("SELECT text FROM chunks").fetchone()[0] == "new words"
    assert store.keyword_search(conn, "old", 5) == []
    assert [hit["text"] for hit in store.keyword_search(conn, "new", 5)] == [
        "new words"
    ]


def test_a_chunk_needs_a_known_document(conn):
    with pytest.raises(sqlite3.IntegrityError):
        store.add_chunk(conn, "c1", DOC, 1, 0, "orphan", None)
    assert count(conn, "chunks") == 0
    assert count(conn, "chunks_fts") == 0


@pytest.mark.parametrize(
    "embedding",
    [
        np.zeros(3, np.float32),
        [0.5] * (embed.DIMENSIONS - 1),
        np.zeros(embed.DIMENSIONS + 1),
        np.zeros((2, embed.DIMENSIONS)),
        [],
    ],
    ids=["3 values", "one short", "one long", "two rows", "empty"],
)
def test_add_chunk_rejects_an_embedding_of_the_wrong_size_at_write_time(
    conn, embedding
):
    add_document(conn)
    with pytest.raises(ValueError, match="384"):
        store.add_chunk(conn, "c1", DOC, 1, 0, "text", embedding)
    assert count(conn, "chunks") == 0
    assert count(conn, "chunks_fts") == 0


@pytest.mark.parametrize(
    "length",
    [0, 4, embed.DIMENSIONS * 4 - 4, embed.DIMENSIONS * 4 + 1, embed.DIMENSIONS * 8],
)
def test_add_chunk_rejects_raw_bytes_that_are_not_dimensions_times_four(conn, length):
    add_document(conn)
    with pytest.raises(ValueError, match="384"):
        store.add_chunk(conn, "c1", DOC, 1, 0, "text", bytes(length))
    assert count(conn, "chunks") == 0


def test_add_chunk_keeps_raw_bytes_of_the_right_length_as_they_are(conn):
    add_document(conn)
    raw = vec(0.5, -0.25).tobytes()
    assert len(raw) == embed.DIMENSIONS * 4
    store.add_chunk(conn, "c1", DOC, 1, 0, "text", raw)
    assert conn.execute("SELECT embedding FROM chunks").fetchone()[0] == raw


def test_a_rejected_embedding_leaves_the_stored_chunk_untouched(conn):
    add_document(conn)
    store.add_chunk(conn, "c1", DOC, 1, 0, "old words", vec(1.0))
    with pytest.raises(ValueError, match="384"):
        store.add_chunk(conn, "c1", DOC, 1, 0, "new words", np.zeros(5))
    assert conn.execute("SELECT text FROM chunks").fetchone()[0] == "old words"
    assert [hit["chunk_id"] for hit in store.keyword_search(conn, "old", 5)] == ["c1"]
    assert store.keyword_search(conn, "new", 5) == []


# --- nodes ------------------------------------------------------------------


def test_upsert_node_round_trips_through_get_node(conn):
    put(conn, "CNC-01")
    node = store.get_node(conn, "CNC-01")
    assert node["id"] == "CNC-01"
    assert node["type"] == "Asset"
    assert node["label"] == "CNC lathe 1"
    assert node["props"]["vendor"] == "Kessler Werkzeugmaschinen"
    assert node["props"]["aliases"] == []  # the model default is filled in
    raw = conn.execute("SELECT props FROM nodes WHERE id = 'CNC-01'").fetchone()[0]
    assert json.loads(raw) == node["props"]  # props are stored as JSON text


def test_get_node_returns_none_for_a_missing_id(conn):
    assert store.get_node(conn, "CNC-99") is None


def test_dates_in_props_are_stored_as_iso_text(conn):
    store.upsert_node(
        conn,
        "INC-2026-001",
        "Incident",
        "Chiller alarm",
        {
            "id": "INC-2026-001",
            "date": dt.date(2026, 4, 3),
            "shift": "A",
            "downtime_min": 45,
            "symptom_text": "E-214 on CNC-01",
        },
    )
    assert store.get_node(conn, "INC-2026-001")["props"]["date"] == "2026-04-03"


def test_upsert_node_rejects_an_unknown_type(conn):
    with pytest.raises(ValueError, match="Gadget"):
        store.upsert_node(conn, "CNC-01", "Gadget", "x", {})
    assert count(conn, "nodes") == 0


@pytest.mark.parametrize("bad_id", ["cnc-1", "CNC-1", "CNC-01 ", "LINE-01", ""])
def test_upsert_node_rejects_an_id_that_does_not_fit_the_type(conn, bad_id):
    props = {**NODES["CNC-01"][2], "id": bad_id}
    with pytest.raises(ValueError, match="id"):
        store.upsert_node(conn, bad_id, "Asset", "x", props)
    assert count(conn, "nodes") == 0


def test_upsert_node_rejects_props_that_do_not_parse(conn):
    missing = {"id": "CNC-01", "name": "CNC lathe 1"}  # no machine_type, vendor, model
    with pytest.raises(ValueError, match="CNC-01"):
        store.upsert_node(conn, "CNC-01", "Asset", "x", missing)
    wrong = {"code": "E-214", "text": "t", "severity": "fatal"}  # not a severity
    with pytest.raises(ValueError, match="E-214"):
        store.upsert_node(conn, "E-214", "AlarmCode", "x", wrong)
    assert count(conn, "nodes") == 0


def test_upsert_node_merges_props_and_new_values_win(conn):
    put(conn, "GRP-FNG-12")
    store.upsert_node(
        conn,
        "GRP-FNG-12",
        "Part",
        "Gripper finger",
        {"description": "Gripper finger, long", "qty_on_hand": 4, "bin": "A1"},
    )
    store.upsert_node(conn, "GRP-FNG-12", "Part", "Gripper finger", {"qty_on_hand": 2})
    props = store.get_node(conn, "GRP-FNG-12")["props"]
    assert props["description"] == "Gripper finger, long"  # kept from the second write
    assert props["bin"] == "A1"  # kept from the second write
    assert props["qty_on_hand"] == 2  # the newest value wins
    assert props["part_no"] == "GRP-FNG-12"  # kept from the first write
    assert count(conn, "nodes") == 1


def test_a_none_in_new_props_never_overwrites_a_stored_value(conn):
    def stock(qty):
        store.upsert_node(
            conn, "GRP-FNG-12", "Part", "Gripper finger", {"qty_on_hand": qty}
        )
        return store.get_node(conn, "GRP-FNG-12")["props"]["qty_on_hand"]

    put(conn, "GRP-FNG-12")
    assert stock(4) == 4
    assert stock(None) == 4  # an unknown value is not a change
    assert stock(5) == 5  # a known one still is


def test_a_none_on_the_first_write_is_skipped_like_any_other(conn):
    # With no stored value to protect, the skip leaves the field at its model default.
    props = {**NODES["GRP-FNG-12"][2], "aliases": None, "bin": None}
    store.upsert_node(conn, "GRP-FNG-12", "Part", "Gripper finger", props)
    stored = store.get_node(conn, "GRP-FNG-12")["props"]
    assert stored["aliases"] == []
    assert stored["bin"] is None


def test_upsert_node_replaces_the_label(conn):
    put(conn, "GRP-FNG-12")
    store.upsert_node(conn, "GRP-FNG-12", "Part", "Finger, gripper", {})
    assert store.get_node(conn, "GRP-FNG-12")["label"] == "Finger, gripper"


def test_a_merge_that_breaks_the_props_model_is_rejected_and_changes_nothing(conn):
    put(conn, "E-214")
    with pytest.raises(ValueError, match="E-214"):
        store.upsert_node(
            conn, "E-214", "AlarmCode", "new label", {"severity": "fatal"}
        )
    node = store.get_node(conn, "E-214")
    assert node["label"] == "E-214"
    assert node["props"]["severity"] == "warning"


def test_upsert_node_refuses_to_change_the_type_of_an_existing_node(conn):
    # CNC-01 is a well-formed ID for an Asset and for a Part.
    put(conn, "CNC-01")
    part = {"part_no": "CNC-01", "description": "A part"}
    with pytest.raises(ValueError, match="Asset"):
        store.upsert_node(conn, "CNC-01", "Part", "x", part)
    assert store.get_node(conn, "CNC-01")["type"] == "Asset"


# --- edges ------------------------------------------------------------------


def test_upsert_edge_uses_src_rel_dst_as_its_id(conn):
    put(conn, "LINE-01", "CNC-01")
    edge_id = store.upsert_edge(conn, "LINE-01", "HAS_MACHINE", "CNC-01", {})
    assert edge_id == "LINE-01|HAS_MACHINE|CNC-01"
    row = conn.execute("SELECT * FROM edges").fetchone()
    assert tuple(row) == (edge_id, "LINE-01", "HAS_MACHINE", "CNC-01", "{}")


@pytest.mark.parametrize(
    ("src", "rel", "dst"),
    [
        ("CNC-01", "HAS_MACHINE", "LINE-01"),  # reversed
        ("LINE-01", "HAS_COMPONENT", "CNC-01"),  # wrong relation for these types
        ("LINE-01", "FIXES", "CNC-01"),  # no such relation
        ("LINE-01", "has_machine", "CNC-01"),  # relation names are case sensitive
    ],
)
def test_upsert_edge_rejects_what_the_ontology_does_not_allow(conn, src, rel, dst):
    put(conn, "LINE-01", "CNC-01")
    with pytest.raises(ValueError, match=rel):
        store.upsert_edge(conn, src, rel, dst, {})
    assert count(conn, "edges") == 0


def test_upsert_edge_rejects_an_endpoint_that_is_not_in_the_graph(conn):
    put(conn, "LINE-01")
    with pytest.raises(ValueError, match="CNC-01"):  # the destination is missing
        store.upsert_edge(conn, "LINE-01", "HAS_MACHINE", "CNC-01", {})
    with pytest.raises(ValueError, match="CNC-99"):  # the source is missing
        store.upsert_edge(conn, "CNC-99", "HAS_MACHINE", "LINE-01", {})
    assert count(conn, "edges") == 0


def test_upsert_edge_merges_props_and_new_values_win(conn):
    link(conn, "CNC-01", "RAISED", "E-214", RAISED)
    store.upsert_edge(
        conn, "CNC-01", "RAISED", "E-214", {"count": 7, "last_seen": "2026-05-01"}
    )
    (edge,) = store.neighbors(conn, "CNC-01", rel="RAISED")
    assert edge["edge_props"] == {
        "count": 7,
        "first_seen": "2026-04-01T08:00:00",
        "last_seen": "2026-05-01",
    }
    assert count(conn, "edges") == 1


def test_a_none_in_new_edge_props_never_overwrites_a_stored_value(conn):
    def raised(**props):
        store.upsert_edge(conn, "CNC-01", "RAISED", "E-214", props)
        (edge,) = store.neighbors(conn, "CNC-01", rel="RAISED")
        return edge["edge_props"]

    link(conn, "CNC-01", "RAISED", "E-214", RAISED)  # count 3
    assert raised(count=None)["count"] == 3  # an unknown value is not a change
    both = raised(count=None, last_seen="2026-05-01")
    assert (both["count"], both["last_seen"]) == (3, "2026-05-01")  # a known one wins
    assert raised(count=5)["count"] == 5


def test_a_none_on_the_first_edge_write_is_left_out_like_any_other(conn):
    put(conn, "CNC-01", "E-214")
    store.upsert_edge(
        conn, "CNC-01", "RAISED", "E-214", {"count": None, "first_seen": "2026-04-01"}
    )
    (edge,) = store.neighbors(conn, "CNC-01", rel="RAISED")
    assert edge["edge_props"] == {"first_seen": "2026-04-01"}


def test_edge_props_with_dates_are_stored_as_iso_text(conn):
    link(
        conn,
        "CNC-01",
        "RAISED",
        "E-214",
        {
            "count": 1,
            "first_seen": dt.datetime(2026, 4, 1, 8, 0),
            "last_seen": dt.date(2026, 4, 9),
        },
    )
    (edge,) = store.neighbors(conn, "CNC-01", rel="RAISED")
    assert edge["edge_props"]["first_seen"] == "2026-04-01T08:00:00"
    assert edge["edge_props"]["last_seen"] == "2026-04-09"


def test_edge_props_default_to_empty(conn):
    put(conn, "LINE-01", "CNC-01")
    store.upsert_edge(conn, "LINE-01", "HAS_MACHINE", "CNC-01")
    (edge,) = store.neighbors(conn, "LINE-01")
    assert edge["edge_props"] == {}


# --- evidence and the review queue ------------------------------------------


def evidence(**changes) -> Evidence:
    fields = {
        "fact_kind": "edge",
        "fact_id": "E-214|INDICATES|FM-LOW-COOLANT",
        "doc_id": DOC,
        "chunk_id": "c1",
        "page": 3,
        "quote": "E-214 means the chiller ran low on coolant",
        "extractor": "sol-extract-1",
        "confidence": 0.9,
    }
    return Evidence(**{**fields, **changes})


def test_add_evidence_stores_one_row_and_returns_its_id(conn):
    row_id = store.add_evidence(conn, evidence())
    row = conn.execute("SELECT * FROM evidence WHERE id = ?", (row_id,)).fetchone()
    assert dict(row) == {
        "id": row_id,
        "fact_kind": "edge",
        "fact_id": "E-214|INDICATES|FM-LOW-COOLANT",
        "doc_id": DOC,
        "chunk_id": "c1",
        "page": 3,
        "quote": "E-214 means the chiller ran low on coolant",
        "extractor": "sol-extract-1",
        "confidence": 0.9,
    }


def test_evidence_can_leave_the_chunk_and_page_empty_and_rows_accumulate(conn):
    first = store.add_evidence(conn, evidence(chunk_id=None, page=None))
    second = store.add_evidence(conn, evidence(fact_kind="node", fact_id="E-214"))
    assert first != second
    assert count(conn, "evidence") == 2
    row = conn.execute("SELECT chunk_id, page FROM evidence WHERE id = ?", (first,))
    assert tuple(row.fetchone()) == (None, None)


def test_queue_review_stores_the_payload_as_json_and_opens_the_item(conn):
    row_id = store.queue_review(
        conn, "ambiguous_alias", {"mention": "chiller", "options": ["A", "B"]}, 0.42
    )
    row = conn.execute("SELECT * FROM review_queue WHERE id = ?", (row_id,)).fetchone()
    assert row["kind"] == "ambiguous_alias"
    assert json.loads(row["payload"]) == {"mention": "chiller", "options": ["A", "B"]}
    assert row["confidence"] == 0.42
    assert row["status"] == "open"


# --- neighbours -------------------------------------------------------------


def test_neighbors_out_follows_edges_from_the_node(conn):
    graph(conn)
    found = store.neighbors(conn, "CNC-01")
    assert [(n["rel"], n["id"], n["direction"]) for n in found] == [
        ("HAS_COMPONENT", "CNC-01-SPINDLE", "out"),
        ("RAISED", "E-214", "out"),
    ]
    alarm = found[1]
    assert alarm["type"] == "AlarmCode"
    assert alarm["label"] == "E-214"
    assert alarm["props"]["severity"] == "warning"
    assert alarm["edge_id"] == "CNC-01|RAISED|E-214"
    assert alarm["edge_props"] == RAISED


def test_neighbors_in_follows_edges_into_the_node(conn):
    graph(conn)
    found = store.neighbors(conn, "CNC-01", direction="in")
    assert [(n["rel"], n["id"], n["direction"]) for n in found] == [
        ("HAS_MACHINE", "LINE-01", "in")
    ]


def test_neighbors_both_lists_outgoing_then_incoming(conn):
    graph(conn)
    found = store.neighbors(conn, "CNC-01", direction="both")
    assert [(n["rel"], n["id"], n["direction"]) for n in found] == [
        ("HAS_COMPONENT", "CNC-01-SPINDLE", "out"),
        ("RAISED", "E-214", "out"),
        ("HAS_MACHINE", "LINE-01", "in"),
    ]


def test_neighbors_can_filter_by_relation_in_every_direction(conn):
    graph(conn)
    assert [n["id"] for n in store.neighbors(conn, "CNC-01", rel="RAISED")] == ["E-214"]
    assert store.neighbors(conn, "CNC-01", rel="HAS_MACHINE") == []
    assert [n["id"] for n in store.neighbors(conn, "CNC-01", "HAS_MACHINE", "in")] == [
        "LINE-01"
    ]
    assert [
        n["id"] for n in store.neighbors(conn, "E-214", "RAISED", direction="both")
    ] == ["CNC-01"]


def test_neighbors_of_a_node_without_edges_or_a_missing_node_is_empty(conn):
    put(conn, "GRP-FNG-12")
    assert store.neighbors(conn, "GRP-FNG-12", direction="both") == []
    assert store.neighbors(conn, "CNC-99", direction="both") == []


def test_neighbors_rejects_an_unknown_direction(conn):
    with pytest.raises(ValueError, match="sideways"):
        store.neighbors(conn, "CNC-01", direction="sideways")


# --- keyword search ---------------------------------------------------------

CHUNKS = {
    "c1": "Replace the hydraulic seal kit on the clamp cylinder",
    "c2": "Seal kit 50 mm stock is low, reorder",
    "c3": "Alarm E-214 on CNC-01: spindle chiller low coolant",
    "c4": "GRP-FNG-12 gripper finger replaced at the pick station",
    "c5": "Spindle bearing noise after the tool change",
}


@pytest.fixture
def indexed(conn):
    add_document(conn)
    for order, (chunk_id, text) in enumerate(CHUNKS.items()):
        store.add_chunk(conn, chunk_id, DOC, order + 1, order, text, None)
    return conn


def test_keyword_search_ranks_by_bm25_and_describes_each_hit(indexed):
    hits = store.keyword_search(indexed, "seal kit 50", 5)
    assert [hit["chunk_id"] for hit in hits[:2]] == ["c2", "c1"]  # more terms first
    first = hits[0]
    assert first["doc_id"] == DOC
    assert (first["page"], first["ord"]) == (2, 1)
    assert first["text"] == CHUNKS["c2"]
    scores = [hit["score"] for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(score > 0 for score in scores)


def test_keyword_search_matches_codes_and_part_numbers_as_phrases(indexed):
    assert store.keyword_search(indexed, "E-214", 3)[0]["chunk_id"] == "c3"
    assert store.keyword_search(indexed, "GRP-FNG-12", 3)[0]["chunk_id"] == "c4"


def test_keyword_search_respects_k(indexed):
    assert len(store.keyword_search(indexed, "the", 2)) == 2
    assert store.keyword_search(indexed, "the", 0) == []
    assert store.keyword_search(indexed, "the", -1) == []


@pytest.mark.parametrize(
    "hostile",
    [
        'seal "kit" -50 OR',
        '"',
        '""',
        "-",
        "_",
        "*",
        "^",
        "(",
        ")",
        "NEAR(",
        "NEAR(seal kit, 2)",
        "text:",
        "chunk_id:c1",
        "AND",
        "NOT",
        "OR",
        "seal AND",
        "seal NOT kit",
        "'; DROP TABLE chunks; --",
        "{seal}",
        'a"b',
        'say "hello',
        '"unterminated',
        "seal\x00kit",
        "\ud800",
        "seal \udfff kit",
        "seal\ud800kit",
        "",
        "   ",
        "a" * 5000,
        " ".join(f"w{i}" for i in range(3000)),
    ],
)
def test_keyword_search_never_raises_on_user_text(indexed, hostile):
    result = store.keyword_search(indexed, hostile, 5)
    assert isinstance(result, list)
    assert count(indexed, "chunks") == len(CHUNKS)  # nothing was dropped


def test_user_text_becomes_quoted_phrases_joined_with_or():
    assert (
        store._fts_query('seal "kit" -50 OR') == '"seal" OR """kit""" OR "-50" OR "OR"'
    )
    assert store._fts_query("E-214") == '"E-214"'
    assert store._fts_query("a\x00b") == '"a" OR "b"'


@pytest.mark.parametrize("text", ["", "   ", '"', "-", "_", "!!! --- ___", "()"])
def test_user_text_without_a_letter_or_digit_makes_no_query(text):
    assert store._fts_query(text) is None


def test_keyword_search_finds_the_seal_chunks_for_the_hostile_example(indexed):
    hits = store.keyword_search(indexed, 'seal "kit" -50 OR', 5)
    assert [hit["chunk_id"] for hit in hits[:2]] == ["c2", "c1"]


def test_keyword_search_treats_operators_as_plain_words(indexed):
    assert store.keyword_search(indexed, "chunk_id:c1", 5) == []
    assert store.keyword_search(indexed, "NOT", 5) == []
    assert store.keyword_search(indexed, "!!!", 5) == []


# --- vector search ----------------------------------------------------------


@pytest.fixture
def vectors(conn):
    add_document(conn)
    data = {
        "same": [1.0, 0.0, 0.0],
        "near": [0.8, 0.6, 0.0],
        "orthogonal": [0.0, 1.0, 0.0],
        "opposite": [-1.0, 0.0, 0.0],
    }
    for order, (chunk_id, vector) in enumerate(data.items()):
        store.add_chunk(conn, chunk_id, DOC, 1, order, chunk_id, vec(*vector))
    store.add_chunk(conn, "none", DOC, 1, 9, "no embedding", None)
    return conn


def test_vector_search_ranks_by_cosine_similarity(vectors):
    hits = store.vector_search(vectors, vec(1.0), 4)
    assert [hit["chunk_id"] for hit in hits] == [
        "same",
        "near",
        "orthogonal",
        "opposite",
    ]
    assert [hit["score"] for hit in hits] == pytest.approx([1.0, 0.8, 0.0, -1.0])
    top = hits[0]
    assert (top["doc_id"], top["page"], top["ord"], top["text"]) == (DOC, 1, 0, "same")


def test_vector_search_ignores_vector_length_and_chunks_without_embeddings(vectors):
    store.add_chunk(vectors, "long", DOC, 1, 10, "long", vec(30.0))
    hits = store.vector_search(vectors, vec(5.0), 10)
    ids = [hit["chunk_id"] for hit in hits]
    assert "none" not in ids
    assert {"same", "long"} == set(ids[:2])
    assert hits[0]["score"] == pytest.approx(1.0)


def test_vector_search_respects_k(vectors):
    hits = store.vector_search(vectors, vec(0.8, 0.6), 2)
    assert [hit["chunk_id"] for hit in hits] == ["near", "same"]
    assert store.vector_search(vectors, vec(1.0), 0) == []


def test_vector_search_with_no_embeddings_is_empty(conn):
    add_document(conn)
    store.add_chunk(conn, "c1", DOC, 1, 0, "text", None)
    assert store.vector_search(conn, vec(1.0), 3) == []


def test_vector_search_gives_a_zero_vector_a_score_of_zero_not_nan(vectors):
    store.add_chunk(vectors, "zero", DOC, 1, 11, "zero", vec())
    hits = store.vector_search(vectors, vec(1.0), 10)
    assert {hit["chunk_id"]: hit["score"] for hit in hits}["zero"] == 0.0
    flat = store.vector_search(vectors, vec(), 2)  # a zero query scores 0 too
    assert [hit["score"] for hit in flat] == [0.0, 0.0]


def test_vector_search_rejects_a_query_of_the_wrong_size(vectors):
    with pytest.raises(ValueError, match="query has 2 dimensions.*have 384"):
        store.vector_search(vectors, np.array([1.0, 0.0]), 3)


# --- networkx export --------------------------------------------------------


def test_load_networkx_returns_a_multidigraph_with_attributes(conn):
    graph(conn)
    g = store.load_networkx(conn)
    assert isinstance(g, nx.MultiDiGraph)
    assert set(g.nodes) == {
        "LINE-01",
        "CNC-01",
        "CNC-01-SPINDLE",
        "E-214",
        "FM-LOW-COOLANT",
    }
    assert g.nodes["CNC-01"]["type"] == "Asset"
    assert g.nodes["CNC-01"]["label"] == "CNC lathe 1"
    assert g.nodes["CNC-01"]["props"]["model"] == "KL-300"

    assert g.number_of_edges() == 4
    data = g.get_edge_data("CNC-01", "E-214")
    assert list(data) == ["CNC-01|RAISED|E-214"]  # the edge id is the key
    assert data["CNC-01|RAISED|E-214"]["rel"] == "RAISED"
    assert data["CNC-01|RAISED|E-214"]["props"] == RAISED
    assert g.has_edge("E-214", "FM-LOW-COOLANT")
    assert not g.has_edge("FM-LOW-COOLANT", "E-214")  # edges keep their direction


def test_load_networkx_of_an_empty_store_is_an_empty_graph(conn):
    g = store.load_networkx(conn)
    assert isinstance(g, nx.MultiDiGraph)
    assert g.number_of_nodes() == 0
