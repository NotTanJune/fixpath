"""Tests for the golden question sets. They are built from the hidden truth with fixed
templates and a fixed seed, so they cost nothing and come out the same on every run."""

import json
import re
from collections import Counter

import pytest

from app.evals import golden
from app.evals.truth import load_truth

TRUTH_ONLY_ID = re.compile(r"\b(INC|FM|PROC)-", re.I)
GATE_KINDS = {
    "alias_only",
    "alarm_only",
    "no_alarm",
    "ambiguous_model",
    "part_question",
    "out_of_scope",
}
TRAPS = {
    "unknown_machine",
    "vague_symptom",
    "out_of_scope",
    "conflicting_notes",
    "zero_stock",
}


@pytest.fixture(scope="module")
def truth():
    return load_truth()


@pytest.fixture(scope="module")
def triage_items(truth):
    return golden.build_triage(truth)


@pytest.fixture(scope="module")
def answer_items(truth):
    return golden.build_answers(truth)


def incident_types(truth):
    """The distinct machine, alarm and failure mode combinations in the truth incidents."""
    found = set()
    for incident_id in truth.of_type("Incident"):
        triggered = truth.targets(incident_id, "TRIGGERED_BY")
        found.add(
            (
                truth.machine_of(incident_id),
                triggered[0] if triggered else None,
                truth.root_cause_of(incident_id),
            )
        )
    return found


def test_the_sets_are_identical_on_every_build(truth):
    assert golden.build_triage(truth) == golden.build_triage(truth)
    assert golden.build_answers(truth) == golden.build_answers(truth)


def test_the_files_are_written_byte_for_byte_the_same_each_time(truth, tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    golden.write(first, truth)
    golden.write(second, truth)
    for name in ("triage.jsonl", "answers.jsonl"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
        assert (first / name).read_bytes().endswith(b"\n")


def test_the_committed_files_are_what_the_generator_writes(truth, tmp_path):
    golden.write(tmp_path, truth)
    for name in ("triage.jsonl", "answers.jsonl"):
        committed = golden.GOLDEN_DIR / name
        assert committed.is_file(), "run `uv run python -m app.evals.golden` and commit"
        assert committed.read_bytes() == (tmp_path / name).read_bytes()


def test_triage_has_one_item_per_incident_type_and_the_six_gate_items(
    truth, triage_items
):
    by_source = Counter(item["source"] for item in triage_items)
    assert by_source["incident"] == len(incident_types(truth))
    assert by_source["gate"] == 6
    assert {i["kind"] for i in triage_items if i["source"] == "gate"} == GATE_KINDS
    assert 28 <= len(triage_items) <= 36
    assert len({i["id"] for i in triage_items}) == len(triage_items)


def test_triage_covers_every_machine_and_every_failure_mode(truth, triage_items):
    machines = {
        i["labels"]["machine"] for i in triage_items if i["source"] == "incident"
    }
    modes = {
        i["labels"]["root_cause"] for i in triage_items if i["source"] == "incident"
    }
    assert machines == set(truth.of_type("Asset"))
    assert modes == set(truth.of_type("FailureMode"))


def test_a_question_never_holds_a_truth_only_id(triage_items, answer_items):
    for item in [*triage_items, *answer_items]:
        assert not TRUTH_ONLY_ID.search(item["question"]), item["question"]


def test_a_question_reads_like_a_technician_wrote_it(triage_items, answer_items):
    text = " ".join(item["question"] for item in [*triage_items, *answer_items])
    assert "w/" in text and "pls" in text
    spellings = set(re.findall(r"\b(?:ERR 217|Err:217|E217|E-217)\b", text))
    assert len(spellings) >= 2


def test_triage_labels_come_from_the_truth(truth, triage_items):
    incident = next(i for i in triage_items if i["source"] == "incident")
    source = truth.nodes[incident["truth_incident"]]
    assert incident["labels"]["machine"] == truth.machine_of(source.id)
    assert incident["labels"]["root_cause"] == truth.root_cause_of(source.id)
    procedure = truth.nodes[truth.incident_details[source.id].procedure_id]
    hazard = incident["hazard"]
    assert incident["labels"]["safety"] == (procedure.props["requires_loto"] or hazard)
    assert incident["labels"]["intent"] == "troubleshoot"


def test_a_gate_item_has_no_truth_incident_so_its_safety_is_unscored(triage_items):
    gates = [i for i in triage_items if i["source"] == "gate"]
    assert gates
    for item in gates:
        expected = True if item["hazard"] else None
        assert item["labels"]["safety"] is expected, item["kind"]
    # an item made from an incident keeps a real label
    incidents = [i for i in triage_items if i["source"] == "incident"]
    assert all(isinstance(i["labels"]["safety"], bool) for i in incidents)


def test_an_incident_with_no_alarm_is_labeled_none(truth, triage_items):
    no_alarm = [
        i
        for i in triage_items
        if i["source"] == "incident"
        and not truth.targets(i["truth_incident"], "TRIGGERED_BY")
    ]
    assert no_alarm
    assert all(i["labels"]["alarm"] == "none" for i in no_alarm)


def test_the_gate_items_test_what_they_say(truth, triage_items):
    gates = {i["kind"]: i for i in triage_items if i["source"] == "gate"}
    assert gates["alias_only"]["labels"]["machine"] in truth.of_type("Asset")
    assert not re.search(r"\b[A-Z]{1,3}-\d{2}\b", gates["alias_only"]["question"])
    assert gates["alarm_only"]["labels"]["alarm"] != "none"
    assert gates["alarm_only"]["labels"]["machine"] in truth.of_type("Asset")
    assert gates["no_alarm"]["labels"]["alarm"] == "none"
    assert gates["ambiguous_model"]["labels"]["machine"] is None
    assert gates["ambiguous_model"]["expect_status"] == ["clarify"]
    assert gates["part_question"]["labels"]["intent"] == "find_part"
    assert gates["out_of_scope"]["labels"]["intent"] == "out_of_scope"
    assert gates["out_of_scope"]["expect_status"] == ["out_of_scope"]


def test_answers_are_twenty_truth_incidents_and_the_five_traps(answer_items):
    assert len(answer_items) == 25
    assert Counter(i["source"] for i in answer_items) == {"incident": 20, "trap": 5}
    assert {i["trap"] for i in answer_items if i["source"] == "trap"} == TRAPS
    assert len({i["id"] for i in answer_items}) == 25
    ids = [i["truth_incident"] for i in answer_items if i["source"] == "incident"]
    assert len(set(ids)) == 20


def test_answers_cover_each_machine_three_times_and_each_failure_mode(
    truth, answer_items
):
    found = [i for i in answer_items if i["source"] == "incident"]
    machines = Counter(i["expect"]["machine"] for i in found)
    assert set(machines) == set(truth.of_type("Asset"))
    assert min(machines.values()) >= 3
    assert {i["expect"]["root_cause"] for i in found} == set(
        truth.of_type("FailureMode")
    )


def test_answer_expectations_come_from_the_truth(truth, answer_items):
    for item in (i for i in answer_items if i["source"] == "incident"):
        incident_id = item["truth_incident"]
        detail = truth.incident_details[incident_id]
        expect = item["expect"]
        assert expect["root_cause"] == truth.root_cause_of(incident_id)
        assert expect["fix"] == detail.procedure_id
        assert "fixes" not in expect, "no field that nothing reads"
        assert item["hazard"] == golden._hazard(item["question"])
        required = {
            e.dst: e.props["qty"]
            for e in truth.edges
            if e.src == expect["fix"] and e.rel == "REQUIRES"
        }
        assert {p["part_no"]: p["qty"] for p in expect["parts"]} == required
        for part in expect["parts"]:
            assert part["bin"] == truth.nodes[part["part_no"]].props["bin"]
        assert expect["lockout"] == truth.nodes[expect["fix"]].props["requires_loto"]


def test_the_traps_carry_their_expectations(answer_items):
    traps = {i["trap"]: i for i in answer_items if i["source"] == "trap"}
    assert traps["unknown_machine"]["question"] == "Line 4 robot is down"
    assert traps["unknown_machine"]["expect"]["status"] == ["clarify"]
    assert traps["unknown_machine"]["expect"]["reason_contains"] == ["RW-01", "Line 3"]
    assert traps["vague_symptom"]["question"] == "the CNC is making a weird noise"
    assert traps["vague_symptom"]["expect"]["status"] == ["confirm", "clarify"]
    assert traps["out_of_scope"]["question"] == "can you approve my leave for Friday"
    assert traps["out_of_scope"]["expect"]["status"] == ["out_of_scope"]
    assert "VT-018" in traps["conflicting_notes"]["question"].replace("VT018", "VT-018")
    assert traps["conflicting_notes"]["expect"]["note_contains"] == ["INC-2026-010"]
    assert traps["zero_stock"]["expect"]["part"] == {
        "part_no": "GRP-FNG-12",
        "enough": False,
    }


def test_load_reads_a_file_back_as_dicts(truth, tmp_path):
    golden.write(tmp_path, truth)
    assert golden.load("triage", tmp_path) == golden.build_triage(truth)
    assert json.loads((tmp_path / "answers.jsonl").read_text().splitlines()[0])["id"]
