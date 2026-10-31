"""Tests for layer 3, which scores triage on the golden set field by field. It also
reports how often triage acted, confirmed or asked, and how reliable its confidence is."""

import pytest
from crew_stubs import FakeDecider, choice, noul, open_crew_kg, triage_answers

from app.ask.types import Triage
from app.evals import layer3
from app.evals.match import Matching

MATCHING = Matching(
    ids={"FM-SEAL-WEAR": "FM-SEAL-WEAR", "FM-GRAPH-OIL": "FM-OIL-CONTAM"}
)


def item(id, machine, alarm, root_cause, intent="troubleshoot", safety=True, **more):
    return {
        "id": id,
        "kind": more.pop("kind", "incident"),
        "source": more.pop("source", "incident"),
        "question": f"question {id}",
        "labels": {
            "machine": machine,
            "alarm": alarm,
            "root_cause": root_cause,
            "intent": intent,
            "safety": safety,
        },
        **more,
    }


def triage_of(
    status="act",
    machine=None,
    alarm=None,
    mode=None,
    intent="troubleshoot",
    flag=0.9,
    p_machine=0.95,
    p_root_cause=0.8,
):
    confidence = {
        "machine": p_machine,
        "alarm": 0.9,
        "root_cause": p_root_cause,
        "intent": 0.95,
    }
    return Triage(
        asset_id=machine,
        alarm_code=alarm,
        failure_mode_id=mode,
        intent=intent,
        safety_critical=flag,
        confidence=confidence,
        status=status,
    )


def test_a_correct_triage_scores_every_field_right():
    row = layer3.score_item(
        item("t1", "HP-01", "E-217", "FM-SEAL-WEAR"),
        triage_of(machine="HP-01", alarm="E-217", mode="FM-SEAL-WEAR"),
        MATCHING,
    )
    assert row["correct"] == {
        "machine": True,
        "alarm": True,
        "root_cause": True,
        "intent": True,
        "safety": True,
    }
    assert row["predicted"]["root_cause"] == "FM-SEAL-WEAR"


def test_a_graph_failure_mode_id_is_mapped_to_the_truth_id_before_it_is_compared():
    row = layer3.score_item(
        item("t1", "HP-01", "E-221", "FM-OIL-CONTAM"),
        triage_of(machine="HP-01", alarm="E-221", mode="FM-GRAPH-OIL"),
        MATCHING,
    )
    assert row["correct"]["root_cause"] is True
    assert row["predicted"]["root_cause"] == "FM-OIL-CONTAM"


def test_no_alarm_is_the_word_none_and_no_machine_is_a_null_label():
    row = layer3.score_item(
        item("t2", None, "none", None, safety=False),
        triage_of(status="clarify", flag=0.2),
        MATCHING,
    )
    assert row["predicted"]["machine"] is None and row["predicted"]["alarm"] == "none"
    assert (
        row["correct"]["machine"]
        and row["correct"]["alarm"]
        and row["correct"]["safety"]
    )
    # an item with no truth root cause does not score that field
    assert row["correct"]["root_cause"] is None


def test_wrong_picks_score_false_and_the_safety_flag_is_read_at_one_half():
    row = layer3.score_item(
        item("t3", "HP-01", "E-217", "FM-SEAL-WEAR", safety=True),
        triage_of(
            machine="CNC-01",
            alarm="E-221",
            mode="FM-GRAPH-OIL",
            intent="history",
            flag=0.49,
        ),
        MATCHING,
    )
    assert row["correct"] == {
        "machine": False,
        "alarm": False,
        "root_cause": False,
        "intent": False,
        "safety": False,
    }
    assert layer3.score_item(
        item("t3", "HP-01", "E-217", None, safety=True),
        triage_of(machine="HP-01", alarm="E-217", flag=0.5),
        MATCHING,
    )["correct"]["safety"]


def test_a_triage_with_no_safety_answer_counts_as_no_flag():
    row = layer3.score_item(
        item("t4", "HP-01", "none", None, safety=False),
        triage_of(machine="HP-01", flag=None),
        MATCHING,
    )
    assert row["predicted"]["safety"] is False and row["correct"]["safety"]


def test_an_item_with_no_safety_label_is_not_scored_for_safety():
    row = layer3.score_item(
        item("g2", "HP-01", "none", None, source="gate", kind="no_alarm", safety=None),
        triage_of(machine="HP-01", flag=0.9),
        MATCHING,
    )
    assert row["correct"]["safety"] is None
    assert row["predicted"]["safety"] is True
    # the other fields of the item are still scored
    assert row["correct"]["machine"] is True


def test_a_gate_item_is_ok_when_the_status_is_one_of_those_expected():
    gate = item(
        "g1",
        None,
        "none",
        None,
        source="gate",
        kind="ambiguous_model",
        expect_status=["clarify"],
    )
    assert (
        layer3.score_item(gate, triage_of(status="clarify"), MATCHING)["status_ok"]
        is True
    )
    assert (
        layer3.score_item(gate, triage_of(status="act", machine="CNC-01"), MATCHING)[
            "status_ok"
        ]
        is False
    )
    assert (
        layer3.score_item(item("t1", None, "none", None), triage_of(), MATCHING)[
            "status_ok"
        ]
        is None
    )


def rows():
    items = [
        item("a", "HP-01", "E-217", "FM-SEAL-WEAR"),
        item("b", "HP-01", "E-221", "FM-OIL-CONTAM"),
        item("c", "CNC-01", "none", "FM-TOOL-CLAMP"),
        item(
            "d",
            None,
            "none",
            None,
            source="gate",
            kind="out_of_scope",
            intent="out_of_scope",
            safety=False,
            expect_status=["out_of_scope"],
        ),
    ]
    results = [
        triage_of("act", "HP-01", "E-217", "FM-SEAL-WEAR"),
        triage_of("act", "HP-01", "E-230", "FM-GRAPH-OIL", p_machine=0.9),
        triage_of("confirm", "CNC-02", None, None, p_machine=0.55, p_root_cause=0.3),
        triage_of("out_of_scope", None, None, None, intent="out_of_scope", flag=0.1),
    ]
    return items, results


def test_the_section_reports_accuracy_per_field():
    items, results = rows()
    section = layer3.evaluate(items, results, MATCHING)
    accuracy = section["accuracy"]
    assert section["n"] == 4
    assert accuracy["machine"] == {"correct": 3, "n": 4, "accuracy": 0.75}
    assert accuracy["alarm"] == {"correct": 3, "n": 4, "accuracy": 0.75}
    # the root cause is scored on the three items that have one
    assert accuracy["root_cause"] == {
        "correct": 2,
        "n": 3,
        "accuracy": pytest.approx(2 / 3),
    }
    assert accuracy["intent"]["accuracy"] == 1.0


def test_the_section_reports_accuracy_on_the_items_where_the_gate_said_act():
    items, results = rows()
    on_act = layer3.evaluate(items, results, MATCHING)["on_act"]
    assert on_act["n"] == 2
    assert on_act["accuracy"]["machine"] == {"correct": 2, "n": 2, "accuracy": 1.0}
    assert on_act["accuracy"]["alarm"] == {"correct": 1, "n": 2, "accuracy": 0.5}


def test_safety_recall_and_the_false_positive_rate_are_reported_apart_from_accuracy():
    items = [
        item("a", "HP-01", "E-217", None, safety=True),
        item("b", "HP-01", "E-217", None, safety=True),
        item("c", "HP-01", "E-217", None, safety=False),
        item("d", "HP-01", "E-217", None, safety=False),
        item("e", "HP-01", "E-217", None, safety=False),
        item("g", None, "none", None, source="gate", kind="no_alarm", safety=None),
    ]
    flags = [0.9, 0.1, 0.9, 0.2, 0.3, 0.9]
    results = [triage_of("act", "HP-01", "E-217", flag=f) for f in flags]
    section = layer3.evaluate(items, results, MATCHING)
    assert section["safety"] == {
        "scored": 5,
        "unscored": 1,
        "positives": 2,
        "negatives": 3,
        "tp": 1,
        "fn": 1,
        "fp": 1,
        "tn": 2,
        "recall": 0.5,
        "false_positive_rate": pytest.approx(1 / 3),
    }
    # the unscored gate item is not in the safety accuracy either
    assert section["accuracy"]["safety"]["n"] == 5


def test_safety_has_no_rates_when_nothing_is_scored():
    section = layer3.evaluate([], [], MATCHING)
    assert section["safety"]["recall"] is None
    assert section["safety"]["false_positive_rate"] is None


def test_the_section_reports_the_share_of_each_status():
    items, results = rows()
    share = layer3.evaluate(items, results, MATCHING)["status"]
    assert share["count"] == {"act": 2, "confirm": 1, "clarify": 0, "out_of_scope": 1}
    assert share["share"]["act"] == 0.5 and share["share"]["confirm"] == 0.25


def test_the_section_holds_a_reliability_table_and_ece_for_machine_and_root_cause():
    items, results = rows()
    reliability = layer3.evaluate(items, results, MATCHING)["reliability"]
    machine = reliability["machine"]
    assert sum(b["count"] for b in machine["bins"]) == 4
    # three right picks at 0.95, 0.9 and 0.95 and one wrong pick at 0.55
    assert machine["ece"] == pytest.approx((3 * (1 - 2.8 / 3) + 0.55) / 4)
    assert sum(b["count"] for b in reliability["root_cause"]["bins"]) == 3


def test_the_gate_items_are_listed_with_their_verdict():
    items, results = rows()
    gates = layer3.evaluate(items, results, MATCHING)["gates"]
    assert gates == [
        {
            "id": "d",
            "kind": "out_of_scope",
            "expect": ["out_of_scope"],
            "status": "out_of_scope",
            "ok": True,
        }
    ]


def test_the_section_keeps_one_row_per_item_for_the_langfuse_push():
    items, results = rows()
    section = layer3.evaluate(items, results, MATCHING)
    assert [r["id"] for r in section["items"]] == ["a", "b", "c", "d"]
    assert set(section["items"][0]) >= {
        "id",
        "question",
        "status",
        "predicted",
        "correct",
        "confidence",
    }


def test_the_section_has_no_timing_or_cost_so_a_replay_matches_a_live_run():
    items, results = rows()
    text = str(layer3.evaluate(items, results, MATCHING))
    assert "seconds" not in text and "usd" not in text


def test_an_empty_set_has_no_ratios_instead_of_a_made_up_one():
    section = layer3.evaluate([], [], MATCHING)
    assert section["n"] == 0
    assert section["accuracy"]["machine"]["accuracy"] is None
    assert section["status"]["share"]["act"] is None


def test_run_asks_the_decider_once_per_item_with_the_purpose_triage(tmp_path):
    fake = FakeDecider(
        lambda purpose, state, questions: triage_answers(
            machine=choice("HP-01", 0.97), alarm=choice("E-217", 0.95), safety=noul(0.1)
        )
    )
    items = [
        item("a", "HP-01", "E-217", "FM-SEAL-WEAR"),
        item("b", "HP-01", "E-217", None),
    ]
    kg = tmp_path / "kg.sqlite"
    open_crew_kg(tmp_path).close()
    results = layer3.run_triage(items, kg, fake)
    assert fake.purposes() == ["triage", "triage"]
    assert [r.asset_id for r in results] == ["HP-01", "HP-01"]
    assert fake.calls[0][1]["question"] == "question a"
