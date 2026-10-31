"""Tests of the manual agent, which searches the manuals. A stub model writes the search
plan, and the searches run on the small test graph with a fake embedder.
"""

import pytest
from ask_stubs import FakeEmbedder, open_kg, plan_model
from llm_stubs import TERRA, isolated, rows  # noqa: F401  (an autouse fixture)

from app import llm
from app.ask import manual_agent as agent_module
from app.ask import search
from app.ask.manual_agent import SearchPlan, manual_agent
from app.ask.types import Hints
from app.config import Settings

QUESTION = "seal leak"
HINTS = Hints(asset_id="HP-01", alarm_code="E-217", intent="troubleshoot")


@pytest.fixture
def conn(tmp_path):
    connection = open_kg(tmp_path)
    yield connection
    connection.close()


def ids(result):
    return [p.chunk_id for p in result.passages]


def test_the_plan_queries_and_the_raw_question_are_searched_and_fused(
    conn, monkeypatch
):
    monkeypatch.setattr(search, "POOL", 1)
    embedder = FakeEmbedder({"seal kit": 2, "bearing": 3, "seal leak": 2})
    model = plan_model(["seal kit", "bearing"])
    result = manual_agent(QUESTION, Hints(), conn, model=model, embedder=embedder)
    assert result.queries == ["seal kit", "bearing", "seal leak"]
    assert embedder.asked == ["seal kit", "bearing", "seal leak"]
    # HP-5 tops two lists (the plan's first query and the original question), CNC-2 tops one
    assert ids(result) == ["HP-5", "CNC-2"]
    assert result.passages[0].score == pytest.approx(2 / 61)
    assert result.passages[1].score == pytest.approx(1 / 61)
    assert result.passages[0].matched_by == ["keyword", "vector"]
    assert (
        result.passages[0].page == 5 and result.passages[0].doc_path == "manuals/hp.pdf"
    )


def test_the_top_five_are_returned(conn):
    embedder = FakeEmbedder({"seal": 2})
    result = manual_agent(
        "seal", Hints(), conn, model=plan_model(["seal"]), embedder=embedder
    )
    assert len(result.passages) == agent_module.TOP_K == 5
    assert set(ids(result)) == {"HP-1", "HP-3", "HP-5", "CNC-2", "CNC-4"}


def test_only_the_top_k_of_the_fused_list_are_kept(conn, monkeypatch):
    monkeypatch.setattr(agent_module, "TOP_K", 2)
    result = manual_agent(
        "seal",
        Hints(),
        conn,
        model=plan_model(["seal"]),
        embedder=FakeEmbedder({"seal": 2}),
    )
    assert len(result.passages) == 2 and ids(result)[0] == "HP-5"


def test_a_plan_is_cut_to_three_distinct_queries(conn):
    plan = ["seal kit", "  ", "Seal Kit", "main cylinder", "relief valve", "pump"]
    embedder = FakeEmbedder()
    result = manual_agent(
        QUESTION, Hints(), conn, model=plan_model(plan), embedder=embedder
    )
    assert result.queries == ["seal kit", "main cylinder", "relief valve", "seal leak"]


def test_the_raw_question_is_not_searched_twice(conn):
    result = manual_agent(
        QUESTION,
        Hints(),
        conn,
        model=plan_model(["seal leak"]),
        embedder=FakeEmbedder(),
    )
    assert result.queries == ["seal leak"]


def test_an_empty_plan_falls_back_to_the_raw_question(conn):
    result = manual_agent(
        QUESTION, Hints(), conn, model=plan_model([]), embedder=FakeEmbedder()
    )
    assert result.queries == ["seal leak"]


def test_the_machine_hint_keeps_the_search_to_its_manual(conn):
    embedder = FakeEmbedder({"E-217": 1})
    result = manual_agent(
        "E-217", HINTS, conn, model=plan_model(["E-217"]), embedder=embedder
    )
    assert ids(result) and all(c.startswith("HP-") for c in ids(result))


def test_each_query_fetches_ten_chunks_before_the_fusion_and_five_are_kept(
    conn, monkeypatch
):
    asked = []
    real = search.manual_search

    def spy(conn_, query, asset_id=None, k=5, embedder=None, **kwargs):
        asked.append((query, k))
        return real(conn_, query, asset_id, k, embedder, **kwargs)

    monkeypatch.setattr(search, "manual_search", spy)
    embedder = FakeEmbedder({"seal": 2})
    result = manual_agent(
        "seal", Hints(), conn, model=plan_model(["seal", "bearing"]), embedder=embedder
    )
    assert asked == [("seal", 10), ("bearing", 10)]
    assert (
        agent_module.FETCH_K == 10 and len(result.passages) == agent_module.TOP_K == 5
    )


def test_a_known_alarm_with_no_machine_searches_the_manual_that_holds_the_code(conn):
    # E-214 is in the alarm table of the lathe manual only
    hints = Hints(alarm_code="E-214", intent="troubleshoot")
    result = manual_agent(
        "alarm",
        hints,
        conn,
        model=plan_model(["alarm"]),
        embedder=FakeEmbedder({"alarm": 2}),
    )
    assert ids(result) and all(c.startswith("CNC-") for c in ids(result))


def test_an_alarm_in_no_manual_table_searches_every_manual(conn):
    hints = Hints(alarm_code="E-999", intent="troubleshoot")
    result = manual_agent(
        "alarm",
        hints,
        conn,
        model=plan_model(["alarm"]),
        embedder=FakeEmbedder({"alarm": 2}),
    )
    assert {c.split("-")[0] for c in ids(result)} == {"HP", "CNC"}


def test_the_machine_beats_the_alarm_when_both_are_known(conn):
    hints = Hints(asset_id="HP-01", alarm_code="E-214", intent="troubleshoot")
    result = manual_agent(
        "alarm",
        hints,
        conn,
        model=plan_model(["alarm"]),
        embedder=FakeEmbedder({"alarm": 2}),
    )
    assert ids(result) and all(c.startswith("HP-") for c in ids(result))


def test_a_machine_with_no_manual_gives_no_passages_and_a_note(conn):
    hints = Hints(asset_id="XX-99", intent="troubleshoot")
    result = manual_agent(
        "q", hints, conn, model=plan_model(["q"]), embedder=FakeEmbedder()
    )
    assert result.passages == []
    assert any("XX-99" in note for note in result.notes)


def test_a_search_with_no_hit_is_noted(conn):
    result = manual_agent(
        "   ", Hints(), conn, model=plan_model([]), embedder=FakeEmbedder()
    )
    assert result.passages == [] and result.notes


def test_the_model_sees_the_question_and_what_triage_picked(conn):
    model = plan_model(["q"])
    manual_agent(QUESTION, HINTS, conn, model=model, embedder=FakeEmbedder())
    system, user = model.seen[0]
    assert system.type == "system" and "queries" in system.content
    assert QUESTION in user.content
    for text in ("E-217", "HP-01", "Hydraulic press", "BH-250"):
        assert text in user.content


def test_the_plan_call_is_booked_with_the_purpose(conn):
    manual_agent(
        QUESTION, HINTS, conn, model=plan_model(["q"]), embedder=FakeEmbedder()
    )
    (row,) = rows()
    assert (row["purpose"], row["model"], row["cache_hit"]) == (
        "manual agent",
        TERRA,
        0,
    )


def test_the_default_plan_is_a_structured_terra_call_at_low_effort(conn, monkeypatch):
    asked = []

    def fake_structured(
        role, schema, system, user, purpose, reasoning_effort="medium", **kw
    ):
        asked.append((role, schema, purpose, reasoning_effort))
        return SearchPlan(queries=["seal kit"])

    monkeypatch.setattr(agent_module, "structured", fake_structured)
    result = manual_agent(QUESTION, HINTS, conn, embedder=FakeEmbedder())
    assert asked == [("terra", SearchPlan, "manual agent", "low")]
    assert result.queries == ["seal kit", "seal leak"]


def test_the_settings_of_the_caller_go_to_the_default_plan_call(conn, monkeypatch):
    seen, given = {}, Settings()

    def fake(role, schema, system, user, purpose, effort="medium", **kwargs):
        seen.update(kwargs)
        return SearchPlan(queries=["seal"])

    monkeypatch.setattr(agent_module, "structured", fake)
    monkeypatch.setattr(llm, "load_settings", lambda: pytest.fail("loaded its own"))
    embedder = FakeEmbedder({"seal": 2})
    manual_agent(QUESTION, Hints(), conn, embedder=embedder, settings=given)
    assert seen == {"settings": given}


def test_the_settings_of_the_caller_go_to_the_call_config_of_a_given_model(
    conn, monkeypatch
):
    seen, given = [], Settings()
    real = agent_module.call_config

    def spy(purpose, settings=None):
        seen.append(settings)
        return real(purpose, given)

    monkeypatch.setattr(agent_module, "call_config", spy)
    manual_agent(
        QUESTION,
        Hints(),
        conn,
        model=plan_model(["seal"]),
        embedder=FakeEmbedder({"seal": 2}),
        settings=given,
    )
    assert seen == [given]
