"""Tests for layer 5, which reports the speed and cost of answers from the traces of
the layer 4 cards.

A replayed run has no real speed. The section keeps the numbers of the last live run and
marks a replay as such."""

from datetime import UTC, datetime, timedelta

import pytest

from app.ask.types import AnswerCard, NodeTrace, Triage
from app.evals import layer5
from app.evals.layer4 import Run

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def trace(name, begin, seconds, cache_hit=None, cost=0.0):
    started = START + timedelta(seconds=begin)
    return NodeTrace(
        name=name,
        started_at=started,
        ended_at=started + timedelta(seconds=seconds),
        seconds=seconds,
        backend=None if cache_hit is None else "model",
        cache_hit=cache_hit,
        cost_usd=cost,
    )


def run(*traces):
    card = AnswerCard(question="q", triage=Triage(status="act"), trace=list(traces))
    return Run({"id": "x"}, card, [])


def live_runs():
    return [
        run(
            trace("triage", 0, 1.0, False, 0.001),
            trace("synthesize", 1, 3.0, False, 0.01),
            trace("assemble", 4, 0.1),
        ),
        run(
            trace("triage", 0, 2.0, False, 0.002),
            trace("synthesize", 2, 5.0, True, 0.0),
            trace("assemble", 7, 0.1),
        ),
    ]


def test_percentiles_interpolate_between_the_ranks():
    assert layer5.percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert layer5.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95) == pytest.approx(4.8)
    assert layer5.percentile([7.0], 95) == 7.0
    assert layer5.percentile([], 50) is None


def test_latency_is_reported_per_node_and_per_answer():
    section = layer5.evaluate(live_runs(), None, today="2026-10-28")
    live = section["live"]
    nodes = live["latency"]["nodes"]
    assert nodes["triage"] == {"n": 2, "p50": 1.5, "p95": pytest.approx(1.95)}
    assert nodes["synthesize"]["p50"] == 4.0
    # an answer runs from the start of its first node to the end of its last
    answer = live["latency"]["answer"]
    assert answer["n"] == 2
    assert answer["p50"] == pytest.approx((4.1 + 7.1) / 2)


def test_cost_per_answer_is_the_sum_of_its_nodes():
    live = layer5.evaluate(live_runs(), None, today="2026-10-28")["live"]
    assert live["cost_per_answer"]["mean"] == pytest.approx((0.011 + 0.002) / 2)
    assert live["cost_per_answer"]["total"] == pytest.approx(0.013)
    assert live["cost_per_answer"]["p95"] > live["cost_per_answer"]["p50"]


def test_the_cache_hit_share_counts_the_nodes_that_called_a_model():
    section = layer5.evaluate(live_runs(), None, today="2026-10-28")
    assert section["live"]["cache_hit_share"] == 0.25
    assert section["this_run"] == {
        "answers": 2,
        "cache_hit_share": 0.25,
        "replayed": False,
    }


def test_a_live_run_stamps_the_date_and_replaces_the_stored_numbers():
    old = {"live": {"measured_at": "2026-01-01", "marker": True}}
    section = layer5.evaluate(live_runs(), old, today="2026-10-28")
    assert section["live"]["measured_at"] == "2026-10-28"
    assert "marker" not in section["live"]


def replayed_runs():
    return [
        run(
            trace("triage", 0, 0.01, True),
            trace("synthesize", 0, 0.02, True),
            trace("assemble", 0, 0.0),
        )
    ]


def test_a_replay_keeps_the_numbers_of_the_last_live_run_and_marks_itself():
    old = {"live": {"measured_at": "2026-10-20", "latency": {"answer": {"p50": 9.0}}}}
    section = layer5.evaluate(replayed_runs(), old, today="2026-10-28")
    assert section["live"] == old["live"]
    assert section["this_run"] == {
        "answers": 1,
        "cache_hit_share": 1.0,
        "replayed": True,
    }


def mostly_cached_runs(hits, total):
    """One run per model step. `hits` of them are answered from the cache, the rest live."""
    return [
        run(trace("synthesize", 0, 3.0 if n >= hits else 0.01, n < hits, 0.01))
        for n in range(total)
    ]


def test_a_run_with_one_live_node_in_a_hundred_is_a_replay_and_keeps_the_old_numbers():
    old = {"live": {"measured_at": "2026-10-20", "latency": {"answer": {"p50": 9.0}}}}
    section = layer5.evaluate(mostly_cached_runs(99, 100), old, today="2026-10-28")
    assert section["live"] == old["live"]
    assert section["this_run"]["replayed"] is True
    assert section["this_run"]["cache_hit_share"] == pytest.approx(0.99)


def test_a_run_is_live_only_when_less_than_half_of_its_model_nodes_were_cached():
    old = {"live": {"measured_at": "2026-10-20", "marker": True}}
    just_live = layer5.evaluate(mostly_cached_runs(49, 100), old, today="2026-10-28")
    assert just_live["this_run"]["replayed"] is False
    assert just_live["live"]["measured_at"] == "2026-10-28"
    half = layer5.evaluate(mostly_cached_runs(50, 100), old, today="2026-10-28")
    assert half["this_run"]["replayed"] is True and half["live"] == old["live"]


def test_the_section_says_what_the_cost_mean_holds_and_which_number_to_read():
    for runs in (live_runs(), replayed_runs()):
        note = layer5.evaluate(runs, None, today="2026-10-28")["cost_note"]
        assert "mean" in note and "p50" in note and "do not retrieve" in note


def test_a_replay_with_no_earlier_live_run_has_no_live_numbers():
    section = layer5.evaluate(replayed_runs(), None, today="2026-10-28")
    assert section["live"] is None and section["this_run"]["replayed"] is True


def test_cards_that_stop_at_triage_still_count_as_answers():
    runs = [run(trace("triage", 0, 1.0, False, 0.001), trace("assemble", 1, 0.1))]
    live = layer5.evaluate(runs, None, today="2026-10-28")["live"]
    assert live["latency"]["answer"]["n"] == 1
    assert set(live["latency"]["nodes"]) == {"triage", "assemble"}
