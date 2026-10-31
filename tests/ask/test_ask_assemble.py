"""Tests of assemble, the last crew step, on its own. A hand-built crew state goes in and
the answer card comes out.
"""

import pytest
from crew_stubs import (  # noqa: F401
    FakeDecider,
    add_tool_clamp,
    build_crew_kg,
    choice,
    hp_facts,
    triage_answers,
)
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.ask import tools
from app.ask.assemble import build_card, rank_causes
from app.ask.safety import FIXED_LINE
from app.ask.synthesize import Synthesized
from app.ask.types import GraphFacts, ManualPassages, Triage

QUESTION = "HP-01 stopped with E-217, oil under the press"


@pytest.fixture
def kg(tmp_path):
    return build_crew_kg(tmp_path / "kg.sqlite")


@pytest.fixture
def conn(kg):
    connection = tools.connect_ro(kg)
    yield connection
    connection.close()


def state(facts, triage=None, question=QUESTION):
    return {
        "question": question,
        "triage": triage or Triage(asset_id="HP-01", alarm_code="E-217", status="act"),
        "facts": facts,
        "passages": ManualPassages(),
        "synthesis": Synthesized("", [], {}, []),
        "judged": [],
    }


def test_a_shown_fix_with_no_parts_in_the_facts_has_them_read_from_the_graph(kg, conn):
    facts = GraphFacts(
        asset_id="HP-01",
        alarm_code="E-217",
        causes=tools.alarm_causes(conn, "HP-01", "E-217"),
        fixes=tools.fixes_for(conn, "FM-SEAL-WEAR"),
    )
    assert facts.parts == []
    card = build_card(state(facts), kg)
    assert card.fix.procedure_id == "PROC-HP-04"
    assert card.parts == tools.parts_for(conn, "PROC-HP-04") and len(card.parts) == 4
    assert ("PROC-HP-04", "REQUIRES", "SEAL-KIT-50") in {
        (s.src, s.rel, s.dst) for s in card.path
    }
    assert any(
        n.startswith("OIL-HLP46-20L") and "not enough in stock" in n for n in card.notes
    )


def test_parts_the_agent_fetched_are_used_as_they_are(kg, conn):
    facts = hp_facts(conn)
    card = build_card(state(facts), kg)
    assert card.parts == [p for p in facts.parts if p.path[0].src == "PROC-HP-04"]


def test_only_the_causes_of_the_alarm_are_kept_when_the_facts_name_it(kg, conn):
    other = tools.alarm_causes(conn, "HP-01", "E-221")
    own = tools.alarm_causes(conn, "HP-01", "E-217")
    assert other and own
    facts = GraphFacts(asset_id="HP-01", alarm_code="E-217", causes=[*other, *own])
    card = build_card(state(facts), kg)
    assert card.causes == own[:3] and card.fix.procedure_id == "PROC-HP-04"


def test_the_alarm_of_the_triage_is_used_when_the_facts_have_none(kg, conn):
    other = tools.alarm_causes(conn, "HP-01", "E-221")
    own = tools.alarm_causes(conn, "HP-01", "E-217")
    facts = GraphFacts(asset_id="HP-01", causes=[*other, *own])
    assert build_card(state(facts), kg).causes == own[:3]
    assert [
        c.failure_mode_id
        for c in build_card(
            state(facts, Triage(asset_id="HP-01", alarm_code="E-221", status="act")), kg
        ).causes
    ] == ["FM-OIL-HOT"]


def test_with_no_alarm_known_the_causes_are_not_filtered(kg, conn):
    other = tools.alarm_causes(conn, "HP-01", "E-221")
    facts = GraphFacts(asset_id="HP-01", causes=other)
    triage = Triage(asset_id="HP-01", status="confirm")
    assert build_card(state(facts, triage), kg).causes == other


def test_a_fix_with_no_parts_at_all_shows_none(kg, conn):
    facts = GraphFacts(
        asset_id="HP-01",
        alarm_code="E-221",
        causes=tools.alarm_causes(conn, "HP-01", "E-221"),
    )
    triage = Triage(asset_id="HP-01", alarm_code="E-221", status="act")
    card = build_card(state(facts, triage), kg)
    assert card.fix.procedure_id == "PROC-HP-05" and card.parts == []


def test_a_failed_graph_agent_with_no_machine_known_gets_the_fixed_line_alone(kg):
    triage = Triage(status="confirm", safety_critical=0.1)
    card = build_card({**state(GraphFacts(), triage), "graph_failed": True}, kg)
    assert card.safety.required and card.safety.text == FIXED_LINE
    assert card.safety.reason == (
        "The graph lookup failed, so the lockout need is unknown."
    )


def test_a_failed_graph_agent_with_no_alarm_known_gets_the_machine_paragraph(kg):
    triage = Triage(asset_id="HP-01", status="act", safety_critical=0.1)
    trace = []
    card = build_card({**state(GraphFacts(), triage), "graph_failed": True}, kg, trace)
    assert card.causes == [] and card.safety.required
    assert "accumulator" in card.safety.text and card.safety.text.endswith(FIXED_LINE)
    assert any("not possible" in note and "alarm" in note for note in trace)


def test_the_code_fill_leaves_a_trace_note_for_each_step(kg, conn):
    trace = []
    card = build_card({**state(GraphFacts()), "graph_failed": True}, kg, trace)
    assert card.fix.procedure_id == "PROC-HP-04" and card.breakdowns
    by_code = "filled by code after the graph agent failed"
    assert [n.split(":")[0] for n in trace] == [by_code] * 3
    assert "alarm_causes(HP-01, E-217)" in trace[0]
    assert "similar_breakdowns(HP-01)" in trace[1]
    assert "PROC-HP-04" in trace[2]


def test_a_card_whose_graph_agent_worked_has_no_code_fill_notes(kg, conn):
    trace = []
    build_card(state(hp_facts(conn)), kg, trace)
    assert trace == []


# --- the cause that matches the symptom comes first ---


def triage_for(mode, p, alarm="E-217"):
    return Triage(
        asset_id="HP-01",
        alarm_code=alarm,
        failure_mode_id=mode,
        status="confirm",
        confidence={"root_cause": p},
    )


def test_the_cause_the_triage_picked_moves_first_and_is_marked(conn):
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    assert causes[0].failure_mode_id == "FM-SEAL-WEAR"  # the cause seen most often in the history
    ranked = rank_causes(causes, triage_for("FM-RELIEF-MISADJUSTED", 0.9))
    assert [c.failure_mode_id for c in ranked] == [
        "FM-RELIEF-MISADJUSTED",
        "FM-SEAL-WEAR",
        "FM-PUMP-WEAR",
        "FM-OIL-CONTAM",
    ]
    assert [c.matches_symptom for c in ranked] == [True, False, False, False]
    assert all(not c.matches_symptom for c in causes)  # the input is left unchanged


def test_a_triage_pick_at_exactly_the_confirm_gate_still_moves(conn):
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    ranked = rank_causes(causes, triage_for("FM-PUMP-WEAR", 0.4))
    assert ranked[0].failure_mode_id == "FM-PUMP-WEAR" and ranked[0].matches_symptom


def test_a_triage_pick_below_the_confirm_gate_leaves_the_order_alone(conn):
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    assert rank_causes(causes, triage_for("FM-RELIEF-MISADJUSTED", 0.39)) == causes
    assert rank_causes(causes, triage_for("FM-RELIEF-MISADJUSTED", None)) == causes
    assert rank_causes(causes, triage_for(None, 0.9)) == causes


def test_a_pick_that_is_not_a_cause_of_the_alarm_leaves_the_order_alone(conn):
    # a misadjusted relief valve is not a cause of E-221 on the press
    causes = tools.alarm_causes(conn, "HP-01", "E-221")
    assert [c.failure_mode_id for c in causes] == ["FM-OIL-HOT"]
    pick = triage_for("FM-RELIEF-MISADJUSTED", 0.9, "E-221")
    assert rank_causes(causes, pick) == causes


def test_a_cause_of_another_alarm_in_the_facts_is_not_a_match(conn):
    other = tools.alarm_causes(conn, "HP-01", "E-221")
    own = tools.alarm_causes(conn, "HP-01", "E-217")
    ranked = rank_causes([*other, *own], triage_for("FM-RELIEF-MISADJUSTED", 0.9))
    first_two = [c.failure_mode_id for c in ranked][:2]
    assert first_two == ["FM-RELIEF-MISADJUSTED", "FM-OIL-HOT"]
    # overheated oil is a cause of E-221, but the alarm found at triage is E-217
    pick = triage_for("FM-OIL-HOT", 0.9)
    assert rank_causes([*own, *other], pick) == [*own, *other]


def test_the_seal_wear_case_keeps_its_order(conn):
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    ranked = rank_causes(causes, triage_for("FM-SEAL-WEAR", 0.9))
    assert [c.failure_mode_id for c in ranked] == [c.failure_mode_id for c in causes]
    assert ranked == causes and not any(c.matches_symptom for c in ranked)


def test_the_code_fill_after_a_graph_failure_puts_the_matching_cause_first(kg):
    triage = triage_for("FM-RELIEF-MISADJUSTED", 0.9)
    card = build_card({**state(GraphFacts(), triage), "graph_failed": True}, kg)
    assert card.causes[0].failure_mode_id == "FM-RELIEF-MISADJUSTED"
    assert card.causes[0].matches_symptom
    assert card.fix.procedure_id == "PROC-HP-06"
    rest = [c.failure_mode_id for c in card.causes[1:]]
    assert rest == ["FM-SEAL-WEAR", "FM-PUMP-WEAR"]


def test_a_pick_below_the_gate_leaves_the_failed_graph_card_as_it_was(kg):
    triage = triage_for("FM-RELIEF-MISADJUSTED", 0.3)
    card = build_card({**state(GraphFacts(), triage), "graph_failed": True}, kg)
    assert card.causes[0].failure_mode_id == "FM-SEAL-WEAR"
    assert card.fix.procedure_id == "PROC-HP-04"


# --- a question with no alarm: the picked cause fills the gaps ---


def test_the_code_fill_with_no_alarm_reads_the_cause_the_triage_picked(kg):
    add_tool_clamp(kg)
    triage = Triage(
        asset_id="CNC-01",
        failure_mode_id="FM-TOOL-CLAMP",
        status="act",
        confidence={"root_cause": 0.99},
    )
    trace = []
    card = build_card({**state(GraphFacts(), triage), "graph_failed": True}, kg, trace)
    assert [c.failure_mode_id for c in card.causes] == ["FM-TOOL-CLAMP"]
    assert card.fix.procedure_id == "PROC-CNC-02"
    assert [p.part_no for p in card.parts] == ["CLAMP-KIT-1"]
    assert any("cause_on(CNC-01, FM-TOOL-CLAMP)" in note for note in trace)
    assert not any("not possible" in note for note in trace)
    assert any("similar_breakdowns(CNC-01)" in note for note in trace)


def test_the_code_fill_with_no_alarm_and_no_cause_is_still_not_possible(kg):
    triage = Triage(asset_id="CNC-01", status="act", confidence={"root_cause": None})
    trace = []
    card = build_card({**state(GraphFacts(), triage), "graph_failed": True}, kg, trace)
    assert card.causes == [] and card.safety.required
    assert any("not possible" in note and "alarm" in note for note in trace)
