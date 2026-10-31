"""Tests of the suggested questions, and of the check that finds which of them the caches
can answer.
"""

import sqlite3
from contextlib import closing

import pytest
from api_stubs import make_app
from fastapi.testclient import TestClient
from llm_stubs import SETTINGS, isolated  # noqa: F401  (an autouse fixture)

from app import config, ledger, llm, llm_cache
from app.api import suggestions
from app.ask.types import AnswerCard, Triage
from app.decide import CacheOnlyDecider, NoDecisionBackend
from app.decide.base import DecisionCache
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey, NoCacheInstalled

CARD = AnswerCard(question="q", triage=Triage(status="clarify"))


def test_the_suggested_questions_are_the_six_of_the_brief_in_order():
    assert suggestions.QUESTIONS == [
        "HP-01 stopped with E-217, oil under the press",
        "CNC-02 spindle alarm SP-104, grinding noise",
        "RW-01 gripper keeps dropping parts, RB-551",
        "Leak tester failing the master part, VT-018",
        "Line 4 robot is down",
        "Is SEAL-KIT-50 in stock?",
    ]


# --- the endpoint ---


def test_the_endpoint_lists_each_question_with_what_the_check_said(tmp_path):
    asked = []

    def check(question):
        asked.append(question)
        return question.startswith("HP-01")

    client = TestClient(make_app(tmp_path, replay_check=check))
    found = client.get("/api/suggestions").json()
    assert [s["question"] for s in found] == suggestions.QUESTIONS
    assert [s["replayable"] for s in found] == [True, False, False, False, False, False]


def test_the_check_runs_once_and_the_result_is_kept(tmp_path):
    asked = []
    client = TestClient(
        make_app(tmp_path, replay_check=lambda q: asked.append(q) or True)
    )
    client.get("/api/suggestions")
    client.get("/api/suggestions")
    assert asked == suggestions.QUESTIONS


def test_a_check_that_fails_marks_its_question_not_replayable_and_the_server_starts(
    tmp_path, caplog
):
    def check(question):
        if question == suggestions.QUESTIONS[1]:
            raise RuntimeError("the embedding model is missing")
        return True

    app = make_app(tmp_path, replay_check=check)
    with TestClient(app) as client:  # the check runs at startup
        assert client.get("/api/health").status_code == 200
        found = client.get("/api/suggestions").json()
    assert [s["replayable"] for s in found] == [True, False, True, True, True, True]
    assert "the replay check failed" in caplog.text


def test_the_check_runs_at_startup_when_the_app_starts(tmp_path):
    asked = []
    app = make_app(tmp_path, replay_check=lambda q: asked.append(q) or True)
    with TestClient(app):
        assert asked == suggestions.QUESTIONS


# --- the check ---


def test_a_question_the_crew_answers_with_free_calls_only_is_replayable(tmp_path):
    def run(question, **kwargs):
        ledger.record(config.JEV_MODEL, "triage", 10, 0, 0, True)
        return CARD

    assert suggestions.check_replayable("q", tmp_path / "kg.sqlite", run=run) is True


@pytest.mark.parametrize(
    "error",
    [
        MissingApiKey("no key"),
        NoDecisionBackend("no answer"),
        BudgetExceeded("spent"),
        NoCacheInstalled("none"),
    ],
)
def test_a_cache_miss_or_a_stop_means_not_replayable(tmp_path, error):
    def run(question, **kwargs):
        raise error

    assert suggestions.check_replayable("q", tmp_path / "kg.sqlite", run=run) is False


def test_a_run_that_booked_a_paid_call_is_not_replayable(tmp_path):
    def run(question, **kwargs):
        ledger.record(config.SOL_MODEL, "synthesize", 10, 0, 10, False)
        return CARD

    assert suggestions.check_replayable("q", tmp_path / "kg.sqlite", run=run) is False


def test_other_failures_are_not_swallowed(tmp_path):
    def run(question, **kwargs):
        raise RuntimeError("a bug")

    with pytest.raises(RuntimeError, match="a bug"):
        suggestions.check_replayable("q", tmp_path / "kg.sqlite", run=run)


def seed(path, table, row):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(table)
        conn.execute(row)


def test_the_run_gets_keyless_settings_cache_copies_and_a_scratch_ledger(tmp_path):
    committed_cache = llm_cache.CACHE_PATH
    seed(
        committed_cache,
        "CREATE TABLE marker(x TEXT)",
        "INSERT INTO marker VALUES ('committed')",
    )
    before = committed_cache.read_bytes()
    real_ledger = ledger.LEDGER_PATH
    seen = {}

    def run(question, kg_path, settings, decider):
        seen["settings"] = settings
        seen["decider"] = decider
        seen["load_settings"] = llm.load_settings  # the crew passes its own
        seen["cache_path"] = llm_cache.CACHE_PATH
        seen["ledger_path"] = ledger.LEDGER_PATH
        with closing(sqlite3.connect(llm_cache.CACHE_PATH)) as conn:
            seen["marker"] = conn.execute("SELECT x FROM marker").fetchall()
        # a write must not reach the original
        llm_cache.CACHE_PATH.write_bytes(b"scribble")
        ledger.record(config.JEV_MODEL, "triage", 1, 0, 0, True)
        return CARD

    assert llm.load_settings().has_openai  # the test setup has a key
    original_load = llm.load_settings
    assert suggestions.check_replayable("q", tmp_path / "kg.sqlite", run=run) is True

    assert not seen["settings"].has_openai and not seen["settings"].has_typesafe
    assert seen["load_settings"] is original_load  # the settings were not switched for the whole process
    assert isinstance(seen["decider"], CacheOnlyDecider)
    assert seen["decider"].cache.path != config.CACHE_DIR / "decisions.sqlite"
    assert seen["cache_path"] != committed_cache and seen["marker"] == [("committed",)]
    assert seen["ledger_path"] != real_ledger
    # everything is put back, the committed cache is untouched, and the real ledger
    # is not written
    assert llm_cache.CACHE_PATH == committed_cache and ledger.LEDGER_PATH == real_ledger
    assert committed_cache.read_bytes() == before
    assert not real_ledger.exists()
    assert llm.load_settings() is SETTINGS and llm.load_settings is original_load


def test_the_decider_copy_holds_the_committed_decisions(tmp_path, monkeypatch):
    from app.decide import base

    committed = tmp_path / "decisions.sqlite"
    monkeypatch.setattr(base, "CACHE_PATH", committed)
    DecisionCache(committed)._connect().close()
    seed(committed, "CREATE TABLE marker(x TEXT)", "INSERT INTO marker VALUES ('d')")
    seen = {}

    def run(question, kg_path, settings, decider):
        with closing(sqlite3.connect(decider.cache.path)) as conn:
            seen["marker"] = conn.execute("SELECT x FROM marker").fetchall()
        return CARD

    suggestions.check_replayable("q", tmp_path / "kg.sqlite", run=run)
    assert seen["marker"] == [("d",)]


def test_the_default_check_runs_the_crew_and_a_miss_on_the_fixture_is_not_replayable(
    tmp_path,
):
    from api_stubs import build_api_data

    kg = build_api_data(tmp_path)
    # the caches are empty in this setup, so the first model or decision call misses
    assert suggestions.check_replayable("Line 4 robot is down", kg) is False
    assert not ledger.LEDGER_PATH.exists() or ledger.spent_usd() == 0.0


# --- make warm ---


def test_warm_needs_an_openai_key(tmp_path):
    from app.config import Settings

    with pytest.raises(suggestions.NoKeys, match="OPENAI_API_KEY"):
        suggestions.warm(settings=Settings(), run=lambda *a, **k: CARD)


def test_warm_runs_every_question_once_with_the_spend_capped_above_the_start(tmp_path):
    ledger.record(config.SOL_MODEL, "earlier", 1_000_000, 0, 0, False)  # $2.00 already
    runs = []

    def run(question, kg_path, settings):
        runs.append((question, settings.budget_usd, llm.load_settings))
        ledger.record(config.TERRA_MODEL, "graph agent", 100_000, 0, 0, False)  # $0.10
        return CARD

    results = suggestions.warm(
        settings=SETTINGS, kg_path=tmp_path / "kg.sqlite", run=run, cap_usd=0.15
    )
    assert [q for q, _, _ in runs] == suggestions.QUESTIONS
    # the run receives the capped settings
    assert all(b == pytest.approx(2.15) for _, b, _ in runs)
    assert all(load is llm.load_settings for _, _, load in runs)  # nothing is switched
    assert llm.load_settings() is SETTINGS
    assert [r.question for r in results] == suggestions.QUESTIONS
    assert all(r.usd == pytest.approx(0.10) and r.paid_calls == 1 for r in results)


def test_warm_stops_at_the_budget_and_says_which_question_it_reached(tmp_path):
    def run(question, kg_path, settings):
        if question == suggestions.QUESTIONS[2]:
            raise BudgetExceeded("model spend has reached the budget")
        return CARD

    with pytest.raises(suggestions.WarmStopped) as stopped:
        suggestions.warm(settings=SETTINGS, kg_path=tmp_path / "kg.sqlite", run=run)
    assert stopped.value.question == suggestions.QUESTIONS[2]
    assert [r.question for r in stopped.value.done] == suggestions.QUESTIONS[:2]


def test_the_default_decision_cache_path_is_a_copy_inside_the_block_and_restored_after(
    tmp_path, monkeypatch
):
    from app.decide import base

    committed = tmp_path / "decisions.sqlite"
    monkeypatch.setattr(base, "CACHE_PATH", committed)
    seed(committed, "CREATE TABLE marker(x TEXT)", "INSERT INTO marker VALUES ('d')")
    with suggestions.scratch_state():
        assert base.CACHE_PATH != committed
        with closing(sqlite3.connect(base.CACHE_PATH)) as conn:
            assert conn.execute("SELECT x FROM marker").fetchall() == [("d",)]
        assert DecisionCache().path == base.CACHE_PATH  # the cache the app would open
    assert base.CACHE_PATH == committed
