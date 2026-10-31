"""Tests for the parts of the timed crew run that need no model call. The timed run
itself pays for real calls and is run by hand. These tests check what keeps it safe:
the spending cap, the scratch caches and the timing of each question."""

import dataclasses
from pathlib import Path

import pytest
from langchain_core.globals import get_llm_cache, set_llm_cache

from app import ledger, llm_cache
from app.config import Settings
from app.decide import base as decide_base
from app.evals import crews_time
from app.ledger import BudgetExceeded


@pytest.fixture(autouse=True)
def scratch_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")
    yield
    set_llm_cache(None)


def live_settings(**changes):
    base = Settings(openai_api_key="k1", typesafe_api_key="k2", budget_usd=1.0)
    return dataclasses.replace(base, **changes)


def test_the_cap_is_the_ledger_total_plus_the_run_cap():
    ledger.record("gpt-6.1-sol", "x", 1_000_000, 0, 0, cache_hit=False)  # $2.00
    assert crews_time.cap_for(ledger.spent_usd()) == pytest.approx(2.10)


def test_a_budget_that_is_not_the_cap_is_refused_before_any_call():
    spent = ledger.spent_usd()
    with pytest.raises(RuntimeError, match="budget"):
        crews_time.confirm_cap(live_settings(budget_usd=spent + 5.0), spent)


def test_the_budget_check_stops_at_the_cap_and_not_before():
    ledger.record("gpt-6.1-sol", "x", 1_000_000, 0, 0, cache_hit=False)
    spent = ledger.spent_usd()
    settings = live_settings(budget_usd=crews_time.cap_for(spent))
    crews_time.confirm_cap(settings, spent)  # no error: under the cap it passes
    ledger.record("gpt-6.1-sol", "y", 50_000, 0, 0, cache_hit=False)  # +$0.10
    with pytest.raises(BudgetExceeded):
        ledger.check_budget(settings)


def test_the_decider_must_be_jev_with_both_keys():
    crews_time.require_backends(live_settings())
    for bad in (
        live_settings(typesafe_api_key=None),
        live_settings(openai_api_key=None),
        live_settings(decider="decisions"),
    ):
        with pytest.raises((RuntimeError, ValueError)):
            crews_time.require_backends(bad)


def test_the_langfuse_keys_are_dropped_so_the_timing_has_no_tracing_in_it():
    settings = live_settings(langfuse_public_key="a", langfuse_secret_key="b")
    plain = crews_time.without_tracing(settings)
    assert not plain.has_langfuse
    assert plain.openai_api_key == "k1" and plain.budget_usd == 1.0


def test_scratch_caches_are_empty_files_and_the_real_paths_come_back(tmp_path):
    real = (llm_cache.CACHE_PATH, decide_base.CACHE_PATH)
    model, decisions = tmp_path / "m.sqlite", tmp_path / "d.sqlite"
    with crews_time.scratch_caches(model, decisions):
        assert (llm_cache.CACHE_PATH, decide_base.CACHE_PATH) == (model, decisions)
        llm_cache.enable_cache()
        assert get_llm_cache().path == model
        assert decide_base.DecisionCache().path == decisions
    assert (llm_cache.CACHE_PATH, decide_base.CACHE_PATH) == real
    assert get_llm_cache() is None


class Inner:
    name = "jev"
    model = "jev-1.14.0"

    def __init__(self, hit=False):
        self.hit = hit

    def ask(self, state, questions, purpose):
        ledger.record(self.model, purpose, 1000, 0, 0, cache_hit=self.hit)
        return {"q": object()}


def test_a_timed_decider_records_the_seconds_and_cost_of_each_live_question():
    timed = crews_time.TimingDecider(Inner())
    timed.ask({}, {"q": None}, "merge Part")
    timed.ask({}, {"q": None}, "link root cause")
    first, second = timed.asks
    assert first["purpose"] == "merge Part" and first["live"] is True
    assert first["seconds"] >= 0 and first["usd"] > 0
    assert second["purpose"] == "link root cause"
    assert timed.name == "jev" and timed.model == "jev-1.14.0"


def test_a_question_answered_from_the_cache_is_marked_as_not_live():
    timed = crews_time.TimingDecider(Inner(hit=True))
    timed.ask({}, {"q": None}, "merge Part")
    (only,) = timed.asks
    assert only["live"] is False and only["usd"] == 0


def test_the_questions_of_a_step_are_split_by_their_ledger_purpose():
    timed = crews_time.TimingDecider(Inner())
    for purpose in ("merge Part", "merge Incident", "link root cause", "merge Part"):
        timed.ask({}, {"q": None}, purpose)
    resolver = timed.questions("merge")
    linker = timed.questions("link")
    assert len(resolver["questions"]) == 3 and len(linker["questions"]) == 1
    assert resolver["cached"] == 0


def test_the_text_files_timed_are_spread_over_the_handovers_and_one_note():
    handovers = [Path(f"handovers/h{i:02d}.txt") for i in range(90)]
    notes = [Path("maintenance/note_a.txt"), Path("maintenance/note_b.txt")]
    types = {"handovers": "handover", "maintenance": "maintenance_note"}
    picked = crews_time.pick_text_files(
        handovers + notes, kind=lambda p: types[p.parent.name]
    )
    assert [p.name for p in picked] == ["h00.txt", "h45.txt", "h89.txt", "note_a.txt"]


def test_a_timing_file_is_written_with_sorted_keys(tmp_path):
    out = tmp_path / "t.json"
    crews_time.write_timing({"b": 1, "a": {"d": 1, "c": 2}}, out)
    text = out.read_text(encoding="utf-8")
    assert text.index('"a"') < text.index('"b"') and text.endswith("\n")
