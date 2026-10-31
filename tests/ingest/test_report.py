"""Tests for the pipeline report, which lists each file processed and the totals, and
for the ledger helpers it uses. The input is hand-made, with no network or model."""

import json

from app import ledger
from app.ingest.report import (
    build_report,
    ledger_mark,
    ledger_rows_since,
    write_report,
)
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    Extraction,
    MergeDecision,
    NodeRef,
    Resolution,
    ResolvedEdge,
    ResolvedNode,
    ReviewItem,
)
from app.ingest.validate import Flag, Validated
from llm_stubs import isolated  # noqa: F401
from load_stubs import ev

LOG = "logs/HP01.log"
HO = "handovers/h.txt"


def cand(type_, key):
    return CandidateNode(
        ref=NodeRef(type=type_, key=key), label=key, evidence=ev(LOG, "q")
    )


def cedge(src, rel, dst):
    return CandidateEdge(
        src=NodeRef(type=src[0], key=src[1]),
        rel=rel,
        dst=NodeRef(type=dst[0], key=dst[1]),
    )


def extractions():
    return [
        Extraction(
            doc_path=LOG,
            doc_type="log",
            extractor="log-parser",
            nodes=[
                cand("Asset", "HP-01"),
                cand("AlarmCode", "E-217"),
                cand("AlarmCode", "E-218"),
            ],
            edges=[cedge(("Asset", "HP-01"), "RAISED", ("AlarmCode", "E-217"))],
            notes=["2 of 10 lines could not be parsed"],
        ),
        Extraction(doc_path=HO, doc_type="handover", extractor="sol-text"),
    ]


def rnode(node_id, type_, doc):
    return ResolvedNode(
        id=node_id, type=type_, label=node_id, evidence=ev(doc, "q"), sources=[doc]
    )


def resolution():
    return Resolution(
        nodes=[
            rnode("E-217", "AlarmCode", LOG),
            rnode("HP-01", "Asset", LOG),
            rnode("INC-2026-001", "Incident", HO),
        ],
        edges=[
            ResolvedEdge(src="HP-01", rel="RAISED", dst="E-217", evidence=ev(LOG, "q"))
        ],
        merges=[
            MergeDecision(
                doc_path=LOG,
                type="Asset",
                mention="press",
                candidate="HP-01",
                p=0.9,
                decision="merge",
                backend="jev",
            ),
            MergeDecision(
                doc_path=LOG,
                type="Asset",
                mention="HP1",
                candidate="HP-01",
                decision="rule",
            ),
            MergeDecision(
                doc_path=HO, type="Part", mention="seal", decision="unresolved"
            ),
        ],
        review=[
            ReviewItem(
                kind="merge",
                doc_paths=[HO],
                payload={"mention": "seal"},
                confidence=0.5,
            ),
            ReviewItem(kind="conflict", doc_paths=[LOG, HO], payload={"reason": "x"}),
        ],
    )


def validated():
    return Validated(
        nodes=[rnode("E-217", "AlarmCode", LOG), rnode("HP-01", "Asset", LOG)],
        edges=[
            ResolvedEdge(src="HP-01", rel="RAISED", dst="E-217", evidence=ev(LOG, "q"))
        ],
        flags=[
            Flag(rule="bad_id", severity="error", subject="INC-2026-001", detail="d"),
            Flag(rule="no_edges", severity="warning", subject="E-217", detail="d"),
            Flag(rule="no_evidence", severity="error", subject="NOWHERE", detail="d"),
        ],
    )


ROWS = [
    {
        "model": "gpt-6.1-sol",
        "purpose": "extract",
        "input_tokens": 1000,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "usd": 0.003,
        "cache_hit": 0,
    },
    {
        "model": "gpt-6.1-sol",
        "purpose": "extract",
        "input_tokens": 1000,
        "cached_input_tokens": 0,
        "output_tokens": 100,
        "usd": 0.0,
        "cache_hit": 1,
    },
    {
        "model": "jev-1.14.0",
        "purpose": "link root cause",
        "input_tokens": 500,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "usd": 0.0,
        "cache_hit": 1,
    },
]


def report():
    return build_report(extractions(), resolution(), validated(), ROWS)


def test_the_report_is_plain_json():
    assert json.loads(json.dumps(report())) == report()


def test_each_file_has_its_candidate_counts_by_type_and_its_notes():
    log, handover = report()["files"]
    assert (log["doc_path"], log["doc_type"], log["extractor"]) == (
        LOG,
        "log",
        "log-parser",
    )
    assert log["candidates"] == {
        "nodes": {"AlarmCode": 2, "Asset": 1},
        "edges": {"RAISED": 1},
        "chunks": 0,
    }
    assert log["notes"] == ["2 of 10 lines could not be parsed"]
    assert handover["candidates"] == {"nodes": {}, "edges": {}, "chunks": 0}


def test_each_file_lists_its_merge_decisions_review_items_and_flags():
    log, handover = report()["files"]
    assert [m["decision"] for m in log["merges"]] == ["merge", "rule"]
    assert log["merges"][0]["mention"] == "press"
    assert [m["decision"] for m in handover["merges"]] == ["unresolved"]
    assert [r["kind"] for r in log["review"]] == ["conflict"]
    assert [r["kind"] for r in handover["review"]] == ["merge", "conflict"]
    assert [(f["rule"], f["subject"]) for f in log["flags"]] == [("no_edges", "E-217")]
    assert [(f["rule"], f["subject"]) for f in handover["flags"]] == [
        ("bad_id", "INC-2026-001")
    ]


def test_flags_whose_item_has_no_known_file_are_listed_apart():
    assert [f["subject"] for f in report()["unassigned_flags"]] == ["NOWHERE"]


def test_the_totals_count_the_validated_nodes_and_edges():
    totals = report()["totals"]
    assert totals["nodes"] == {"AlarmCode": 1, "Asset": 1}
    assert totals["edges"] == {"RAISED": 1}


def test_the_totals_count_review_items_by_kind_and_merges_by_band():
    totals = report()["totals"]
    assert totals["review"] == {"conflict": 1, "merge": 1}
    assert totals["merges"] == {"merge": 1, "rule": 1, "unresolved": 1}


def test_the_totals_count_the_flags():
    flags = report()["totals"]["flags"]
    assert flags == {
        "error": 2,
        "warning": 1,
        "by_rule": {"bad_id": 1, "no_edges": 1, "no_evidence": 1},
    }


def test_the_cost_comes_from_the_ledger_rows_of_the_run():
    cost = report()["totals"]["cost"]
    assert cost["calls"] == 3
    assert cost["cache_hits"] == 2
    assert cost["paid_calls"] == 1
    assert cost["usd"] == 0.003
    assert cost["input_tokens"] == 2500
    assert cost["output_tokens"] == 200
    assert cost["by_purpose"] == {
        "extract": {"calls": 2, "cache_hits": 1, "usd": 0.003},
        "link root cause": {"calls": 1, "cache_hits": 1, "usd": 0.0},
    }


def test_a_run_with_no_ledger_rows_costs_nothing():
    cost = build_report([], Resolution(), Validated(), [])["totals"]["cost"]
    assert cost == {
        "calls": 0,
        "cache_hits": 0,
        "paid_calls": 0,
        "usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "by_purpose": {},
    }


def test_the_report_is_the_same_for_the_same_inputs():
    assert json.dumps(report(), sort_keys=True) == json.dumps(report(), sort_keys=True)


def test_write_report_writes_sorted_keys_with_indent_2_and_a_trailing_newline(tmp_path):
    path = write_report(
        {"b": 1, "a": {"d": 2, "c": 3}}, tmp_path / "deeper" / "latest.json"
    )
    assert path.read_text(encoding="utf-8") == (
        '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}\n'
    )


def test_ledger_rows_since_returns_only_the_rows_after_the_mark():
    assert ledger_mark() == 0
    assert ledger_rows_since(0) == []
    ledger.record("gpt-6.1-sol", "before", 10, 0, 10, False)
    mark = ledger_mark()
    ledger.record("gpt-6.1-sol", "after one", 20, 0, 5, False)
    ledger.record("gpt-6.1-sol", "after two", 30, 0, 5, True)
    rows = ledger_rows_since(mark)
    assert [r["purpose"] for r in rows] == ["after one", "after two"]
    assert [r["cache_hit"] for r in rows] == [0, 1]
