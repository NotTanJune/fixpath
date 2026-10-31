"""Tests for the crews section of the results file: accuracy, time and cost of each
crew step. Each test changes one input and checks that the output moves."""

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from app.evals import crews

MANUAL_CATEGORIES = ("alarm_codes", "assets", "failure_modes", "parts", "procedures")


def counts(tp, fn=0, fp=0):
    return {"tp": tp, "fn": fn, "fp": fp}


def results():
    return {
        "layer1": {
            "documents": {
                "log": {
                    "docs": 5,
                    "alarm_codes": counts(26),
                    "assets": counts(5),
                    "parts": counts(0),
                },
                "inventory": {
                    "docs": 2,
                    "alarm_codes": counts(0),
                    "assets": counts(5),
                    "parts": counts(78, fn=2),
                },
                "maintenance_export": {
                    "docs": 1,
                    "alarm_codes": counts(16),
                    "assets": counts(5),
                    "parts": counts(22, fp=1),
                },
                "handover": {
                    "docs": 90,
                    "alarm_codes": counts(40),
                    "assets": counts(42, fn=109),
                    "parts": counts(56, fn=11),
                },
                "maintenance_note": {
                    "docs": 5,
                    "alarm_codes": counts(5),
                    "assets": counts(5),
                    "parts": counts(18),
                },
                "manual": {
                    "docs": 4,
                    **{
                        name: counts(10 + i) for i, name in enumerate(MANUAL_CATEGORIES)
                    },
                },
            }
        },
        "layer2": {
            "same_entity": {
                "at_gate": {"tp": 17, "fn": 0, "fp": 0, "precision": 1.0, "gate": 0.7}
            },
            "same_incident": {
                "decision_level": {
                    "at_gate": {
                        "tp": 41,
                        "fn": 1,
                        "fp": 0,
                        "precision": 1.0,
                        "gate": 0.9,
                    }
                }
            },
            "root_cause": {
                "at_gate": {"tp": 60, "fn": 0, "fp": 0, "precision": 1.0, "gate": 0.75}
            },
        },
        "layer3": {
            "all_fields": {"correct": 29, "n": 32, "accuracy": 29 / 32},
            "on_act": {"all_fields": {"correct": 22, "n": 22, "accuracy": 1.0}},
        },
        "layer4": {
            "root_cause": {"top1": {"correct": 20, "n": 20, "accuracy": 1.0}},
            "judge": {
                "citation_precision": {"kept": 102, "supported": 96, "precision": 0.94},
                "verifier_agreement": {"agree": 96, "n": 102, "rate": 0.94},
            },
            "lockout": {
                "caught": 19,
                "n": 19,
                "recall": 1.0,
                "over_warning": {"warned": 1, "negatives": 1, "rate": 1.0},
            },
            "fix": {"shown": {"correct": 20, "n": 20, "accuracy": 1.0}},
            "parts": {"correct": 20, "n": 20, "accuracy": 1.0},
            "traps_passed": {"passed": 5, "n": 5},
        },
        "layer5": {
            "live": {
                "measured_at": "2026-10-28",
                "answers": 25,
                "cost_per_answer": {
                    "mean": 0.0088,
                    "p50": 0.0098,
                    "p95": 0.0114,
                    "total": 0.22,
                },
                "latency": {
                    "answer": {"n": 25, "p50": 13.6, "p95": 15.5},
                    "nodes": {
                        "assemble": {"n": 25, "p50": 0.006, "p95": 0.008},
                        "graph_agent": {"n": 22, "p50": 8.5, "p95": 9.7},
                        "manual_agent": {"n": 22, "p50": 2.5, "p95": 4.3},
                        "synthesize": {"n": 22, "p50": 4.7, "p95": 5.8},
                        "triage": {"n": 25, "p50": 0.29, "p95": 0.39},
                        "verify": {"n": 22, "p50": 0.32, "p95": 0.36},
                    },
                },
            }
        },
    }


def report():
    return {
        "files": [
            {
                "doc_path": "handovers/a.txt",
                "doc_type": "handover",
                "extractor": "sol-text",
            },
            {
                "doc_path": "handovers/b.txt",
                "doc_type": "handover",
                "extractor": "sol-text",
            },
            {
                "doc_path": "maintenance/n.txt",
                "doc_type": "maintenance_note",
                "extractor": "sol-text",
            },
            {
                "doc_path": "manuals/m.pdf",
                "doc_type": "manual",
                "extractor": "sol-manual",
            },
        ],
        "run": {
            "loaded": {
                "chunks": 128,
                "documents": 107,
                "edges": 442,
                "evidence": 906,
                "nodes": 164,
                "review": 37,
            }
        },
        "totals": {
            "flags": {"error": 0, "warning": 20},
            "cost": {
                "by_purpose": {
                    "merge Part": {"calls": 31},
                    "merge Incident": {"calls": 111},
                    "link root cause": {"calls": 61},
                }
            },
        },
    }


DOCUMENTS = [
    ("handovers/a.txt", "handover"),
    ("logs/x.log", "log"),
    ("maintenance/n.txt", "maintenance_note"),
    ("manuals/m.pdf", "manual"),
]


def ledger_rows():
    def row(purpose, usd, ts, hit=0):
        return {"purpose": purpose, "usd": usd, "cache_hit": hit, "ts": ts}

    return [
        row("extract handover", 0.002, "2026-10-28T08:00:00+00:00"),
        row("extract handover", 0.004, "2026-10-29T08:00:00+00:00"),
        row("extract handover", 0.0, "2026-10-29T09:00:00+00:00", hit=1),
        row("extract note", 0.004, "2026-10-28T08:00:00+00:00"),
        row("extract manual", 0.02, "2026-10-28T08:00:00+00:00"),
        row("merge Part", 0.00004, "2026-10-28T09:00:00+00:00"),
        row("merge Incident", 0.00002, "2026-10-28T09:00:00+00:00"),
        row("merge Incident", 0.00002, "2026-10-28T09:00:00+00:00"),
        row("link root cause", 0.00003, "2026-10-28T09:00:00+00:00"),
    ]


def timing():

    def node(seconds, usd):
        return {"seconds": seconds, "usd": usd}

    return {
        "measured_at": "2026-10-29",
        "ingestion": {
            "router": {
                "files": [
                    {"doc_path": f"f{i}", "seconds": 0.001 * (i + 1)}
                    for i in range(107)
                ]
            },
            "parsers": {
                "files": [
                    {"doc_path": f"p{i}", "seconds": 0.1 * (i + 1)} for i in range(8)
                ]
            },
            "text_reader": {
                "files": [{"doc_path": f"t{i}", "seconds": 4.0 + i} for i in range(4)]
            },
            "manual_reader": {
                "files": [{"doc_path": "manuals/m.pdf", "seconds": 21.0}]
            },
            "resolver": {
                "seconds": 9.0,
                "cached": 3,
                "questions": [
                    {"seconds": 0.2 + 0.01 * i, "usd": 0.00003} for i in range(30)
                ],
            },
            "linker": {
                "seconds": 5.0,
                "cached": 0,
                "questions": [{"seconds": 0.3, "usd": 0.00003} for _ in range(10)],
            },
            "validator": {"seconds": 0.05},
            "loader": {"seconds": 1.5},
            "pipeline_seconds": 20.0,
        },
        "answers": [
            {
                "question": f"q{i}",
                "seconds": 14.0 + i,
                "nodes": {
                    "triage": node(0.3, 0.00001),
                    "graph_agent": node(8.0, 0.004),
                    "manual_agent": node(2.0, 0.001),
                    "synthesize": node(4.0, 0.005),
                    "verify": node(0.3, 0.00002),
                    "assemble": node(0.006, 0.0),
                },
            }
            for i in range(4)
        ],
    }


def build(**changes):
    inputs = {
        "results": results(),
        "report": report(),
        "documents": DOCUMENTS,
        "ledger": crews.ledger_means(ledger_rows()),
        "timing": timing(),
    }
    inputs.update(changes)
    return crews.build(**inputs)


def step(block, crew, step_id):
    (found,) = [s for s in block[crew]["steps"] if s["id"] == step_id]
    return found


# --- the ledger -----------------------------------------------------------------


def test_ledger_means_count_only_paid_calls_and_keep_the_date_range():
    means = crews.ledger_means(ledger_rows())
    handover = means["extract handover"]
    assert handover["n"] == 2
    assert handover["usd"] == pytest.approx(0.003)
    assert (handover["first"], handover["last"]) == ("2026-10-28", "2026-10-29")
    assert means["merge Incident"]["n"] == 2


def test_a_row_with_no_cost_is_not_a_paid_call():
    rows = [
        {"purpose": "x", "usd": 0.0, "cache_hit": 0, "ts": "2026-10-28T00:00:00+00:00"},
        {"purpose": "x", "usd": 0.5, "cache_hit": 1, "ts": "2026-10-28T00:00:00+00:00"},
    ]
    assert crews.ledger_means(rows) == {}


def test_a_mean_over_several_purposes_is_pooled_by_calls():
    means = crews.ledger_means(ledger_rows())
    pooled = crews.pooled(means, "merge Part", "merge Incident")
    assert pooled["n"] == 3
    assert pooled["usd"] == pytest.approx((0.00004 + 0.00002 * 2) / 3)


def test_the_ledger_is_read_read_only(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE calls(id INTEGER PRIMARY KEY, ts TEXT, model TEXT, purpose TEXT,"
            " input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,"
            " usd REAL, cache_hit INTEGER)"
        )
        conn.execute(
            "INSERT INTO calls(ts, model, purpose, input_tokens, cached_input_tokens,"
            " output_tokens, usd, cache_hit) VALUES ('2026-10-28T00:00:00+00:00', 'm', 'p',"
            " 1, 0, 1, 0.5, 0)"
        )
    before = path.read_bytes()
    rows = crews.read_ledger(path)
    assert [(r["purpose"], r["usd"]) for r in rows] == [("p", 0.5)]
    assert path.read_bytes() == before
    assert crews.read_ledger(tmp_path / "missing.sqlite") is None


# --- the ingestion crew -----------------------------------------------------------


def test_the_ingestion_steps_come_in_the_order_of_the_diagram():
    block = build()
    ids = [s["id"] for s in block["ingestion"]["steps"]]
    assert ids == [
        "router",
        "parsers",
        "text_reader",
        "manual_reader",
        "resolver",
        "linker",
        "validate_load",
    ]
    for entry in block["ingestion"]["steps"]:
        assert {"id", "name", "model", "does", "accuracy", "cost", "time"} <= set(entry)


def test_the_router_is_scored_by_documents_filed_as_their_folder_says():
    block = build()
    assert step(block, "ingestion", "router")["accuracy"]["k"] == 4
    assert step(block, "ingestion", "router")["accuracy"]["n"] == 4
    wrong = [("handovers/a.txt", "log"), *DOCUMENTS[1:]]
    accuracy = step(build(documents=wrong), "ingestion", "router")["accuracy"]
    assert (accuracy["k"], accuracy["n"]) == (3, 4)


def test_the_parsers_sum_found_over_expected_across_their_three_document_types():
    accuracy = step(build(), "ingestion", "parsers")["accuracy"]
    # log 26+5+0, inventory 0+5+78, export 16+5+22 found; 2 inventory parts missed
    assert accuracy["k"] == 26 + 5 + 5 + 78 + 16 + 5 + 22
    assert accuracy["n"] == accuracy["k"] + 2
    # one wrong part from the export
    assert accuracy["precision"] == pytest.approx(accuracy["k"] / (accuracy["k"] + 1))


def test_the_text_reader_counts_alarm_codes_and_parts_and_leaves_machines_out():
    entry = step(build(), "ingestion", "text_reader")
    accuracy = entry["accuracy"]
    assert accuracy["k"] == 40 + 56 + 5 + 18
    assert accuracy["n"] == accuracy["k"] + 11
    assert accuracy["precision"] == 1.0
    assert "machine" in entry["note"]
    changed = results()
    changed["layer1"]["documents"]["handover"]["assets"] = counts(1, fn=500)
    assert (
        step(build(results=changed), "ingestion", "text_reader")["accuracy"] == accuracy
    )


def test_the_manual_reader_counts_every_category_the_manuals_have():
    accuracy = step(build(), "ingestion", "manual_reader")["accuracy"]
    assert accuracy["k"] == sum(10 + i for i in range(5))
    assert accuracy["n"] == accuracy["k"]
    changed = results()
    changed["layer1"]["documents"]["manual"]["procedures"] = counts(14, fn=6)
    moved = step(build(results=changed), "ingestion", "manual_reader")["accuracy"]
    assert moved["n"] == accuracy["n"] + 6


def test_the_resolver_reports_both_gates_and_the_linker_the_root_cause_gate():
    resolver = step(build(), "ingestion", "resolver")
    assert (resolver["accuracy"]["k"], resolver["accuracy"]["n"]) == (17, 17)
    assert resolver["accuracy"]["precision"] == 1.0
    (also,) = resolver["also"]
    assert (also["k"], also["n"]) == (41, 42)
    linker = step(build(), "ingestion", "linker")["accuracy"]
    assert (linker["k"], linker["n"], linker["precision"]) == (60, 60, 1.0)


def test_the_validator_and_loader_report_the_counts_of_the_pipeline_report():
    entry = step(build(), "ingestion", "validate_load")
    assert entry["accuracy"] is None
    assert entry["outcome"] == {"errors": 0, "warnings": 20, "nodes": 164, "links": 442}
    changed = report()
    changed["totals"]["flags"]["error"] = 3
    assert (
        step(build(report=changed), "ingestion", "validate_load")["outcome"]["errors"]
        == 3
    )


def test_code_steps_cost_nothing_and_model_steps_cost_the_ledger_mean():
    block = build()
    for step_id in ("router", "parsers", "validate_load"):
        assert step(block, "ingestion", step_id)["cost"]["usd"] == 0
    text = step(block, "ingestion", "text_reader")["cost"]
    # the handover and note means, pooled over their paid calls
    assert text["usd"] == pytest.approx((0.002 + 0.004 + 0.004) / 3)
    assert text["n"] == 3
    assert text["per"] == "note"
    assert "ledger" in text["source"] and "28 Oct 2026" in text["source"]
    assert step(block, "ingestion", "manual_reader")["cost"]["usd"] == pytest.approx(
        0.02
    )
    resolver = step(block, "ingestion", "resolver")["cost"]
    assert resolver["usd"] == pytest.approx((0.00004 + 0.00002 * 2) / 3)
    assert resolver["per"] == "JEV question"
    assert resolver["step_usd"] == pytest.approx(30 * 0.00003)
    assert resolver["step_questions"] == 30


def test_the_time_of_each_step_is_computed_from_the_timing_file():
    block = build()
    router = step(block, "ingestion", "router")["time"]
    assert router["n"] == 107 and router["per"] == "file"
    assert router["p50_s"] == pytest.approx(0.054)
    assert router["p95_s"] > router["p50_s"]
    assert router["step_s"] == pytest.approx(sum(0.001 * (i + 1) for i in range(107)))
    text = step(block, "ingestion", "text_reader")["time"]
    # four notes are too few for a p95
    assert text["n"] == 4 and "p95_s" not in text
    assert text["p50_s"] == pytest.approx(5.5)
    assert "4 notes" in text["source"] and "29 Oct 2026" in text["source"]
    manual = step(block, "ingestion", "manual_reader")["time"]
    assert (manual["p50_s"], manual["n"], manual["per"]) == (21.0, 1, "manual")
    resolver = step(block, "ingestion", "resolver")["time"]
    assert resolver["n"] == 30 and resolver["per"] == "JEV question"
    assert resolver["step_s"] == 9.0
    assert "p95_s" in resolver
    both = step(block, "ingestion", "validate_load")["time"]
    assert both["step_s"] == pytest.approx(1.55)
    assert both["n"] == 1


def test_the_time_moves_when_the_timing_moves():
    slower = timing()
    slower["ingestion"]["manual_reader"]["files"][0]["seconds"] = 40.0
    assert (
        step(build(timing=slower), "ingestion", "manual_reader")["time"]["p50_s"]
        == 40.0
    )


def test_the_graph_totals_come_from_the_pipeline_report_and_the_ledger_means():
    graph = build()["ingestion"]["graph"]
    assert graph["documents"] == 107 and graph["nodes"] == 164 and graph["links"] == 442
    assert graph["chunks"] == 128 and graph["review_items"] == 37
    assert graph["validator_errors"] == 0 and graph["validator_warnings"] == 20
    assert graph["pipeline_s"] == 20.0
    estimate = graph["fresh_ingest_estimate"]
    expected = (
        2 * 0.003  # two handovers
        + 0.004  # one loose note
        + 0.02  # one manual
        + 31 * 0.00004
        + 111 * 0.00002
        + 61 * 0.00003
    )
    assert estimate["usd"] == pytest.approx(expected)
    assert estimate["usd"] > 0


# --- the troubleshooting crew --------------------------------------------------------


def test_the_troubleshooting_steps_come_in_the_order_of_the_diagram():
    block = build()
    ids = [s["id"] for s in block["troubleshooting"]["steps"]]
    assert ids == [
        "triage",
        "graph_agent",
        "manual_agent",
        "synthesize",
        "verify",
        "assemble",
    ]


def test_each_troubleshooting_step_reads_its_own_accuracy():
    block = build()
    triage = step(block, "troubleshooting", "triage")
    assert (triage["accuracy"]["k"], triage["accuracy"]["n"]) == (29, 32)
    (on_act,) = triage["also"]
    assert (on_act["k"], on_act["n"]) == (22, 22)
    graph_agent = step(block, "troubleshooting", "graph_agent")["accuracy"]
    assert (graph_agent["k"], graph_agent["n"]) == (20, 20)
    synthesize = step(block, "troubleshooting", "synthesize")["accuracy"]
    assert (synthesize["k"], synthesize["n"]) == (96, 102)
    verify = step(block, "troubleshooting", "verify")["accuracy"]
    assert (verify["k"], verify["n"]) == (96, 102)
    assemble = step(block, "troubleshooting", "assemble")
    assert (assemble["accuracy"]["k"], assemble["accuracy"]["n"]) == (19, 19)
    labels = {a["label"]: (a["k"], a["n"]) for a in assemble["also"]}
    assert sorted(labels.values()) == [(20, 20), (20, 20)]
    assert "lockout" in assemble["note"] and "1 of 1" in assemble["note"]


def test_the_manual_agent_has_no_accuracy_of_its_own_and_says_where_it_is_scored():
    entry = step(build(), "troubleshooting", "manual_agent")
    assert entry["accuracy"] is None
    assert "citation" in entry["note"] and "synthesizer" in entry["note"]


def test_the_time_of_a_troubleshooting_step_is_the_live_answers_latency():
    block = build()
    graph_agent = step(block, "troubleshooting", "graph_agent")["time"]
    assert graph_agent == {
        "p50_s": 8.5,
        "p95_s": 9.7,
        "per": "answer",
        "n": 22,
        "source": "live answers, 28 Oct 2026",
    }
    changed = results()
    changed["layer5"]["live"]["latency"]["nodes"]["verify"]["p50"] = 9.9
    assert (
        step(build(results=changed), "troubleshooting", "verify")["time"]["p50_s"]
        == 9.9
    )


def test_the_cost_of_a_troubleshooting_step_is_the_mean_of_the_timed_answers():
    block = build()
    graph_agent = step(block, "troubleshooting", "graph_agent")["cost"]
    assert graph_agent["usd"] == pytest.approx(0.004)
    assert graph_agent["per"] == "answer" and graph_agent["n"] == 4
    assert graph_agent["source"] == "timed run, 29 Oct 2026, 4 answers"
    assert step(block, "troubleshooting", "assemble")["cost"]["usd"] == 0


def test_the_whole_answer_reads_the_traps_the_live_cost_and_the_live_latency():
    answer = build()["troubleshooting"]["answer"]
    assert (answer["accuracy"]["k"], answer["accuracy"]["n"]) == (5, 5)
    assert answer["cost"]["usd"] == 0.0088 and answer["cost"]["p50_usd"] == 0.0098
    assert answer["cost"]["n"] == 25
    assert answer["time"]["p50_s"] == 13.6 and answer["time"]["p95_s"] == 15.5
    assert answer["timed_run"]["answers"] == 4
    assert answer["timed_run"]["cost_usd"] == pytest.approx(0.01003)
    assert answer["timed_run"]["p50_s"] == pytest.approx(15.5)


def test_building_the_block_changes_none_of_its_inputs():
    inputs = {"results": results(), "report": report(), "timing": timing()}
    before = copy.deepcopy(inputs)
    crews.build(
        **inputs,
        documents=DOCUMENTS,
        ledger=crews.ledger_means(ledger_rows()),
    )
    assert inputs == before


def test_the_block_is_json_and_has_no_dash_punctuation():
    text = json.dumps(build(), ensure_ascii=False)
    for character in ("\u2014", "\u2013", "\u2018", "\u2019", "\u201c", "\u201d"):
        assert character not in text


def test_a_step_without_any_timing_for_it_is_refused():
    bare = timing()
    del bare["ingestion"]["linker"]
    with pytest.raises(ValueError, match="linker"):
        build(timing=bare)


# --- writing the block ---------------------------------------------------------------


def write_inputs(tmp_path, with_ledger=True):
    from eval_stubs import write_kg

    paths = {
        "results": tmp_path / "latest.json",
        "report": tmp_path / "pipeline.json",
        "kg": write_kg(tmp_path / "kg.sqlite", [], []),
        "ledger": tmp_path / "ledger.sqlite",
        "timing": tmp_path / "timing.json",
    }
    with closing(sqlite3.connect(paths["kg"])) as conn, conn:
        for i, (path, doc_type) in enumerate(DOCUMENTS):
            conn.execute(
                "INSERT INTO documents(id, path, doc_type, sha256) VALUES (?, ?, ?, 'x')",
                (f"DOC-{i}", path, doc_type),
            )
    other = {"layer9": {"kept": True}, "cost": {"layer1": {"usd": 0}}}
    paths["results"].write_text(json.dumps({**results(), **other}))
    paths["report"].write_text(json.dumps(report()))
    paths["timing"].write_text(json.dumps(timing()))
    if with_ledger:
        with closing(sqlite3.connect(paths["ledger"])) as conn, conn:
            conn.execute(
                "CREATE TABLE calls(id INTEGER PRIMARY KEY, ts TEXT, model TEXT, purpose TEXT,"
                " input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,"
                " usd REAL, cache_hit INTEGER)"
            )
            for row in ledger_rows():
                conn.execute(
                    "INSERT INTO calls(ts, model, purpose, input_tokens, cached_input_tokens,"
                    " output_tokens, usd, cache_hit) VALUES (?, 'm', ?, 1, 0, 1, ?, ?)",
                    (row["ts"], row["purpose"], row["usd"], row["cache_hit"]),
                )
    return paths


def test_writing_the_block_keeps_every_other_section_of_the_results(tmp_path):
    paths = write_inputs(tmp_path)
    before = json.loads(paths["results"].read_text())
    block = crews.write_block(**paths)
    after = json.loads(paths["results"].read_text())
    assert after["crews"] == json.loads(json.dumps(block))
    assert {k: v for k, v in after.items() if k != "crews"} == before
    text = paths["results"].read_text()
    assert (
        text == json.dumps(after, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert crews.write_block(**paths) == block  # the same inputs, the same block


def test_the_block_keeps_the_ledger_means_for_a_machine_with_no_ledger(tmp_path):
    paths = write_inputs(tmp_path)
    block = crews.write_block(**paths)
    paths["ledger"].unlink()
    again = crews.write_block(**paths)
    assert again == block
    assert block["ledger"]["extract handover"]["n"] == 2


def test_with_no_ledger_and_no_earlier_block_the_write_stops_and_says_why(tmp_path):
    paths = write_inputs(tmp_path, with_ledger=False)
    with pytest.raises(ValueError, match="ledger"):
        crews.write_block(**paths)
    assert "crews" not in json.loads(paths["results"].read_text())


def test_with_no_timing_file_the_write_stops_and_names_the_flag(tmp_path):
    paths = write_inputs(tmp_path)
    paths["timing"].unlink()
    with pytest.raises(ValueError, match="--time-crews"):
        crews.write_block(**paths)


def test_a_timed_step_with_no_live_question_is_refused():
    replayed = timing()
    replayed["ingestion"]["resolver"]["questions"] = []
    with pytest.raises(ValueError, match="live JEV question"):
        build(timing=replayed)


def test_the_block_keeps_the_ledger_means_of_the_purposes_it_reads_and_no_others():
    rows = [
        *ledger_rows(),
        {
            "purpose": "eval judge",
            "usd": 0.1,
            "cache_hit": 0,
            "ts": "2026-10-28T08:00:00+00:00",
        },
    ]
    kept = build(ledger=crews.ledger_means(rows))["ledger"]
    assert "eval judge" not in kept
    assert set(kept) == set(crews.PURPOSES)
