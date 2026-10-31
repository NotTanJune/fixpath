"""Tests for layer 1, which measures how much of the hidden truth the graph recovers."""

import pytest
from eval_stubs import edge, make_graph, make_truth, mentions, node

from app.evals.layer1 import (
    document_metrics,
    edge_metrics,
    evaluate,
    node_metrics,
    property_accuracy,
)
from app.evals.match import match
from app.evals.metrics import prf


def test_precision_recall_and_f1_from_known_counts():
    result = prf(tp=6, fp=2, fn=3)
    assert result["precision"] == pytest.approx(0.75)
    assert result["recall"] == pytest.approx(6 / 9)
    assert result["f1"] == pytest.approx(2 * 6 / (2 * 6 + 2 + 3))
    assert (result["tp"], result["fp"], result["fn"]) == (6, 2, 3)


def test_a_perfect_score_and_the_undefined_cases():
    assert prf(5, 0, 0)["f1"] == 1.0
    nothing_predicted = prf(0, 0, 4)
    assert nothing_predicted["precision"] is None
    assert nothing_predicted["recall"] == 0.0
    assert nothing_predicted["f1"] == 0.0
    nothing_true = prf(0, 3, 0)
    assert nothing_true["recall"] is None
    assert nothing_true["precision"] == 0.0
    empty = prf(0, 0, 0)
    assert (empty["precision"], empty["recall"], empty["f1"]) == (None, None, None)


def small_world():
    truth = make_truth(
        [
            node("HP-01", "Asset"),
            node("CNC-01", "Asset"),
            node("E-1", "AlarmCode", severity="alarm"),
            node("P-1", "Part", qty_on_hand=6, bin="D-13", stock_as_of="2026-10-15"),
            node("P-2", "Part", qty_on_hand=2, bin="A-01", stock_as_of="2026-10-15"),
        ],
        [
            edge("HP-01", "RAISED", "E-1", count=4),
            edge("CNC-01", "RAISED", "E-1", count=2),
        ],
    )
    graph = make_graph(
        [
            node("HP-01", "Asset"),
            node("E-1", "AlarmCode", severity="warning"),
            node("P-1", "Part", qty_on_hand=6.0, bin="D-14", stock_as_of=None),
            node("P-9", "Part"),
        ],
        [edge("HP-01", "RAISED", "E-1", count=5), edge("HP-01", "RAISED", "P-9")],
    )
    return truth, graph


def test_node_precision_and_recall_per_type():
    truth, graph = small_world()
    result = node_metrics(truth, graph, match(truth, graph))
    assert (result["Asset"]["tp"], result["Asset"]["fp"], result["Asset"]["fn"]) == (
        1,
        0,
        1,
    )
    assert (result["Part"]["tp"], result["Part"]["fp"], result["Part"]["fn"]) == (
        1,
        1,
        1,
    )
    assert result["AlarmCode"]["f1"] == 1.0


def test_edges_map_through_endpoints_and_an_unmatched_endpoint_is_a_false_positive():
    truth, graph = small_world()
    result = edge_metrics(truth, graph, match(truth, graph))
    raised = result["RAISED"]
    assert (raised["tp"], raised["fp"], raised["fn"]) == (1, 1, 1)


def test_property_accuracy_counts_correct_wrong_and_missing_on_matched_nodes():
    truth, graph = small_world()
    result = property_accuracy(truth, graph, match(truth, graph))
    quantity = result["Part.qty_on_hand"]
    assert (
        quantity["n"],
        quantity["correct"],
        quantity["wrong"],
        quantity["missing"],
    ) == (1, 1, 0, 0)
    assert result["Part.bin"]["wrong"] == 1
    assert result["Part.stock_as_of"]["missing"] == 1
    assert result["AlarmCode.severity"]["accuracy"] == 0.0
    assert result["RAISED.count"]["wrong"] == 1
    assert result["RAISED.count"]["n"] == 1
    # total recall of the events: the graph counts 5 on the one matched edge, the truth 4 + 2
    assert result["RAISED.count"]["events"] == {
        "graph": 5,
        "truth": 6,
        "recall": pytest.approx(5 / 6),
    }


def test_incident_properties_count_a_missing_shift_apart_and_downtime_has_a_30_minute_band():
    machine = [node("HP-01", "Asset")]
    truth = make_truth(
        [
            *machine,
            node(
                "T-1",
                "Incident",
                date="2026-04-18",
                shift="A",
                downtime_min=100,
                symptom_text="x",
            ),
            node(
                "T-2",
                "Incident",
                date="2026-05-18",
                shift="B",
                downtime_min=100,
                symptom_text="y",
            ),
            node(
                "T-3",
                "Incident",
                date="2026-06-18",
                shift="C",
                downtime_min=100,
                symptom_text="z",
            ),
        ],
        [edge(i, "OCCURRED_ON", "HP-01") for i in ("T-1", "T-2", "T-3")],
    )
    graph = make_graph(
        [
            *machine,
            node(
                "G-1",
                "Incident",
                date="2026-04-18",
                shift=None,
                downtime_min=100,
                symptom_text="x",
            ),
            node(
                "G-2",
                "Incident",
                date="2026-05-18",
                shift="C",
                downtime_min=125,
                symptom_text="y",
            ),
            node(
                "G-3",
                "Incident",
                date="2026-06-18",
                shift="C",
                downtime_min=190,
                symptom_text="z",
            ),
        ],
        [edge(i, "OCCURRED_ON", "HP-01") for i in ("G-1", "G-2", "G-3")],
    )
    result = property_accuracy(truth, graph, match(truth, graph))
    shift = result["Incident.shift"]
    assert (shift["n"], shift["correct"], shift["wrong"], shift["missing"]) == (
        3,
        1,
        1,
        1,
    )
    downtime = result["Incident.downtime_min"]
    assert (downtime["correct"], downtime["within_30_count"], downtime["wrong"]) == (
        1,
        2,
        2,
    )
    assert downtime["within_30"] == pytest.approx(2 / 3)
    assert "within_30" not in result["Incident.shift"]
    assert result["Incident.date"]["accuracy"] == 1.0


def test_used_and_indicates_properties_are_compared_on_matched_edges():
    truth = make_truth(
        [node("P-1", "Part"), node("A", "AlarmCode")],
        [edge("A", "INDICATES", "P-1", support=3)],
    )
    graph = make_graph(
        [node("P-1", "Part"), node("A", "AlarmCode")],
        [edge("A", "INDICATES", "P-1", support=3)],
    )
    # INDICATES between these types is not valid, but the comparison only needs ids
    result = property_accuracy(truth, graph, match(truth, graph))
    assert result["INDICATES.support"]["correct"] == 1
    assert result["USED.qty"]["n"] == 0
    assert result["USED.qty"]["accuracy"] is None


def test_documents_are_scored_by_the_ids_their_evidence_names():
    nodes_t = [
        node("HP-01", "Asset"),
        node("CNC-01", "Asset"),
        node("E-1", "AlarmCode"),
        node("P-1", "Part"),
        node("FM-T", "FailureMode", name="Seal wear"),
        node("PROC-1", "Procedure"),
    ]
    truth = make_truth(
        nodes_t,
        [edge("FM-T", "RESOLVED_BY", "PROC-1")],
        doc_mentions={
            "logs/a.log": mentions(assets=["HP-01"], alarm_codes=["E-1"]),
            "logs/b.log": mentions(assets=["CNC-01"], alarm_codes=["E-1"]),
            "manuals/m.pdf": mentions(
                assets=["HP-01"],
                parts=["P-1"],
                failure_modes=["FM-T"],
                procedures=["PROC-1"],
            ),
        },
    )
    graph = make_graph(
        [
            node("HP-01", "Asset"),
            node("CNC-01", "Asset"),
            node("E-1", "AlarmCode"),
            node("P-1", "Part"),
            node("FM-G", "FailureMode", name="Seal wear"),
            node("PROC-1", "Procedure"),
            node("INC-1", "Incident"),
        ],
        [edge("FM-G", "RESOLVED_BY", "PROC-1"), edge("HP-01", "RAISED", "E-1")],
        documents={"logs/a.log": "log", "logs/b.log": "log", "manuals/m.pdf": "manual"},
        node_evidence={
            "HP-01": [("logs/a.log", "q"), ("manuals/m.pdf", "q")],
            "CNC-01": [("logs/a.log", "q")],
            "P-1": [("manuals/m.pdf", "q")],
            "INC-1": [("logs/a.log", "q")],
            "FM-G": [("manuals/m.pdf", "q")],
        },
        edge_evidence={
            ("HP-01", "RAISED", "E-1"): [("logs/a.log", "q", 0.9)],
            ("FM-G", "RESOLVED_BY", "PROC-1"): [("manuals/m.pdf", "q", 0.9)],
        },
    )
    result = document_metrics(truth, graph, match(truth, graph))
    log = result["log"]
    assert log["docs"] == 2
    # a.log: assets HP-01 ok, CNC-01 extra; alarm E-1 ok. b.log: asset CNC-01 missing, E-1 missing
    assert (log["assets"]["tp"], log["assets"]["fp"], log["assets"]["fn"]) == (1, 1, 1)
    assert (log["alarm_codes"]["tp"], log["alarm_codes"]["fn"]) == (1, 1)
    assert "failure_modes" not in log
    manual = result["manual"]
    assert manual["docs"] == 1
    assert manual["failure_modes"]["tp"] == 1
    assert manual["procedures"]["tp"] == 1
    assert manual["parts"]["recall"] == 1.0


def test_the_maintenance_export_is_reported_apart_from_the_loose_notes():
    truth = make_truth(
        [node("HP-01", "Asset"), node("CNC-01", "Asset"), node("P-1", "Part")],
        [],
        doc_mentions={
            "maintenance/export.csv": mentions(assets=["HP-01"], parts=["P-1"]),
            "maintenance/note_a.txt": mentions(assets=["CNC-01"], parts=[]),
            "maintenance/note_b.txt": mentions(assets=[], parts=[]),
        },
    )
    graph = make_graph(
        [node("HP-01", "Asset"), node("CNC-01", "Asset"), node("P-1", "Part")],
        [edge("HP-01", "USED", "P-1")],
        documents={
            "maintenance/export.csv": "maintenance_note",
            "maintenance/note_a.txt": "maintenance_note",
            "maintenance/note_b.txt": "maintenance_note",
        },
        edge_evidence={
            ("HP-01", "USED", "P-1"): [
                ("maintenance/export.csv", "q", 0.9, "csv-parser"),
                ("maintenance/note_a.txt", "q", 0.8, "sol-text"),
            ],
        },
    )
    result = document_metrics(truth, graph, match(truth, graph))
    assert set(result) == {"maintenance_export", "maintenance_note"}
    export, notes = result["maintenance_export"], result["maintenance_note"]
    assert export["docs"] == 1 and notes["docs"] == 2
    assert (export["assets"]["tp"], export["parts"]["tp"]) == (1, 1)
    # the loose note names HP-01 and P-1 that the truth does not give it, and misses CNC-01
    assert (notes["assets"]["tp"], notes["assets"]["fp"], notes["assets"]["fn"]) == (
        0,
        1,
        1,
    )


def test_evaluate_gathers_every_table_and_what_stayed_unmatched():
    truth, graph = small_world()
    result = evaluate(truth, graph)
    assert set(result) == {"nodes", "edges", "properties", "documents", "matching"}
    assert result["matching"]["Part"] == {
        "matched": 1,
        "unmatched_graph": ["P-9"],
        "unmatched_truth": ["P-2"],
    }
