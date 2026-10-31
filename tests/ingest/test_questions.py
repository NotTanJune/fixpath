"""Tests for the questions put to the decider: how each one is built and when it is
asked."""

import json
import math

import pytest

from app.decide import Answer, Choice, Noul
from app.ingest import questions


def noul(p):
    return Answer("noul", p=p, probabilities={"yes": p, "no": 1 - p}, backend="fake")


def test_gates_are_in_one_place():
    assert questions.GATES == {
        "merge_entity": 0.7,
        "queue_entity": 0.5,
        "merge_incident": 0.9,
        "queue_incident": 0.5,
        "link": 0.75,
        "link_review": 0.4,
    }


def test_the_entity_gate_is_tuned_and_the_other_gates_are_unchanged():
    # eval layer 2: true part pairs scored 0.72 to 0.97, wrong pairs at most 0.41
    assert questions.GATES["merge_entity"] == 0.7
    assert questions.GATES["merge_incident"] == 0.9
    assert questions.GATES["link"] == 0.75
    assert questions.merge_band(0.72, "entity") == "merge"
    assert questions.merge_band(0.69, "entity") == "queue"
    assert questions.merge_band(0.72, "incident") == "queue"


def test_the_two_noul_questions_use_the_contract_wording():
    assert isinstance(questions.SAME_ENTITY, Noul)
    assert questions.SAME_ENTITY.instructions == (
        "Does the mention refer to the candidate? Spelling variants, abbreviations, "
        "shop-floor names and vendor numbers for the same item count as the same."
    )
    assert isinstance(questions.SAME_INCIDENT, Noul)
    assert questions.SAME_INCIDENT.instructions == (
        "Do these two records describe the same breakdown? Records of one breakdown "
        "come from different documents, use different words, and can differ a little "
        "in time or downtime."
    )


def test_the_ledger_purposes_are_named_once():
    assert questions.PURPOSES == {
        "Asset": "merge Asset",
        "Part": "merge Part",
        "Incident": "merge Incident",
        "root_cause": "link root cause",
    }


@pytest.mark.parametrize(
    ("p", "band"),
    [
        (1.0, "merge"),
        (0.95, "merge"),
        (0.9, "merge"),
        (0.5, "queue"),
        (0.49, "none"),
        (0.3, "none"),
        (0.0, "none"),
    ],
)
@pytest.mark.parametrize("kind", ["entity", "incident"])
def test_merge_band(p, band, kind):
    assert questions.merge_band(p, kind) == band


@pytest.mark.parametrize(("kind", "band"), [("entity", "merge"), ("incident", "queue")])
def test_merge_band_at_the_entity_gate(kind, band):
    assert questions.merge_band(0.7, kind) == band


def test_merge_band_reads_the_gates_of_its_kind(monkeypatch):
    monkeypatch.setitem(questions.GATES, "merge_entity", 0.7)
    monkeypatch.setitem(questions.GATES, "queue_incident", 0.6)
    assert questions.merge_band(0.8, "entity") == "merge"
    assert questions.merge_band(0.8, "incident") == "queue"
    assert questions.merge_band(0.55, "entity") == "queue"
    assert questions.merge_band(0.55, "incident") == "none"


@pytest.mark.parametrize(
    ("p", "band"),
    [
        (0.99, "link"),
        (0.75, "link"),
        (0.74, "review"),
        (0.4, "review"),
        (0.39, "none"),
        (0.0, "none"),
    ],
)
def test_link_band(p, band):
    assert questions.link_band(p) == band


def test_probability_of_a_noul_is_p():
    assert questions.probability(noul(0.8)) == 0.8


def test_probability_of_a_choice_is_that_of_the_chosen_option():
    answer = Answer("choice", choice="a", probabilities={"a": 0.7, "b": 0.3})
    assert questions.probability(answer) == 0.7


@pytest.mark.parametrize(
    "answer",
    [
        Answer("refusal"),
        Answer("noul"),
        Answer("noul", p=math.nan),
        Answer("noul", p=1.5),
        Answer("choice", choice="a", probabilities={"b": 1.0}),
        Answer("choice", choice=None, probabilities={"b": 1.0}),
        Answer("score", score=1.0),
    ],
)
def test_probability_is_none_when_there_is_no_decision(answer):
    assert questions.probability(answer) is None


def test_entity_state_holds_the_mention_and_the_candidate_with_two_quotes():
    state = questions.entity_state(
        type="Part",
        text="HLP46",
        quote="HLP46 x2",
        doc_path="maintenance/log.csv",
        candidate_id="OIL-HLP46-20L",
        label="Hydraulic oil",
        description="Hydraulic oil HLP 46",
        aliases=["hlp"],
        quotes=["one", "two", "three"],
    )
    assert state == {
        "mention": {
            "type": "Part",
            "text": "HLP46",
            "quote": "HLP46 x2",
            "doc_path": "maintenance/log.csv",
        },
        "candidate": {
            "id": "OIL-HLP46-20L",
            "label": "Hydraulic oil",
            "description": "Hydraulic oil HLP 46",
            "aliases": ["hlp"],
            "evidence": ["one", "two"],
        },
    }
    json.dumps(state)


def test_incident_state_holds_both_summaries():
    a = questions.incident_summary(
        source_type="log", date="2026-05-01", machine="HP-01"
    )
    b = questions.incident_summary(source_type="handover", date="2026-05-01")
    assert set(a) == {
        "source_type",
        "date",
        "shift",
        "machine",
        "alarm",
        "symptom",
        "action",
        "parts",
        "technician",
        "downtime_min",
        "quote",
    }
    assert a["machine"] == "HP-01"
    assert a["parts"] == []
    assert questions.incident_state(a, b) == {"record_a": a, "record_b": b}
    json.dumps(questions.incident_state(a, b))


def test_root_cause_question_adds_the_unknown_option():
    question = questions.root_cause_question({"FM-A": "A", "FM-B": "B"})
    assert isinstance(question, Choice)
    assert list(question.options) == ["FM-A", "FM-B", questions.UNKNOWN]


def test_root_cause_state_holds_the_incident_and_its_alarms():
    incident = questions.incident_summary(source_type="x", date="2026-05-01")
    alarms = [{"code": "E-217", "text": "Main cylinder pressure loss", "causes": []}]
    assert questions.root_cause_state(incident, alarms) == {
        "incident": incident,
        "alarms": alarms,
    }


# --- ask: the decision policy ----------------------------------------------------------


class FakeDecider:
    name = "fake"
    model = "fake"

    def __init__(self, result):
        self.result = result
        self.seen = []

    def ask(self, state, questions, purpose):
        self.seen.append((state, questions, purpose))
        if isinstance(self.result, BaseException):
            raise self.result
        return {qid: self.result for qid in questions}


def test_ask_returns_the_probability_and_the_backend():
    decider = FakeDecider(noul(0.8))
    verdict = questions.ask(
        decider, {"s": 1}, "same_entity", questions.SAME_ENTITY, "merge Part"
    )
    assert (verdict.p, verdict.backend, verdict.reason) == (0.8, "fake", None)
    assert decider.seen == [
        ({"s": 1}, {"same_entity": questions.SAME_ENTITY}, "merge Part")
    ]


def test_ask_turns_a_refusal_into_no_decision():
    verdict = questions.ask(
        FakeDecider(Answer("refusal", backend="fake")),
        {},
        "q",
        questions.SAME_ENTITY,
        "x",
    )
    assert (verdict.p, verdict.reason) == (None, "no decision")


def test_ask_turns_a_decision_error_into_a_reason_and_goes_on():
    from app.decide import DecisionError

    verdict = questions.ask(
        FakeDecider(DecisionError("boom")), {}, "q", questions.SAME_ENTITY, "x"
    )
    assert (verdict.p, verdict.backend, verdict.reason) == (
        None,
        None,
        "decider error: DecisionError",
    )


def test_ask_lets_a_spent_budget_stop_the_run():
    from app.decide import BudgetExceeded, NoDecisionBackend
    from app.llm import MissingApiKey

    for error in (
        BudgetExceeded("spent"),
        NoDecisionBackend("no key"),
        MissingApiKey("no key"),
    ):
        with pytest.raises(type(error)):
            questions.ask(FakeDecider(error), {}, "q", questions.SAME_ENTITY, "x")
