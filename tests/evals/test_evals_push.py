"""Tests for pushing results to Langfuse: one dataset per golden set, one item per
question, and scores for each item. A fake client stands in, so nothing reaches the
network."""

import pytest

from app.brand import load_brand
from app.evals import push


class FakeSpan:
    def __init__(self, calls, kwargs):
        self.calls, self.kwargs = calls, kwargs

    def end(self):
        self.calls.append(("end", self.kwargs["name"]))


class FakeLangfuse:
    """Records every call it gets, in order."""

    def __init__(self):
        self.calls = []

    def create_dataset(self, **kwargs):
        self.calls.append(("create_dataset", kwargs))

    def create_dataset_item(self, **kwargs):
        self.calls.append(("create_dataset_item", kwargs))

    def create_trace_id(self, *, seed=None):
        self.calls.append(("create_trace_id", seed))
        return "trace-" + seed

    def start_observation(self, **kwargs):
        self.calls.append(("start_observation", kwargs))
        return FakeSpan(self.calls, kwargs)

    def create_score(self, **kwargs):
        self.calls.append(("create_score", kwargs))

    def flush(self):
        self.calls.append(("flush", None))

    def of(self, name):
        return [payload for call, payload in self.calls if call == name]


TRIAGE = [
    {
        "id": "tri-01",
        "kind": "incident",
        "source": "incident",
        "question": "HP-01 E217",
        "labels": {
            "machine": "HP-01",
            "alarm": "E-217",
            "root_cause": "FM-SEAL-WEAR",
            "intent": "troubleshoot",
            "safety": True,
        },
    },
    {
        "id": "tri-02",
        "kind": "out_of_scope",
        "source": "gate",
        "question": "who is on C shift",
        "labels": {
            "machine": None,
            "alarm": "none",
            "root_cause": None,
            "intent": "out_of_scope",
            "safety": False,
        },
    },
]
ANSWERS = [
    {
        "id": "ans-01",
        "source": "incident",
        "trap": None,
        "question": "HP-01 E217 what now",
        "expect": {"machine": "HP-01"},
    },
]
RESULTS = {
    "layer3": {
        "items": [
            {
                "id": "tri-01",
                "question": "HP-01 E217",
                "status": "act",
                "predicted": {"machine": "HP-01"},
                "correct": {
                    "machine": True,
                    "alarm": True,
                    "root_cause": False,
                    "intent": True,
                    "safety": True,
                },
            },
            {
                "id": "tri-02",
                "question": "who is on C shift",
                "status": "out_of_scope",
                "predicted": {"machine": None},
                "correct": {
                    "machine": True,
                    "alarm": True,
                    "root_cause": None,
                    "intent": True,
                    "safety": True,
                },
            },
        ]
    },
    "layer4": {
        "items": [
            {
                "id": "ans-01",
                "question": "HP-01 E217 what now",
                "status": "act",
                "source": "incident",
                "trap": None,
                "ok": True,
                "checks": {"root_top1": True, "lockout": None, "parts": False},
                "judge": {"cause": 2, "contradiction": 1, "safety": 2},
            },
        ]
    },
}


def run_push(client=None):
    client = client or FakeLangfuse()
    summary = push.push(RESULTS, TRIAGE, ANSWERS, client, run_name="run-1")
    return client, summary


def test_there_is_one_dataset_per_golden_set_named_with_the_brand_slug():
    client, summary = run_push()
    slug = load_brand().slug
    created = client.of("create_dataset")
    assert [d["name"] for d in created] == [
        f"{slug}-golden-triage",
        f"{slug}-golden-answers",
    ]
    assert all(d.get("description") for d in created)
    assert summary["datasets"] == 2


def test_each_question_is_an_item_with_its_labels_as_the_expected_output():
    client, _ = run_push()
    items = client.of("create_dataset_item")
    assert len(items) == 3
    first = items[0]
    assert first["dataset_name"].endswith("golden-triage")
    assert first["id"].endswith(":tri-01") and first["input"] == {
        "question": "HP-01 E217"
    }
    assert first["expected_output"]["machine"] == "HP-01"
    # the item is linked to the trace that carries its scores
    assert first["source_trace_id"].startswith("trace-")


def test_an_item_id_is_stable_so_a_second_push_updates_instead_of_duplicating():
    first, _ = run_push()
    second, _ = run_push()
    assert [i["id"] for i in first.of("create_dataset_item")] == [
        i["id"] for i in second.of("create_dataset_item")
    ]
    assert first.of("create_trace_id") == second.of("create_trace_id")


def test_each_check_is_a_boolean_score_on_the_trace_of_its_item_and_nulls_are_skipped():
    client, summary = run_push()
    scores = client.of("create_score")
    triage = [s for s in scores if s["trace_id"].endswith("tri-01")]
    assert {s["name"] for s in triage} == {
        "triage.machine",
        "triage.alarm",
        "triage.root_cause",
        "triage.intent",
        "triage.safety",
    }
    root = next(s for s in triage if s["name"] == "triage.root_cause")
    assert root["value"] == 0.0 and root["data_type"] == "BOOLEAN"
    skipped = [s["name"] for s in scores if s["trace_id"].endswith("tri-02")]
    assert "triage.root_cause" not in skipped
    answers = {s["name"]: s for s in scores if s["trace_id"].endswith("ans-01")}
    assert "answer.lockout" not in answers
    assert answers["answer.parts"]["value"] == 0.0
    assert answers["answer.ok"]["value"] == 1.0
    assert summary["scores"] == len(scores)


def test_a_score_has_an_id_fixed_by_its_run_item_and_name_so_a_repeat_updates_it():
    first, _ = run_push()
    second, _ = run_push()
    ids = [s["score_id"] for s in first.of("create_score")]
    assert ids == [s["score_id"] for s in second.of("create_score")]
    assert len(set(ids)) == len(ids), "two scores share an id"
    # another run name is another set of scores
    other = FakeLangfuse()
    push.push(RESULTS, TRIAGE, ANSWERS, other, run_name="run-2")
    assert not set(ids) & {s["score_id"] for s in other.of("create_score")}


def test_the_judge_rubric_is_a_numeric_score():
    client, _ = run_push()
    rubric = {
        s["name"]: s
        for s in client.of("create_score")
        if s["name"].startswith("judge.")
    }
    assert (
        rubric["judge.cause"]["value"] == 2.0
        and rubric["judge.cause"]["data_type"] == "NUMERIC"
    )
    assert rubric["judge.contradiction"]["value"] == 1.0


def test_each_item_gets_a_trace_with_its_question_and_the_outcome():
    client, _ = run_push()
    spans = client.of("start_observation")
    assert spans[0]["input"] == "HP-01 E217" and spans[0]["trace_context"][
        "trace_id"
    ].startswith("trace-")
    assert spans[0]["output"]["status"] == "act"
    assert ("end", spans[0]["name"]) in client.calls


def test_the_client_is_flushed_last():
    client, _ = run_push()
    assert client.calls[-1] == ("flush", None)


def test_results_without_the_layers_cannot_be_pushed():
    with pytest.raises(ValueError, match="layer3"):
        push.push(
            {"layer4": RESULTS["layer4"]}, TRIAGE, ANSWERS, FakeLangfuse(), run_name="r"
        )
