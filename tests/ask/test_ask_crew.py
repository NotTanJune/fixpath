"""Tests of the whole troubleshooting crew: triage, the two search agents, the
synthesizer, the verifier and the final assembly. Every model is scripted, the decider
is a fake, and the data is the small crew test graph, so nothing touches the network.
"""

import re
import socket
import sqlite3
import threading

import pytest
from ask_stubs import FakeEmbedder, calls, plan_model, say, terra_model
from crew_stubs import (
    CONFLICT_INCIDENT,
    FakeDecider,
    RaisingModel,
    add_tool_clamp,
    build_crew_kg,
    choice,
    noul,
    sol_model,
    triage_answers,
)
from llm_stubs import SETTINGS, SOL, TERRA, isolated  # noqa: F401  (an autouse fixture)

from app import config, ledger, llm
from app.config import Settings
from app.ask import crew, tools
from app.ask.crew import Models, ask, stream_ask
from app.ask.safety import FIXED_LINE
from app.ask.types import AnswerCard
from app.decide import DecisionError, NoDecisionBackend
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey, NoCacheInstalled

QUESTION = "HP-01 stopped with E-217, oil under the press"
CAUSES = ("alarm_causes", {"asset_id": "HP-01", "code": "E-217"})
FIXES = [
    ("fixes_for", {"failure_mode_id": mode})
    for mode in ("FM-SEAL-WEAR", "FM-RELIEF-MISADJUSTED", "FM-SPINDLE-WEAR")
]
TOP_NODES = [
    "triage",
    "graph_agent",
    "manual_agent",
    "synthesize",
    "verify",
    "assemble",
]


@pytest.fixture
def kg(tmp_path):
    return build_crew_kg(tmp_path / "kg.sqlite")


@pytest.fixture
def conn(kg):
    connection = tools.connect_ro(kg)
    yield connection
    connection.close()


def supported(qid_p=0.9):
    return choice(
        "supported", qid_p, {"partly": 0.05, "not_supported": 1 - qid_p - 0.05}
    )


def script(triage=None, verdicts=None):
    """A decider script: the triage answers, and a verdict for each claim (supported by
    default)."""

    def answer(purpose, state, questions):
        if purpose == "triage":
            return triage or triage_answers()
        return {qid: (verdicts or {}).get(qid, supported()) for qid in questions}

    return answer


def crew_models(
    claims=None, summary="Seal wear is the likely cause. Check the seals first."
):
    return Models(
        graph=terra_model(script=[calls(CAUSES, *FIXES), say()]),
        manual=plan_model(["seal kit", "oil leak"]),
        synth=sol_model(
            summary,
            claims
            or [
                ("Seals wear first on this press.", ["C1"]),
                ("Past breakdowns had oil on the floor.", ["C2"]),
            ],
        ),
        embedder=FakeEmbedder(),
    )


def run(kg, decider=None, models=None, question=QUESTION, history=None):
    decider = decider or FakeDecider(script())
    models = models or crew_models()
    card = ask(
        question, history, kg_path=kg, settings=SETTINGS, decider=decider, models=models
    )
    return card, decider, models


# --- the order of the nodes ---


def test_on_act_the_nodes_run_triage_then_both_agents_then_synthesize_verify_assemble(
    kg,
):
    card, decider, models = run(kg)
    names = [t.name for t in card.trace]
    assert names[0] == "triage" and names[-3:] == ["synthesize", "verify", "assemble"]
    assert sorted(names[1:3]) == ["graph_agent", "manual_agent"]
    assert isinstance(card, AnswerCard) and card.triage.status == "act"
    assert decider.purposes() == ["triage", "verify"]
    assert (
        len(models.graph.seen) == 2
        and len(models.manual.seen) == 1
        and len(models.synth.seen) == 1
    )


def test_a_node_starts_only_after_the_nodes_before_it_have_ended(kg):
    card, *_ = run(kg)
    by = {t.name: t for t in card.trace}
    assert by["triage"].ended_at <= min(
        by["graph_agent"].started_at, by["manual_agent"].started_at
    )
    assert by["synthesize"].started_at >= max(
        by["graph_agent"].ended_at, by["manual_agent"].ended_at
    )
    assert by["verify"].started_at >= by["synthesize"].ended_at
    assert by["assemble"].started_at >= by["verify"].ended_at


def test_the_two_agents_run_in_parallel(kg, monkeypatch):
    # each waits for the other at a barrier, which opens only when both are running
    barrier = threading.Barrier(2, timeout=10)
    graph_agent, manual_agent = crew.graph_agent, crew.manual_agent

    def graph(*args, **kwargs):
        barrier.wait()
        return graph_agent(*args, **kwargs)

    def manual(*args, **kwargs):
        barrier.wait()
        return manual_agent(*args, **kwargs)

    monkeypatch.setattr(crew, "graph_agent", graph)
    monkeypatch.setattr(crew, "manual_agent", manual)
    card, *_ = run(kg)
    by = {t.name: t for t in card.trace}
    assert by["graph_agent"].started_at < by["manual_agent"].ended_at
    assert by["manual_agent"].started_at < by["graph_agent"].ended_at
    assert card.causes  # and both results arrived


# --- the short paths ---


def test_an_unknown_machine_clarifies_with_chips_and_nothing_else_runs(kg):
    answers = triage_answers(
        machine=choice("unknown", 0.9, {"RW-01": 0.06, "CNC-01": 0.02, "HP-01": 0.01}),
    )
    card, decider, models = run(
        kg, FakeDecider(script(triage=answers)), question="Line 4 robot is down"
    )
    assert card.triage.status == "clarify"
    assert [c.value for c in card.triage.chips] == ["RW-01", "CNC-01", "HP-01"]
    assert card.triage.reason
    assert [t.name for t in card.trace] == ["triage", "assemble"]
    assert decider.purposes() == ["triage"]
    assert (
        models.graph.seen == [] and models.manual.seen == [] and models.synth.seen == []
    )
    assert (
        card.summary,
        card.causes,
        card.fix,
        card.parts,
        card.breakdowns,
        card.claims,
    ) == (
        None,
        [],
        None,
        [],
        [],
        [],
    )
    assert not card.safety.required


def test_a_low_machine_probability_clarifies_too(kg):
    answers = triage_answers(
        machine=choice("HP-01", 0.3, {"CNC-01": 0.3, "CNC-02": 0.2}),
        alarm=choice("none", 0.9),
    )
    card, _, models = run(
        kg, FakeDecider(script(triage=answers)), question="something is leaking"
    )
    assert card.triage.status == "clarify"
    assert models.synth.seen == []


def test_out_of_scope_stops_with_the_reason(kg):
    answers = triage_answers(intent=choice("out_of_scope", 0.95))
    card, decider, models = run(
        kg,
        FakeDecider(script(triage=answers)),
        question="Who is on the night shift roster?",
    )
    assert card.triage.status == "out_of_scope"
    assert "shift lead" in card.triage.reason
    assert [t.name for t in card.trace] == ["triage", "assemble"]
    assert decider.purposes() == ["triage"] and models.graph.seen == []


def test_a_vague_symptom_confirms_and_the_crew_runs_with_the_best_guess(kg):
    answers = triage_answers(
        machine=choice("HP-01", 0.55, {"CNC-01": 0.3, "CNC-02": 0.1}),
        alarm=choice("none", 0.9),
        root_cause=choice("unknown", 0.9),
    )
    models = Models(
        graph=terra_model(script=[say()]),
        manual=plan_model(["oil leak"]),
        synth=sol_model(
            "Maybe a seal.", [("Oil under a press points to a seal.", ["C1"])]
        ),
        embedder=FakeEmbedder(),
    )
    card, _, _ = run(
        kg,
        FakeDecider(script(triage=answers)),
        models,
        question="there is oil under it",
    )
    assert card.triage.status == "confirm"
    assert [(c.value, c.p) for c in card.triage.chips] == [
        ("HP-01", 0.55),
        ("CNC-01", 0.3),
    ]
    assert card.triage.asset_id == "HP-01"
    assert [t.name for t in card.trace][-1] == "assemble" and "synthesize" in [
        t.name for t in card.trace
    ]
    assert card.causes == []  # no alarm, so no causes were looked up
    assert any("no alarm code" in note for note in card.notes)


def test_the_safety_flag_reaches_a_clarify_card(kg):
    answers = triage_answers(
        machine=choice("unknown", 0.9, {"HP-01": 0.05}),
        alarm=choice("none", 0.9),
        safety=noul(0.95),
    )
    card, *_ = run(
        kg, FakeDecider(script(triage=answers)), question="I have to open a guard"
    )
    assert card.triage.status == "clarify"
    assert card.safety.required and card.safety.text == FIXED_LINE


# --- the events ---


def stream(kg, **kwargs):
    decider = kwargs.pop("decider", None) or FakeDecider(script())
    return list(
        stream_ask(
            kwargs.pop("question", QUESTION),
            None,
            kg_path=kg,
            settings=SETTINGS,
            decider=decider,
            models=kwargs.pop("models", None) or crew_models(),
        )
    )


def test_stream_ask_yields_v2_updates_and_custom_chunks(kg):
    chunks = stream(kg)
    assert {c["type"] for c in chunks} == {"updates", "custom"}
    assert all({"type", "ns", "data"} <= set(c) for c in chunks)
    updated = [name for c in chunks if c["type"] == "updates" for name in c["data"]]
    assert sorted(updated) == sorted(TOP_NODES)
    final = [c for c in chunks if c["type"] == "updates" and "assemble" in c["data"]][0]
    assert isinstance(final["data"]["assemble"]["card"], AnswerCard)


def test_every_node_emits_a_start_and_an_end_event_with_timing_and_cost(kg):
    events = [c["data"] for c in stream(kg) if c["type"] == "custom"]
    for name in TOP_NODES:
        node = [e for e in events if e["node"] == name]
        assert [e["event"] for e in node] == ["node_start", "node_end"], name
        start, end = node
        assert start["at"] <= end["ended_at"] and end["started_at"] <= end["ended_at"]
        assert end["seconds"] >= 0 and end["cost_usd"] >= 0
        assert {"backend", "cache_hit"} <= set(end)
    order = [(e["node"], e["event"]) for e in events]
    assert order[0] == ("triage", "node_start") and order[1] == ("triage", "node_end")
    assert order[-1] == ("assemble", "node_end")


def test_the_short_path_emits_only_triage_and_assemble_events(kg):
    answers = triage_answers(machine=choice("unknown", 0.9, {"HP-01": 0.05}))
    events = [
        c["data"]
        for c in stream(kg, decider=FakeDecider(script(triage=answers)))
        if c["type"] == "custom"
    ]
    assert [(e["node"], e["event"]) for e in events] == [
        ("triage", "node_start"),
        ("triage", "node_end"),
        ("assemble", "node_start"),
        ("assemble", "node_end"),
    ]


def test_the_trace_carries_the_backend_cache_flag_and_cost_from_the_ledger(kg):
    def booking(purpose, state, questions):
        ledger.record(config.JEV_MODEL, purpose, 1000, 0, 0, purpose == "verify")
        return script()(purpose, state, questions)

    card, *_ = run(kg, FakeDecider(booking))
    by = {t.name: t for t in card.trace}
    assert by["triage"].backend == config.JEV_MODEL and by["triage"].cache_hit is False
    assert by["triage"].cost_usd == pytest.approx(1000 * 0.042 / 1e6)
    assert by["verify"].cache_hit is True and by["verify"].cost_usd == 0
    assert by["graph_agent"].backend and by["graph_agent"].cost_usd > 0
    assert by["manual_agent"].cost_usd > 0 and by["synthesize"].cost_usd > 0
    assert by["assemble"].backend is None and by["assemble"].cache_hit is None
    assert by["assemble"].cost_usd == 0
    total = sum(t.cost_usd for t in card.trace)
    assert total == pytest.approx(ledger.spent_usd())


# --- the card ---


def test_the_card_facts_equal_the_tool_results(kg, conn):
    card, *_ = run(kg)
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    assert len(causes) == 4 and len(card.causes) == 3
    assert card.causes == causes[:3]
    seal_fix = tools.fixes_for(conn, "FM-SEAL-WEAR")[0]
    assert card.fix == seal_fix and card.fix.steps == ["Stop the pump.", "Do the work."]
    assert card.parts == tools.parts_for(conn, "PROC-HP-04")
    expected = tools.similar_breakdowns(
        conn, "HP-01", "E-217", "FM-SEAL-WEAR", QUESTION
    )
    assert card.breakdowns == expected[:3] and len(card.breakdowns) == 3


def test_other_fixes_come_only_from_fixes_resolved_by_a_card_cause(kg, conn):
    card, *_ = run(kg)
    assert [f.procedure_id for f in card.other_fixes] == ["PROC-HP-06"]
    shown = {card.fix.procedure_id, *(f.procedure_id for f in card.other_fixes)}
    assert "PROC-CNC-01" not in shown  # found by the agent, but not a cause on the press


def test_the_fixes_of_a_card_cause_the_agent_did_not_fetch_are_read_from_the_graph(
    kg, conn
):
    models = crew_models()
    models.graph = terra_model(
        script=[calls(CAUSES), say()]
    )  # the agent looked up no fixes
    card, *_ = run(kg, models=models)
    assert card.fix == tools.fixes_for(conn, "FM-SEAL-WEAR")[0]
    assert [f.procedure_id for f in card.other_fixes] == ["PROC-HP-06"]
    assert card.other_fixes[0] == tools.fixes_for(conn, "FM-RELIEF-MISADJUSTED")[0]


def test_parts_are_only_those_of_the_fix_that_is_shown(kg, conn):
    card, *_ = run(kg)
    assert {p.path[0].src for p in card.parts} == {"PROC-HP-04"}
    assert {p.part_no for p in card.parts} == {
        "SEAL-KIT-50",
        "OIL-HLP46-20L",
        "WIPER-CYL-50",
        "RELIEF-SPRING-1",
    }


def test_the_lockout_warning_is_built_from_the_graph(kg):
    card, *_ = run(kg)
    assert card.safety.required and card.safety.procedure_ids == ["PROC-HP-04"]
    assert card.safety.text.endswith(FIXED_LINE) and "accumulator" in card.safety.text
    assert [c.chunk_id for c in card.safety.citations] == ["HP-LOTO", "HP-WARN"]


def test_the_safety_flag_alone_requires_the_block(kg):
    # no alarm and no root cause, so no fix is looked up; the safety flag forces the block
    answers = triage_answers(
        alarm=choice("none", 0.9), root_cause=choice("unknown", 0.9), safety=noul(0.9)
    )
    models = Models(
        graph=terra_model(script=[say()]),
        manual=plan_model(["oil leak"]),
        synth=sol_model("S.", [("Claim.", ["C1"])]),
        embedder=FakeEmbedder(),
    )
    card, *_ = run(
        kg,
        FakeDecider(script(triage=answers)),
        models,
        question="open the guard on the press",
    )
    assert card.fix is None and card.safety.required
    assert card.safety.procedure_ids == [] and "hazard" in card.safety.reason.lower()
    assert "accumulator" in card.safety.text


def test_the_stock_notes(kg):
    card, *_ = run(kg)
    stock = [n for n in card.notes if "stock" in n]
    assert (
        "OIL-HLP46-20L (Hydraulic oil 20 L): not enough in stock: 1 on hand, lead time 14 days"
        in stock
    )
    assert (
        "RELIEF-SPRING-1 (Relief valve spring): not enough in stock: 0 on hand, lead time 21 days"
        in stock
    )
    assert any(n.startswith("WIPER-CYL-50") and "not known" in n for n in stock)
    assert not any(n.startswith("SEAL-KIT-50") for n in stock)  # 3 on hand, 1 needed


def test_a_breakdown_with_an_open_conflict_gets_a_note(kg):
    card, *_ = run(kg)
    assert CONFLICT_INCIDENT in [b.incident_id for b in card.breakdowns]
    note = f"{CONFLICT_INCIDENT}: sources disagree on this record (downtime_min: 240 vs 250)"
    assert note in card.notes
    assert sum("sources disagree" in n for n in card.notes) == 1


def test_the_verified_claims_and_the_unverified_ones(kg):
    verdicts = {
        "claim-2": choice("not_supported", 0.9, {"supported": 0.05, "partly": 0.05})
    }
    card, decider, _ = run(kg, FakeDecider(script(verdicts=verdicts)))
    assert [c.id for c in card.claims] == ["claim-1"]
    assert card.claims[0].verdict == "supported" and card.claims[0].p == pytest.approx(
        0.9
    )
    assert [(c.id, c.verdict) for c in card.unverified] == [("claim-2", "unsupported")]
    assert set(card.citations) == {"C1", "C2"}
    assert card.summary == "Seal wear is the likely cause. Check the seals first."


def test_the_card_path_holds_the_steps_of_what_is_shown(kg):
    card, *_ = run(kg)
    keys = {(s.src, s.rel, s.dst) for s in card.path}
    assert ("E-217", "INDICATES", "FM-SEAL-WEAR") in keys
    assert ("FM-SEAL-WEAR", "RESOLVED_BY", "PROC-HP-04") in keys
    assert ("PROC-HP-04", "REQUIRES", "SEAL-KIT-50") in keys
    assert not any(dst == "PROC-CNC-01" for _, _, dst in keys)
    assert len(card.path) == len(keys)


def test_the_notes_of_the_agents_are_kept(kg):
    models = crew_models()
    models.graph = terra_model(
        script=[calls(("alarm_causes", {"asset_id": "HP-01"})), say()]
    )
    card, *_ = run(kg, models=models)
    assert any("bad arguments for alarm_causes" in note for note in card.notes)


# --- the entry points ---


def test_ask_turns_the_model_cache_on_and_takes_the_decider_from_the_settings(
    kg, monkeypatch
):
    seen = []
    decider = FakeDecider(script())
    monkeypatch.setattr(
        crew.llm_cache, "enable_cache", lambda *a, **k: seen.append("cache")
    )
    monkeypatch.setattr(
        crew, "get_decider", lambda settings: seen.append("decider") or decider
    )
    ask(QUESTION, kg_path=kg, settings=SETTINGS, models=crew_models())
    assert seen == ["cache", "decider"]
    assert (
        next(
            iter(
                stream_ask(
                    QUESTION, kg_path=kg, settings=SETTINGS, models=crew_models()
                )
            )
        )
        is not None
    )
    assert seen == ["cache", "decider", "cache", "decider"]


class Watched:
    """A connection that remembers whether it was closed."""

    def __init__(self, connection):
        self.connection, self.closed = connection, False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        self.closed = True
        self.connection.close()


def test_every_question_opens_its_own_read_only_connection_and_closes_it(
    kg, monkeypatch
):
    opened = []
    real = tools.connect_ro

    def spy(path):
        opened.append(Watched(real(path)))
        return opened[-1]

    monkeypatch.setattr(crew.tools, "connect_ro", spy)
    run(kg)
    first = len(opened)
    run(kg)
    assert (
        first == 4 and len(opened) == 8
    )  # triage, both agents and assemble, each time
    assert len({id(c.connection) for c in opened}) == 8
    assert all(c.closed for c in opened)


def test_a_missing_database_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError, match="no graph database"):
        ask(
            QUESTION,
            kg_path=tmp_path / "none.sqlite",
            settings=SETTINGS,
            decider=FakeDecider(script()),
        )


def test_the_database_is_opened_read_only(kg):
    before = kg.read_bytes()
    run(kg)
    assert kg.read_bytes() == before


# --- the trap: a line the plant does not have ---


def test_a_line_the_plant_does_not_have_clarifies_even_when_the_decider_is_sure(kg):
    machine = choice("RW-01", 0.86, {"HP-01": 0.06, "CNC-01": 0.03})
    decider = FakeDecider(script(triage=triage_answers(machine=machine)))
    card, decider, models = run(kg, decider, question="Line 4 robot is down")
    assert card.triage.status == "clarify" and card.triage.asset_id is None
    assert [c.value for c in card.triage.chips] == ["RW-01", "HP-01", "CNC-01"]
    assert (
        card.triage.reason
        == "There is no Line 4 in this plant; did you mean RW-01 on Line 1?"
    )
    assert [t.name for t in card.trace] == ["triage", "assemble"]
    assert decider.purposes() == ["triage"]
    assert (
        models.graph.seen == [] and models.manual.seen == [] and models.synth.seen == []
    )


def test_the_typed_machine_id_gets_the_retrieval_and_the_safety_block(kg):
    machine = choice("RW-01", 0.97, {"HP-01": 0.01})
    decider = FakeDecider(script(triage=triage_answers(machine=machine)))
    card, *_ = run(kg, decider, question="HP-01 on Line 4 leaking, E-217")
    assert card.triage.status == "confirm" and card.triage.asset_id == "HP-01"
    chips = [c.value for c in card.triage.chips if c.field == "machine"]
    assert chips == ["HP-01", "RW-01"] and card.fix.procedure_id == "PROC-HP-04"
    assert card.safety.required and "accumulator" in card.safety.text


# --- a failing node still returns a card ---

FLAGGED = triage_answers(safety=noul(0.9))


def failing(node: str, error: BaseException | None = None) -> Models:
    models = crew_models()
    broken = RaisingModel(
        model=TERRA, callbacks=[llm.CostTracker(SETTINGS)], error=error
    )
    if node == "synthesize":
        broken = RaisingModel(
            model=SOL, callbacks=[llm.CostTracker(SETTINGS)], error=error
        )
    setattr(
        models,
        {"graph_agent": "graph", "manual_agent": "manual", "synthesize": "synth"}[node],
        broken,
    )
    return models


def test_a_failing_graph_agent_still_returns_a_card_with_the_safety_block(kg):
    card, *_ = run(kg, FakeDecider(script(triage=FLAGGED)), failing("graph_agent"))
    assert card.safety.required and "accumulator" in card.safety.text
    assert card.triage.status == "act" and card.trace[-1].name == "assemble"
    graph = next(t for t in card.trace if t.name == "graph_agent")
    assert any(
        "RuntimeError" in note and "the provider is down" in note
        for note in graph.notes
    )


def test_a_failing_manual_agent_still_returns_a_card_with_the_graph_facts(kg, conn):
    card, *_ = run(kg, FakeDecider(script(triage=FLAGGED)), failing("manual_agent"))
    assert "the manual search failed; the card shows the graph facts only" in card.notes
    assert card.causes == tools.alarm_causes(conn, "HP-01", "E-217")[:3]
    assert card.fix and card.safety.required and card.summary
    manual = next(t for t in card.trace if t.name == "manual_agent")
    assert any("RuntimeError" in note for note in manual.notes)


def test_a_failing_synthesizer_still_returns_a_card_without_a_summary(kg, conn):
    card, decider, _ = run(
        kg, FakeDecider(script(triage=FLAGGED)), failing("synthesize")
    )
    assert (
        "the summary could not be written; the card shows the graph facts without it"
        in card.notes
    )
    assert card.summary is None and card.claims == [] and card.unverified == []
    assert card.causes == tools.alarm_causes(conn, "HP-01", "E-217")[:3]
    assert card.fix and card.safety.required
    assert decider.purposes() == ["triage"]  # nothing to verify
    synth = next(t for t in card.trace if t.name == "synthesize")
    assert any("RuntimeError" in note for note in synth.notes)


LOW = triage_answers(safety=noul(0.1))
UNKNOWN_LOCKOUT = "the graph lookup failed, so the lockout need is unknown"


def both_failing() -> Models:
    models = failing("graph_agent")
    models.manual = failing("manual_agent").manual
    return models


def assemble_notes(card) -> list[str]:
    return next(t for t in card.trace if t.name == "assemble").notes


def test_a_failed_graph_agent_does_not_cost_the_card_its_facts(kg, conn):
    card, *_ = run(kg, FakeDecider(script(triage=LOW)), failing("graph_agent"))
    assert card.fix.procedure_id == "PROC-HP-04"
    assert card.causes == tools.alarm_causes(conn, "HP-01", "E-217")[:3]
    assert card.parts == tools.parts_for(conn, "PROC-HP-04") and card.parts
    assert card.breakdowns  # filled in by the final step too
    assert card.safety.required and card.safety.procedure_ids == ["PROC-HP-04"]
    assert "accumulator" in card.safety.text and card.safety.text.endswith(FIXED_LINE)
    assert UNKNOWN_LOCKOUT not in card.safety.reason.casefold()  # the lockout is known
    by_code = "filled by code after the graph agent failed"
    assert any(by_code in n for n in assemble_notes(card))
    assert not any("filled by code" in n for n in card.notes)  # the card keeps no developer messages


def test_the_card_note_for_a_failed_graph_agent_says_what_the_card_shows(kg):
    card, *_ = run(kg, FakeDecider(script(triage=LOW)), failing("graph_agent"))
    assert "the card shows the manual passages only" not in " ".join(card.notes)
    assert any(n.startswith("the graph lookup failed") for n in card.notes)


def test_a_failed_graph_agent_with_no_alarm_known_requires_the_block(kg):
    answers = triage_answers(
        alarm=choice("none", 0.9), root_cause=choice("unknown", 0.9), safety=noul(0.1)
    )
    decider = FakeDecider(script(triage=answers))
    card, *_ = run(kg, decider, failing("graph_agent"), question="the press is down")
    assert card.triage.asset_id == "HP-01" and card.triage.alarm_code is None
    assert card.causes == [] and card.fix is None
    assert card.safety.required and card.safety.reason == (
        "The graph lookup failed, so the lockout need is unknown."
    )
    assert "accumulator" in card.safety.text and card.safety.text.endswith(FIXED_LINE)


def test_a_failed_graph_agent_and_a_machine_with_no_causes_requires_the_block(kg):
    answers = triage_answers(machine=choice("RW-01", 0.97), safety=noul(0.1))
    decider = FakeDecider(script(triage=answers))
    card, *_ = run(kg, decider, failing("graph_agent"), question="RW-01 E-217")
    assert card.causes == [] and card.fix is None
    assert card.safety.required and UNKNOWN_LOCKOUT in card.safety.reason.casefold()
    assert card.safety.text == FIXED_LINE  # the robot has no manual on record


def test_a_failed_read_of_the_graph_requires_the_block_too(kg, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tools, "alarm_causes", broken)
    card, *_ = run(kg, FakeDecider(script(triage=LOW)), failing("graph_agent"))
    assert card.causes == [] and card.fix is None
    assert card.safety.required and UNKNOWN_LOCKOUT in card.safety.reason.casefold()
    assert "accumulator" in card.safety.text


def test_a_fix_the_code_fill_finds_without_lockout_is_authoritative(kg):
    answers = triage_answers(alarm=choice("E-221", 0.95), safety=noul(0.1))
    card, *_ = run(kg, FakeDecider(script(triage=answers)), failing("graph_agent"))
    assert card.fix.procedure_id == "PROC-HP-05" and not card.fix.requires_loto
    assert card.safety.required is False


def test_both_agents_failing_still_gives_the_facts_and_the_block(kg, conn):
    card, decider, _ = run(kg, FakeDecider(script(triage=LOW)), both_failing())
    assert card.fix.procedure_id == "PROC-HP-04" and card.summary is None
    assert card.safety.required and "accumulator" in card.safety.text
    assert "the manual search failed; the card shows the graph facts only" in card.notes
    assert any(n.startswith("the graph lookup failed") for n in card.notes)


@pytest.mark.parametrize("node", ["graph_agent", "manual_agent", "synthesize"])
@pytest.mark.parametrize(
    "error",
    [
        BudgetExceeded("spent"),
        MissingApiKey(),
        NoCacheInstalled(),
        NoDecisionBackend("none"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_the_stop_errors_still_propagate_from_the_model_nodes(kg, node, error):
    with pytest.raises(type(error)):
        run(kg, models=failing(node, error))


# --- the card keeps one alarm and never shows a fix without its parts ---


def test_the_card_keeps_the_alarm_triage_named_when_the_agent_asked_for_another_first(
    kg, conn
):
    first = ("alarm_causes", {"asset_id": "HP-01", "code": "E-221"})
    models = crew_models()
    models.graph = terra_model(script=[calls(first), calls(CAUSES, *FIXES), say()])
    card, *_ = run(kg, models=models)
    assert [c.failure_mode_id for c in card.causes] == [
        c.failure_mode_id for c in tools.alarm_causes(conn, "HP-01", "E-217")[:3]
    ]
    assert all(c.path[0].src == "E-217" for c in card.causes)
    assert card.fix.procedure_id == "PROC-HP-04" and "FM-OIL-HOT" not in str(card.path)
    assert card.parts == tools.parts_for(conn, "PROC-HP-04") and card.parts


# --- the cause that matches the symptom comes first ---


def relief_triage():
    return triage_answers(root_cause=choice("FM-RELIEF-MISADJUSTED", 0.9))


def test_the_card_leads_with_the_cause_the_triage_picked_and_shows_its_fix(kg, conn):
    models = crew_models()
    card, *_ = run(kg, FakeDecider(script(triage=relief_triage())), models)
    assert [c.failure_mode_id for c in card.causes] == [
        "FM-RELIEF-MISADJUSTED",
        "FM-SEAL-WEAR",
        "FM-PUMP-WEAR",
    ]
    assert [c.matches_symptom for c in card.causes] == [True, False, False]
    assert card.fix.procedure_id == "PROC-HP-06"
    assert [f.procedure_id for f in card.other_fixes] == ["PROC-HP-04"]
    assert "PROC-HP-06" in str(card.path) and card.parts == []


def test_the_synthesizer_reads_the_causes_in_the_same_order_as_the_card(kg):
    models = crew_models()
    run(kg, FakeDecider(script(triage=relief_triage())), models)
    (prompt,) = [m.content for m in models.synth.seen[0] if "Cause 1" in str(m.content)]
    assert "Cause 1: relief valve misadjustment" in prompt
    first, second = "Cause 1: relief valve", "Cause 2: main cylinder seal wear"
    assert prompt.index(first) < prompt.index(second)


def test_the_breakdowns_follow_the_cause_that_now_leads(kg):
    models = crew_models()
    card, *_ = run(kg, FakeDecider(script(triage=relief_triage())), models)
    assert card.breakdowns[0].root_cause_id == "FM-RELIEF-MISADJUSTED"
    assert card.breakdowns[0].incident_id == "INC-2026-003"


def test_the_breakdowns_are_unchanged_when_no_cause_moves(kg, conn):
    card, *_ = run(kg)
    assert card.causes[0].failure_mode_id == "FM-SEAL-WEAR"
    assert all(b.root_cause_id == "FM-SEAL-WEAR" for b in card.breakdowns)


def test_a_pick_below_the_gate_leaves_the_card_in_its_history_order(kg):
    triage = triage_answers(root_cause=choice("FM-RELIEF-MISADJUSTED", 0.3))
    card, *_ = run(kg, FakeDecider(script(triage=triage)))
    assert card.causes[0].failure_mode_id == "FM-SEAL-WEAR"
    assert not any(c.matches_symptom for c in card.causes)


# --- a question with no alarm still gets its cause, fix and parts ---


NO_ALARM = "tool not clamping after change, machine stops on CNC1 w/ no alarm"


@pytest.fixture
def clamp_kg(kg):
    return add_tool_clamp(kg)


def clamp_models():
    return Models(
        graph=terra_model(script=[say()]),
        manual=plan_model(["tool clamp"]),
        synth=sol_model("The clamp is not holding.", [("Replace the clamp.", ["C1"])]),
        embedder=FakeEmbedder(),
    )


def clamp_triage(p=0.99):
    return triage_answers(
        machine=choice("CNC-01", 0.97),
        alarm=choice("E-217", 0.6),
        root_cause=choice("FM-TOOL-CLAMP", p),
    )


def test_a_question_with_no_alarm_gets_the_picked_cause_with_its_fix_and_parts(
    clamp_kg,
):
    decider = FakeDecider(script(triage=clamp_triage()))
    card, *_ = run(clamp_kg, decider, clamp_models(), question=NO_ALARM)
    assert card.triage.alarm_code is None and card.triage.status == "act"
    assert [c.failure_mode_id for c in card.causes] == ["FM-TOOL-CLAMP"]
    assert card.fix.procedure_id == "PROC-CNC-02"
    assert [p.part_no for p in card.parts] == ["CLAMP-KIT-1"]
    assert not any("no alarm code is known" in note for note in card.notes)
    assert card.safety.required  # the clamp procedure needs lockout


def test_a_question_with_no_alarm_and_no_root_cause_keeps_the_old_note(clamp_kg):
    triage = triage_answers(
        machine=choice("CNC-01", 0.97),
        alarm=choice("none", 0.9),
        root_cause=choice("unknown", 0.9),
    )
    decider = FakeDecider(script(triage=triage))
    card, *_ = run(clamp_kg, decider, clamp_models(), question=NO_ALARM)
    assert card.causes == [] and card.fix is None
    assert "no alarm code is known, so no causes were looked up" in card.notes


def test_a_root_cause_below_the_gate_keeps_the_old_note(clamp_kg):
    triage = clamp_triage(0.3)
    decider = FakeDecider(script(triage=triage))
    card, *_ = run(clamp_kg, decider, clamp_models(), question=NO_ALARM)
    assert card.causes == [] and card.triage.failure_mode_id is None
    assert "no alarm code is known, so no causes were looked up" in card.notes


def test_a_failed_graph_agent_with_no_alarm_still_gets_the_cause_by_code(clamp_kg):
    models = clamp_models()
    models.graph = RaisingModel()
    decider = FakeDecider(script(triage=clamp_triage()))
    card, *_ = run(clamp_kg, decider, models, question=NO_ALARM)
    assert [c.failure_mode_id for c in card.causes] == ["FM-TOOL-CLAMP"]
    assert card.fix.procedure_id == "PROC-CNC-02" and card.parts


# --- the summary is checked like the claims ---


def verify_state(decider):
    (call,) = [state for purpose, state, _ in decider.calls if purpose == "verify"]
    return call


def test_the_summary_goes_to_the_verifier_as_one_more_claim_with_the_citations_of_the_claims(
    kg,
):
    card, decider, _ = run(kg)
    state = verify_state(decider)
    summary = next(c for c in state["claims"] if c["id"] == "summary")
    assert summary["text"] == "Seal wear is the likely cause. Check the seals first."
    others = [c for c in state["claims"] if c["id"] != "summary"]
    union = [q for c in others for q in c["quotes"]]
    assert summary["quotes"] == list(dict.fromkeys(union))
    assert card.summary == summary["text"]
    assert [c.id for c in card.claims] == [
        "claim-1",
        "claim-2",
    ]  # the summary is not a claim on the card
    assert card.unverified == []


def test_a_summary_the_verifier_rejects_is_not_shown_and_leaves_a_note(kg):
    verdicts = {
        "summary": choice("not_supported", 0.9, {"supported": 0.05, "partly": 0.05})
    }
    card, *_ = run(kg, FakeDecider(script(verdicts=verdicts)))
    assert card.summary is None
    unsupported = "the summary was not supported by its sources, so it is not shown"
    assert unsupported in card.notes
    assert "could not be checked" not in " ".join(card.notes)
    assert [c.id for c in card.claims] == [
        "claim-1",
        "claim-2",
    ] and card.unverified == []
    verify_trace = next(t for t in card.trace if t.name == "verify")
    assert any("Seal wear is the likely cause." in note for note in verify_trace.notes)
    assert not any("Seal wear is the likely cause." in note for note in card.notes)


def test_a_summary_split_between_supported_and_partly_is_shown(kg):
    # the rehearsal summary: supported 0.47, partly 0.42, not_supported 0.11
    verdicts = {
        "summary": choice("supported", 0.47, {"partly": 0.42, "not_supported": 0.11})
    }
    card, *_ = run(kg, FakeDecider(script(verdicts=verdicts)))
    assert card.summary == "Seal wear is the likely cause. Check the seals first."
    assert not any("not shown" in note or "not certain" in note for note in card.notes)


def test_a_summary_with_too_little_support_says_it_was_not_certain_enough(kg):
    verdicts = {
        "summary": choice("partly", 0.3, {"supported": 0.1, "not_supported": 0.2})
    }
    card, *_ = run(kg, FakeDecider(script(verdicts=verdicts)))
    assert card.summary is None
    assert "the summary's support was not certain enough to show it" in card.notes
    assert not any("not supported by its sources" in note for note in card.notes)
    verify_trace = next(t for t in card.trace if t.name == "verify")
    assert any(
        "support 0.40" in note and "partly" in note for note in verify_trace.notes
    )


def test_the_trace_note_of_a_rejected_summary_shows_the_support_mass(kg):
    verdicts = {
        "summary": choice("not_supported", 0.9, {"supported": 0.05, "partly": 0.05})
    }
    card, *_ = run(kg, FakeDecider(script(verdicts=verdicts)))
    verify_trace = next(t for t in card.trace if t.name == "verify")
    assert any("support 0.10" in note for note in verify_trace.notes)


def test_a_summary_that_restates_a_part_loses_that_sentence(kg):
    models = crew_models(
        summary="Replace SEAL-KIT-50 now. Seal wear is the likely cause."
    )
    card, decider, _ = run(kg, models=models)
    assert card.summary == "Seal wear is the likely cause."
    synth = next(t for t in card.trace if t.name == "synthesize")
    assert any("SEAL-KIT-50" in note for note in synth.notes)
    assert not any("SEAL-KIT-50" in note and "summary" in note for note in card.notes)
    assert (
        verify_state(decider)["claims"][-1]["text"] == "Seal wear is the likely cause."
    )


def test_a_summary_with_no_kept_claim_is_not_shown(kg):
    models = crew_models(claims=[("Invented.", ["C99"])])
    card, decider, _ = run(kg, models=models)
    assert card.summary is None and card.claims == []
    assert (
        "the summary could not be checked against its sources, so it is not shown"
        in card.notes
    )
    assert "verify" not in decider.purposes()


# --- the safety block fails safe ---


def test_a_decider_error_on_triage_still_requires_the_block_for_a_known_machine(kg):
    def broken(purpose, state, questions):
        if purpose == "triage":
            return DecisionError("backend failed")
        return {qid: supported() for qid in questions}

    models = Models(
        graph=terra_model(script=[say()]),
        manual=plan_model(["fire"]),
        synth=sol_model("S.", [("Claim.", ["C1"])]),
        embedder=FakeEmbedder(),
    )
    card, *_ = run(kg, FakeDecider(broken), models, question="the press is on fire")
    assert card.triage.asset_id == "HP-01" and card.triage.status == "confirm"
    assert card.safety.required and "accumulator" in card.safety.text
    assert "could not be answered" in card.safety.reason
    assert card.safety.text.endswith(FIXED_LINE)


# --- part and history questions ---


def quiet_models():
    return Models(
        graph=terra_model(script=[say()]),
        manual=plan_model(["stock"]),
        synth=sol_model("S.", [("Claim.", ["C1"])]),
        embedder=FakeEmbedder(),
    )


def test_a_part_question_lists_the_stock_of_the_part_it_names(kg, conn):
    answers = triage_answers(
        alarm=choice("none", 0.9), intent=choice("find_part", 0.95)
    )
    card, *_ = run(
        kg,
        FakeDecider(script(triage=answers)),
        quiet_models(),
        question="Is SEAL-KIT-50 in stock?",
    )
    (stock,) = card.stock
    assert stock == tools.part_stock(conn, "SEAL-KIT-50")
    assert (stock.qty_on_hand, stock.bin, stock.fits) == (3, "B-12", ["HP-01"])
    assert [c.doc_path for c in stock.citations] == ["inventory/spares.xlsx"]


def test_a_part_question_with_no_machine_still_gets_the_stock(kg):
    answers = triage_answers(
        machine=choice("unknown", 0.9, {"HP-01": 0.05}),
        alarm=choice("none", 0.9),
        intent=choice("find_part", 0.95),
    )
    card, _, models = run(
        kg,
        FakeDecider(script(triage=answers)),
        quiet_models(),
        question="Is SEAL-KIT-50 in stock?",
    )
    assert card.triage.status == "clarify" and models.synth.seen == []
    assert [s.part_no for s in card.stock] == ["SEAL-KIT-50"]
    assert [c.value for c in card.triage.chips] == ["HP-01"]
    assert card.triage.reason == (
        "Stock for SEAL-KIT-50 is below. Pick a machine to see the fix it is used in."
    )
    assert (card.stock[0].qty_on_hand, card.stock[0].bin) == (3, "B-12")


def test_a_fault_question_that_names_no_part_has_no_stock(kg):
    card, *_ = run(kg)
    assert card.stock == []


def test_a_part_named_in_a_fault_question_is_listed_too(kg):
    card, *_ = run(kg, question="HP-01 E-217: is the SK-50 in stock?")
    assert [s.part_no for s in card.stock] == ["SEAL-KIT-50"]


def test_a_history_question_shows_up_to_five_breakdowns_and_the_alarm_history(kg, conn):
    answers = triage_answers(
        root_cause=choice("unknown", 0.9), intent=choice("history", 0.95)
    )
    card, *_ = run(
        kg,
        FakeDecider(script(triage=answers)),
        quiet_models(),
        question="What happened the last times HP-01 raised E-217?",
    )
    assert card.breakdowns == tools.similar_breakdowns(
        conn, "HP-01", "E-217", None, None, 5
    )
    assert [b.incident_id for b in card.breakdowns] == [
        "INC-2026-003",
        "INC-2026-002",
        "INC-2026-001",
    ]
    assert card.history == tools.alarm_history(conn, "HP-01", "E-217")
    assert (card.history.count, card.history.incidents) == (15, 3)
    assert ("HP-01", "RAISED", "E-217") in {(s.src, s.rel, s.dst) for s in card.path}


def test_a_history_question_with_no_alarm_shows_the_five_latest_breakdowns_of_the_machine(
    kg, conn
):
    answers = triage_answers(
        alarm=choice("none", 0.9),
        root_cause=choice("unknown", 0.9),
        intent=choice("history", 0.95),
    )
    card, *_ = run(
        kg,
        FakeDecider(script(triage=answers)),
        quiet_models(),
        question="What broke on HP-01 lately?",
    )
    assert len(card.breakdowns) == 5 and card.history is None
    assert card.breakdowns == tools.similar_breakdowns(
        conn, "HP-01", None, None, None, 5
    )


def test_a_fault_question_still_shows_three_breakdowns_and_no_history(kg):
    card, *_ = run(kg)
    assert len(card.breakdowns) == 3 and card.history is None


# --- the events and the trace ---

STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")


def test_every_event_uses_one_timestamp_format_iso_8601_utc_with_z(kg):
    events = [c["data"] for c in stream(kg) if c["type"] == "custom"]
    stamps = [
        e[key] for e in events for key in ("at", "started_at", "ended_at") if key in e
    ]
    assert len(stamps) == 3 * len(TOP_NODES)
    assert all(STAMP.match(s) for s in stamps), stamps
    for name in TOP_NODES:
        start, end = [e for e in events if e["node"] == name]
        assert start["at"] == end["started_at"]


def test_dropped_claim_text_goes_to_the_trace_and_not_to_the_card_notes(kg):
    models = crew_models(claims=[("Invented.", ["C99"]), ("Good.", ["C1"])])
    card, *_ = run(kg, models=models)
    assert not any("Invented." in note or "C99" in note for note in card.notes)
    synth = next(t for t in card.trace if t.name == "synthesize")
    assert any("Invented." in note and "C99" in note for note in synth.notes)
    assert "graph note" not in card.notes  # only the real agents add notes
    assert next(t for t in card.trace if t.name == "triage").notes == []


def test_the_still_asking_for_tools_note_goes_to_the_trace_not_the_card(kg):
    models = crew_models()
    models.graph = terra_model(script=[calls(CAUSES)])  # asks again every round
    card, *_ = run(kg, models=models)
    assert not any("still asking" in note for note in card.notes)
    graph = next(t for t in card.trace if t.name == "graph_agent")
    assert any("still asking for tools after 6 rounds" in note for note in graph.notes)


# --- the settings of the caller reach every model node ---


@pytest.mark.parametrize("default_node", ["graph", "manual", "synth"])
def test_a_default_model_node_gets_the_settings_of_the_caller_and_never_loads_its_own(
    kg, monkeypatch, default_node
):
    """The test setup hands out settings with a fake key. A step that loaded its own
    settings would use that key and try the network. A step that gets the caller's
    keyless settings stops at the budget check, so a cache miss raises an error and no
    connection is ever opened."""
    connects = []

    def refuse(self, address):  # record the attempt and fail at once
        connects.append(address)
        raise ConnectionRefusedError(address)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    assert llm.load_settings().has_openai
    models = crew_models()
    setattr(models, default_node, None)  # this step builds its own model
    with pytest.raises(MissingApiKey):
        ask(
            QUESTION,
            kg_path=kg,
            settings=Settings(),
            decider=FakeDecider(script()),
            models=models,
        )
    assert connects == []


def test_no_node_loads_settings_of_its_own_when_it_is_given_them(kg, monkeypatch):
    loaded = []
    monkeypatch.setattr(llm, "load_settings", lambda: loaded.append(1) or SETTINGS)
    ask(
        QUESTION,
        kg_path=kg,
        settings=SETTINGS,
        decider=FakeDecider(script()),
        models=crew_models(),
    )
    assert loaded == []  # no step loads settings of its own
