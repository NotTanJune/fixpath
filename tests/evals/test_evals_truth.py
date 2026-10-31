"""Tests for loading the hidden truth and the graph under test into typed objects."""

import hashlib
import sqlite3

import pytest
from eval_stubs import edge, node, write_kg, write_truth

from app.evals.truth import load_graph, load_truth


def test_the_truth_loads_nodes_edges_and_incident_details(tmp_path):
    folder = write_truth(
        tmp_path / "truth",
        [
            node("HP-01", "Asset", name="Press"),
            node("INC-2026-001", "Incident", date="2026-04-14", downtime_min=60),
        ],
        [edge("INC-2026-001", "OCCURRED_ON", "HP-01")],
        incidents_detail=[
            {
                "id": "INC-2026-001",
                "start": "2026-04-14T15:38:00+08:00",
                "procedure_id": "PROC-HP-04",
                "parts": [{"part_no": "SEAL-KIT-50", "qty": 1}],
                "technician": "Nur Aisyah",
            }
        ],
        technicians=[
            {"name": "Nur Aisyah", "shift": "B", "short_forms": ["Nur", "NA"]}
        ],
    )
    truth = load_truth(folder)
    assert truth.nodes["HP-01"].type == "Asset"
    assert truth.nodes["HP-01"].props == {"name": "Press"}
    assert [(e.src, e.rel, e.dst) for e in truth.edges] == [
        ("INC-2026-001", "OCCURRED_ON", "HP-01")
    ]
    detail = truth.incident_details["INC-2026-001"]
    assert detail.start == "2026-04-14T15:38:00+08:00"
    assert detail.technician == "Nur Aisyah"
    assert detail.parts == (("SEAL-KIT-50", 1),)
    assert truth.technicians[0].short_forms == ("Nur", "NA")


def test_the_mention_keys_lose_their_raw_prefix_to_match_the_graph_doc_paths(tmp_path):
    folder = write_truth(
        tmp_path / "truth",
        [],
        [],
        mentions={
            "raw/handovers/HO 04-23 B shift.txt": {
                "alarm_codes": ["E-217"],
                "assets": ["HP-01", "LT-01"],
                "conflicts": [],
                "incidents": ["INC-2026-009"],
                "parts": ["SEAL-KIT-50"],
            },
            "raw/manuals/BH-250.pdf": {
                "alarm_codes": [],
                "assets": ["HP-01"],
                "conflicts": [],
                "failure_modes": ["FM-SEAL-WEAR"],
                "incidents": [],
                "parts": [],
                "procedures": ["PROC-HP-04"],
            },
        },
    )
    truth = load_truth(folder)
    assert set(truth.mentions) == {
        "handovers/HO 04-23 B shift.txt",
        "manuals/BH-250.pdf",
    }
    note = truth.mentions["handovers/HO 04-23 B shift.txt"]
    assert note.assets == ("HP-01", "LT-01")
    assert note.incidents == ("INC-2026-009",)
    assert note.failure_modes is None and note.procedures is None
    manual = truth.mentions["manuals/BH-250.pdf"]
    assert manual.failure_modes == ("FM-SEAL-WEAR",)
    assert manual.procedures == ("PROC-HP-04",)


def test_a_typed_view_by_node_type_and_the_incident_facts(tmp_path):
    folder = write_truth(
        tmp_path / "truth",
        [
            node("HP-01", "Asset"),
            node("FM-A", "FailureMode"),
            node("INC-1", "Incident"),
        ],
        [edge("INC-1", "OCCURRED_ON", "HP-01"), edge("INC-1", "ROOT_CAUSE", "FM-A")],
    )
    truth = load_truth(folder)
    assert sorted(truth.of_type("Asset")) == ["HP-01"]
    assert truth.machine_of("INC-1") == "HP-01"
    assert truth.root_cause_of("INC-1") == "FM-A"
    assert truth.machine_of("INC-9") is None


def test_the_graph_loads_nodes_edges_documents_and_evidence(tmp_path):
    kg = write_kg(
        tmp_path / "kg.sqlite",
        [
            node("HP-01", "Asset", name="Press"),
            node("E-217", "AlarmCode", severity="alarm"),
        ],
        [edge("HP-01", "RAISED", "E-217", count=4)],
        documents={"logs/HP01.log": "log"},
        node_evidence={"HP-01": [("logs/HP01.log", "HP-01 started")]},
        edge_evidence={
            ("HP-01", "RAISED", "E-217"): [("logs/HP01.log", "E-217 raised", 0.9)]
        },
    )
    graph = load_graph(kg)
    assert graph.nodes["HP-01"].props == {"name": "Press"}
    assert graph.nodes["HP-01"].label == "Press"
    assert [(e.src, e.rel, e.dst, e.props) for e in graph.edges] == [
        ("HP-01", "RAISED", "E-217", {"count": 4})
    ]
    assert graph.documents == {"logs/HP01.log": "log"}
    item = graph.node_evidence["HP-01"][0]
    assert (item.doc_path, item.quote, item.extractor) == (
        "logs/HP01.log",
        "HP-01 started",
        "csv-parser",
    )
    edge_item = graph.edge_evidence[("HP-01", "RAISED", "E-217")][0]
    assert (edge_item.doc_path, edge_item.confidence) == ("logs/HP01.log", 0.9)
    assert graph.of_type("Asset").keys() == {"HP-01"}


def test_the_graph_is_opened_read_only_and_a_missing_file_is_not_created(tmp_path):
    kg = write_kg(tmp_path / "kg.sqlite", [node("HP-01", "Asset")], [])
    before = hashlib.sha256(kg.read_bytes()).hexdigest()
    load_graph(kg)
    assert hashlib.sha256(kg.read_bytes()).hexdigest() == before
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        load_graph(missing)
    assert not missing.exists()
