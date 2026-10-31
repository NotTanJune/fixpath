"""Tests of the graph agent, which looks things up in the knowledge graph with tools. A
scripted model drives it over the small test graph, and nothing touches the network.
"""

import json
import sqlite3

import pytest
from ask_stubs import calls, open_kg, say, terra_model
from langchain_core.messages import AIMessage, ToolMessage
from llm_stubs import SETTINGS, TERRA, StubChatModel, isolated, rows  # noqa: F401

from app import llm, llm_cache
from app.ask import graph_agent as agent_module
from app.ask.graph_agent import graph_agent
from app.ask.types import Hints, PathStep
from app.config import Settings

QUESTION = "HP-01 stopped with E-217, oil under the press"
HINTS = Hints(asset_id="HP-01", alarm_code="E-217", intent="troubleshoot")
CAUSES = ("alarm_causes", {"asset_id": "HP-01", "code": "E-217"})
FIXES = ("fixes_for", {"failure_mode_id": "FM-SEAL-WEAR"})
PARTS = ("parts_for", {"procedure_id": "PROC-HP-04"})
BREAKDOWNS = ("similar_breakdowns", {"asset_id": "HP-01", "code": "E-217"})


@pytest.fixture
def conn(tmp_path):
    connection = open_kg(tmp_path)
    yield connection
    connection.close()


def summary(facts):
    return [(c.name, c.filled_by, c.result_count) for c in facts.tool_calls]


def tool_messages(model, call_index):
    """The tool messages the model saw on one of its calls."""
    return [m for m in model.seen[call_index] if isinstance(m, ToolMessage)]


# --- the loop ---


def test_the_six_tools_are_bound_to_the_model(conn):
    model = terra_model(script=[say()])
    graph_agent(QUESTION, HINTS, conn, model=model)
    (bound,) = model.bound
    names = [tool["function"]["name"] for tool in bound]
    assert names == [
        "find_machine",
        "alarm_causes",
        "fixes_for",
        "parts_for",
        "similar_breakdowns",
        "alarm_history",
    ]
    assert all(tool["function"]["description"] for tool in bound)


def test_the_model_sees_the_question_and_the_hints(conn):
    model = terra_model(script=[say()])
    graph_agent(QUESTION, HINTS, conn, model=model)
    system, user = model.seen[0]
    assert system.type == "system" and user.type == "human"
    assert QUESTION in user.content
    assert (
        '"asset_id": "HP-01"' in user.content
        and '"alarm_code": "E-217"' in user.content
    )


def test_a_scripted_run_calls_two_tools_and_code_completes_the_rest(conn):
    model = terra_model(script=[calls(CAUSES, FIXES), say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    assert summary(facts) == [
        ("alarm_causes", "agent", 3),
        ("fixes_for", "agent", 1),
        ("parts_for", "code", 3),
        ("similar_breakdowns", "code", 4),
    ]
    assert [c.failure_mode_id for c in facts.causes][0] == "FM-SEAL-WEAR"
    assert [f.procedure_id for f in facts.fixes] == ["PROC-HP-04"]
    assert [p.part_no for p in facts.parts] == [
        "OIL-HLP46-20L",
        "SEAL-KIT-50",
        "WIPER-CYL-50",
    ]
    assert facts.breakdowns[0].incident_id.startswith("INC-2026-")
    assert (facts.asset_id, facts.alarm_code) == ("HP-01", "E-217")
    assert PathStep(src="E-217", rel="INDICATES", dst="FM-SEAL-WEAR") in facts.path
    assert (
        PathStep(src="FM-SEAL-WEAR", rel="RESOLVED_BY", dst="PROC-HP-04") in facts.path
    )
    assert PathStep(src="PROC-HP-04", rel="REQUIRES", dst="SEAL-KIT-50") in facts.path
    assert len(facts.path) == len(set(map(PathStep.model_dump_json, facts.path)))


def test_each_tool_result_goes_back_with_the_matching_call_id(conn):
    model = terra_model(script=[calls(CAUSES, FIXES), say()])
    graph_agent(QUESTION, HINTS, conn, model=model)
    assert len(model.seen) == 2  # one call per round
    first_round = model.seen[1]
    assert isinstance(first_round[-3], AIMessage) and first_round[-3].tool_calls
    answers = tool_messages(model, 1)
    assert [m.tool_call_id for m in answers] == ["call_0", "call_1"]
    causes = json.loads(answers[0].content)
    assert causes[0]["failure_mode_id"] == "FM-SEAL-WEAR"
    assert "citations" not in causes[0] and "path" not in causes[0]


def test_a_run_where_the_agent_fetched_everything_needs_no_fill(conn):
    model = terra_model(script=[calls(CAUSES, FIXES), calls(PARTS, BREAKDOWNS), say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    assert {c.filled_by for c in facts.tool_calls} == {"agent"}
    assert len(facts.tool_calls) == 4
    assert len(model.seen) == 3


def test_when_the_agent_calls_nothing_code_fetches_the_whole_set(conn):
    facts = graph_agent(
        QUESTION, HINTS, conn, model=terra_model(script=[say("nothing")])
    )
    assert summary(facts) == [
        ("alarm_causes", "code", 3),
        ("fixes_for", "code", 1),
        ("parts_for", "code", 3),
        ("similar_breakdowns", "code", 4),
    ]
    assert facts.tool_calls[1].args == {"failure_mode_id": "FM-SEAL-WEAR"}
    assert facts.tool_calls[3].args["code"] == "E-217"


def test_code_fetches_the_parts_of_every_fix_of_the_top_cause(conn):
    # a misadjusted relief valve is not the top cause, so its fix is left alone
    facts = graph_agent(QUESTION, HINTS, conn, model=terra_model(script=[say()]))
    assert [f.procedure_id for f in facts.fixes] == ["PROC-HP-04"]
    assert [c.args for c in facts.tool_calls if c.name == "parts_for"] == [
        {"procedure_id": "PROC-HP-04"}
    ]


def test_the_fill_is_for_troubleshooting_only(conn):
    hints = Hints(asset_id="HP-01", alarm_code="E-217", intent="part_lookup")
    model = terra_model(script=[calls(PARTS), say()])
    facts = graph_agent("what does the seal job need?", hints, conn, model=model)
    assert summary(facts) == [("parts_for", "agent", 3)]
    assert facts.causes == [] and facts.fixes == [] and facts.breakdowns == []


def test_without_a_known_machine_nothing_is_filled_and_a_note_says_so(conn):
    hints = Hints(intent="troubleshoot")
    facts = graph_agent(
        "something is leaking", hints, conn, model=terra_model(script=[say()])
    )
    assert facts.tool_calls == [] and facts.asset_id is None
    assert any("machine" in note for note in facts.notes)


def test_a_machine_the_agent_finds_by_name_is_used_for_the_fill(conn):
    hints = Hints(intent="troubleshoot")
    model = terra_model(script=[calls(("find_machine", {"text": "the press"})), say()])
    facts = graph_agent("the press is leaking", hints, conn, model=model)
    assert facts.asset_id == "HP-01"
    assert [c.name for c in facts.tool_calls] == ["find_machine", "similar_breakdowns"]
    assert facts.tool_calls[1].filled_by == "code"
    assert facts.breakdowns


def test_a_name_that_matches_two_machines_or_none_leaves_the_machine_unknown(
    conn, monkeypatch
):
    hints = Hints(intent="troubleshoot")
    two = [{"id": "HP-01", "score": 100.0}, {"id": "CNC-01", "score": 100.0}]
    for found in (two, []):
        monkeypatch.setattr(agent_module.tools, "find_machine", lambda c, text: found)
        model = terra_model(script=[calls(("find_machine", {"text": "xx"})), say()])
        facts = graph_agent("q", hints, conn, model=model)
        assert facts.asset_id is None
        assert [c.name for c in facts.tool_calls] == ["find_machine"]


@pytest.mark.parametrize(
    ("text", "noted"),
    [
        ("HP-02", None),  # shaped like an id, so the machine lookup returns nothing
        ("Press 3", "close"),  # a near match for HP-01's alias Press 2, which is not used
        ("KX-500", "CNC-01, CNC-02"),  # a model: two lathes, so neither is chosen
    ],
)
def test_only_an_exact_single_hit_becomes_the_machine(conn, text, noted):
    hints = Hints(intent="troubleshoot")
    model = terra_model(script=[calls(("find_machine", {"text": text})), say()])
    facts = graph_agent(f"{text} stopped", hints, conn, model=model)
    assert facts.asset_id is None
    assert [c.name for c in facts.tool_calls] == ["find_machine"]
    assert facts.causes == [] and facts.breakdowns == []
    if noted:
        assert any("find_machine" in n and noted in n for n in facts.notes), facts.notes


def test_a_model_with_one_machine_is_an_exact_hit(conn):
    model = terra_model(script=[calls(("find_machine", {"text": "BH-250"})), say()])
    facts = graph_agent(
        "oil under the press", Hints(intent="troubleshoot"), conn, model=model
    )
    assert facts.asset_id == "HP-01" and facts.breakdowns


def test_a_known_failure_mode_stands_in_for_the_top_cause_without_an_alarm(conn):
    hints = Hints(
        asset_id="HP-01", failure_mode_id="FM-SEAL-WEAR", intent="troubleshoot"
    )
    facts = graph_agent(
        "seals are leaking", hints, conn, model=terra_model(script=[say()])
    )
    assert [(c.name, c.filled_by) for c in facts.tool_calls] == [
        ("cause_on", "code"),
        ("fixes_for", "code"),
        ("parts_for", "code"),
        ("similar_breakdowns", "code"),
    ]
    assert [c.failure_mode_id for c in facts.causes] == ["FM-SEAL-WEAR"]
    assert facts.causes == [agent_module.tools.cause_on(conn, "HP-01", "FM-SEAL-WEAR")]
    assert [f.procedure_id for f in facts.fixes] == ["PROC-HP-04"]
    assert {p.part_no for p in facts.parts} >= {"SEAL-KIT-50"}
    assert facts.notes == []  # the note that no causes were looked up is gone
    path = {(s.src, s.rel, s.dst) for s in facts.path}
    assert ("HP-01", "HAS_COMPONENT", "HP-01-MAINCYL") in path


def test_with_no_alarm_and_no_root_cause_the_old_note_stays(conn):
    hints = Hints(asset_id="HP-01", intent="troubleshoot")
    model = terra_model(script=[say()])
    facts = graph_agent("something leaks", hints, conn, model=model)
    assert [c.name for c in facts.tool_calls] == ["similar_breakdowns"]
    assert facts.causes == []
    assert facts.notes == ["no alarm code is known, so no causes were looked up"]


def test_a_root_cause_that_is_not_on_the_machine_keeps_the_note_and_adds_no_cause(conn):
    hints = Hints(asset_id="HP-01", failure_mode_id="FM-SPINDLE-WEAR")
    facts = graph_agent("q", hints, conn, model=terra_model(script=[say()]))
    assert facts.causes == []
    assert any("no alarm code is known" in note for note in facts.notes)
    first = facts.tool_calls[0]
    assert (first.name, first.result_count) == ("cause_on", 0)


def test_the_symptom_mark_stays_out_of_what_the_model_reads(conn):
    # it is set after the agent runs, and a new key in a tool result would change
    # every cached request of the loop
    model = terra_model(script=[calls(CAUSES), say()])
    graph_agent(QUESTION, HINTS, conn, model=model)
    (message,) = tool_messages(model, 1)
    assert "matches_symptom" not in message.content
    assert "likelihood" in message.content


def test_the_cause_fill_is_not_offered_to_the_model(conn):
    model = terra_model(script=[say()])
    hints = Hints(asset_id="HP-01", failure_mode_id="FM-SEAL-WEAR")
    graph_agent("q", hints, conn, model=model)
    (bound,) = model.bound
    assert "cause_on" not in [tool["function"]["name"] for tool in bound]


def test_a_sqlite_error_in_the_cause_fill_is_noted_and_the_run_goes_on(
    conn, monkeypatch
):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk")

    monkeypatch.setattr(agent_module.tools, "cause_on", boom)
    hints = Hints(asset_id="HP-01", failure_mode_id="FM-SEAL-WEAR")
    facts = graph_agent("q", hints, conn, model=terra_model(script=[say()]))
    assert facts.causes == []
    assert any("cause_on" in note and "failed" in note for note in facts.notes)
    assert facts.breakdowns  # the remaining steps still ran


def test_an_alarm_without_causes_on_the_machine_is_noted(conn):
    hints = Hints(asset_id="HP-01", alarm_code="E-214", intent="troubleshoot")
    facts = graph_agent("q", hints, conn, model=terra_model(script=[say()]))
    assert [c.name for c in facts.tool_calls] == ["alarm_causes", "similar_breakdowns"]
    assert facts.causes == []
    assert any("E-214" in note for note in facts.notes)


# --- one machine ---


WRONG_MACHINE = [
    ("alarm_causes", {"asset_id": "CNC-01", "code": "E-217"}),
    ("similar_breakdowns", {"asset_id": "CNC-01"}),
]


def test_a_call_about_another_machine_is_refused_and_its_facts_are_not_kept(conn):
    model = terra_model(script=[calls(CAUSES, BREAKDOWNS, *WRONG_MACHINE), say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    assert {c.asset_id for c in facts.causes} == {"HP-01"}
    assert {b.asset_id for b in facts.breakdowns} == {"HP-01"}
    assert "FM-SPINDLE-WEAR" not in [c.failure_mode_id for c in facts.causes]
    assert not any(b.incident_id == "INC-2026-006" for b in facts.breakdowns)
    assert facts.asset_id == "HP-01"
    refused = [c for c in facts.tool_calls if c.args.get("asset_id") == "CNC-01"]
    assert [(c.name, c.filled_by, c.result_count) for c in refused] == [
        ("alarm_causes", "agent", 0),
        ("similar_breakdowns", "agent", 0),
    ]
    answers = tool_messages(model, 1)
    assert [m.tool_call_id for m in answers] == ["call_0", "call_1", "call_2", "call_3"]
    assert answers[2].content == (
        "Error: this question is about HP-01; facts for CNC-01 were not fetched"
    )
    assert sum("were not fetched" in n and "CNC-01" in n for n in facts.notes) == 2
    assert not any("CNC-01" in step.src + step.dst for step in facts.path)


def test_the_machine_is_pinned_by_the_first_call_that_finds_facts(conn):
    hints = Hints(intent="troubleshoot")
    first = ("alarm_causes", {"asset_id": "CNC-02", "code": "E-217"})
    other = ("alarm_causes", {"asset_id": "HP-01", "code": "E-217"})
    model = terra_model(script=[calls(first, other), say()])
    facts = graph_agent("q", hints, conn, model=model)
    assert facts.asset_id == "CNC-02"
    assert {c.asset_id for c in facts.causes} == {"CNC-02"}
    assert {b.asset_id for b in facts.breakdowns} == {"CNC-02"}
    (answer_0, answer_1) = tool_messages(model, 1)
    assert "this question is about CNC-02; facts for HP-01" in answer_1.content


def test_a_machine_found_by_name_pins_the_run(conn):
    hints = Hints(intent="troubleshoot")
    model = terra_model(
        script=[calls(("find_machine", {"text": "the press"}), WRONG_MACHINE[0]), say()]
    )
    facts = graph_agent("the press is leaking oil", hints, conn, model=model)
    assert facts.asset_id == "HP-01"
    assert facts.causes == []  # CNC-01's causes were refused, and no alarm is known
    assert facts.breakdowns and {b.asset_id for b in facts.breakdowns} == {"HP-01"}
    assert any("facts for CNC-01 were not fetched" in n for n in facts.notes)


def test_a_call_with_the_machine_in_lower_case_is_the_same_machine(conn):
    lower = ("alarm_causes", {"asset_id": "hp-01", "code": "e-217"})
    facts = graph_agent(
        QUESTION, HINTS, conn, model=terra_model(script=[calls(lower), say()])
    )
    assert facts.tool_calls[0].result_count == 3
    assert not any("were not fetched" in n for n in facts.notes)


def test_an_empty_history_of_another_machine_is_dropped_from_the_facts(conn):
    # CNC-01 never raised E-221, so its history is empty and does not fix the result
    empty = ("alarm_history", {"asset_id": "CNC-01", "code": "E-221"})
    hints = Hints(intent="troubleshoot")
    model = terra_model(script=[calls(empty, CAUSES), say()])
    facts = graph_agent("q", hints, conn, model=model)
    assert facts.asset_id == "HP-01"
    assert [h.asset_id for h in facts.history] == []
    assert {c.asset_id for c in facts.causes} == {"HP-01"}


def test_ids_the_model_types_in_lower_case_match_the_fill(conn):
    lower = ("fixes_for", {"failure_mode_id": "fm-seal-wear"})
    facts = graph_agent(
        QUESTION, HINTS, conn, model=terra_model(script=[calls(lower), say()])
    )
    assert [c.name for c in facts.tool_calls].count("fixes_for") == 1  # no second lookup


# --- bad calls ---


def test_an_unknown_tool_is_answered_with_an_error_and_counted(conn):
    model = terra_model(
        script=[calls(("drop_table", {"name": "nodes"}), CAUSES), say()]
    )
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    first, second, *_ = facts.tool_calls
    assert (first.name, first.filled_by, first.result_count) == (
        "drop_table",
        "agent",
        0,
    )
    assert second.name == "alarm_causes" and second.result_count == 3
    answers = tool_messages(model, 1)
    assert [m.tool_call_id for m in answers] == ["call_0", "call_1"]
    assert "unknown tool" in answers[0].content and "alarm_causes" in answers[0].content
    assert any("drop_table" in note for note in facts.notes)


@pytest.mark.parametrize(
    "args",
    [
        {"asset_id": "HP-01"},  # the alarm code is missing
        {
            "asset_id": "HP-01",
            "code": "E-217",
            "extra": 1,
        },  # an argument that does not exist
        {"asset_id": 5, "code": ["E-217"]},  # the wrong types
        {},
    ],
)
def test_bad_arguments_are_answered_with_an_error_and_counted(conn, args):
    model = terra_model(script=[calls(("alarm_causes", args)), say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    first = facts.tool_calls[0]
    assert (first.name, first.args, first.filled_by, first.result_count) == (
        "alarm_causes",
        args,
        "agent",
        0,
    )
    (answer,) = tool_messages(model, 1)
    assert answer.content.startswith("Error:") and "alarm_causes" in answer.content
    # the bad call found nothing, so the later steps still fill in the facts
    assert facts.causes and facts.tool_calls[1].filled_by == "code"


def test_a_call_with_arguments_that_are_not_json_is_answered_too(conn):
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "alarm_causes",
                "args": "{oops",
                "id": "call_9",
                "error": "bad json",
                "type": "invalid_tool_call",
            }
        ],
    )
    model = terra_model(script=[broken, say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    (answer,) = tool_messages(model, 1)
    assert answer.tool_call_id == "call_9" and answer.content.startswith("Error:")
    assert (facts.tool_calls[0].name, facts.tool_calls[0].result_count) == (
        "alarm_causes",
        0,
    )


@pytest.mark.parametrize("call_id", [None, "call_9"])
def test_an_invalid_call_without_an_id_or_a_name_is_answered_not_raised(conn, call_id):
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": None,
                "args": "{oops",
                "id": call_id,
                "error": "bad json",
                "type": "invalid_tool_call",
            },
            {
                "name": "alarm_causes",
                "args": None,
                "id": None,
                "error": "bad json",
                "type": "invalid_tool_call",
            },
        ],
    )
    model = terra_model(script=[broken, say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    first, second = tool_messages(model, 1)
    assert first.tool_call_id == (call_id or "invalid_0") and first.name == "unknown"
    assert second.tool_call_id == "invalid_1" and second.name == "alarm_causes"
    assert first.content.startswith("Error:") and second.content.startswith("Error:")
    assert [c.name for c in facts.tool_calls[:2]] == ["unknown", "alarm_causes"]
    assert facts.causes  # and the later steps still ran


def test_a_tool_that_raises_a_sqlite_error_is_answered_and_the_fill_goes_on(
    conn, monkeypatch
):
    def broken(conn_, failure_mode_id):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(agent_module.tools, "fixes_for", broken)
    model = terra_model(script=[calls(CAUSES, FIXES), say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    answers = tool_messages(model, 1)
    assert answers[1].content.startswith("Error:") and "malformed" in answers[1].content
    assert any("fixes_for" in n and "malformed" in n for n in facts.notes)
    names = [(c.name, c.filled_by, c.result_count) for c in facts.tool_calls]
    assert ("fixes_for", "agent", 0) in names
    assert ("similar_breakdowns", "code", 4) in names  # the later steps ran to the end
    assert facts.causes and facts.breakdowns


def test_a_sqlite_error_in_the_fill_does_not_end_the_run(conn, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("locked")

    monkeypatch.setattr(agent_module.tools, "similar_breakdowns", broken)
    facts = graph_agent(QUESTION, HINTS, conn, model=terra_model(script=[say()]))
    assert facts.causes and facts.breakdowns == []
    assert any("similar_breakdowns" in n and "locked" in n for n in facts.notes)


# --- limits ---


def test_the_loop_stops_after_six_rounds(conn):
    forever = calls(("find_machine", {"text": "hp"}))
    model = terra_model(script=[forever])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    assert len(model.seen) == agent_module.MAX_ROUNDS == 6
    agent_calls = [c for c in facts.tool_calls if c.filled_by == "agent"]
    assert len(agent_calls) == 6  # the calls of the last round still run
    assert any("round" in note for note in facts.notes)
    assert facts.causes  # and the later steps still ran


def test_no_round_note_when_the_last_reply_asks_for_no_tool(conn):
    one = [calls(("find_machine", {"text": "hp"})) for _ in range(5)]
    facts = graph_agent(QUESTION, HINTS, conn, model=terra_model(script=[*one, say()]))
    assert not any("round" in note for note in facts.notes)


def test_the_loop_stops_running_tools_after_eight_calls(conn):
    six = calls(*[("find_machine", {"text": f"hp {i}"}) for i in range(6)])
    model = terra_model(script=[six, six, say()])
    facts = graph_agent(QUESTION, HINTS, conn, model=model)
    agent_calls = [c for c in facts.tool_calls if c.filled_by == "agent"]
    assert len(agent_calls) == agent_module.MAX_TOOL_CALLS == 8
    answers = tool_messages(model, 2)
    assert len(answers) == 12  # every call is answered, or the service would reject the request
    refused = [m for m in answers if "limit" in m.content]
    assert len(refused) == 4
    assert [m.tool_call_id for m in answers[6:]] == [f"call_{i}" for i in range(6)]
    assert any("limit" in note for note in facts.notes)


# --- where the facts come from ---


def test_facts_come_from_the_tools_and_never_from_the_model_text(conn):
    lie = AIMessage(
        content="The cause is FM-FAKE-CAUSE at 99 percent. Fit part FAKE-PART-1 from bin Z-99.",
        tool_calls=[
            {
                "name": "alarm_causes",
                "args": {"asset_id": "HP-01", "code": "E-999"},
                "id": "call_0",
                "type": "tool_call",
            }
        ],
    )
    final = say("Everything is fine, the cause is FM-FAKE-CAUSE.")
    hints = Hints(asset_id="HP-01", alarm_code="E-999", intent="troubleshoot")
    facts = graph_agent(QUESTION, hints, conn, model=terra_model(script=[lie, final]))
    dumped = facts.model_dump_json()
    assert "FM-FAKE-CAUSE" not in dumped and "FAKE-PART-1" not in dumped
    assert facts.causes == [] and facts.parts == []


def test_the_facts_hold_the_citations_the_tools_read(conn):
    facts = graph_agent(QUESTION, HINTS, conn, model=terra_model(script=[say()]))
    assert facts.causes[0].citations[0].doc_path == "manuals/hp.pdf"
    assert facts.fixes[0].citations[0].page == 5


# --- the ledger and the cache ---


def test_every_model_call_in_the_loop_books_one_ledger_row_with_the_purpose(conn):
    model = terra_model(script=[calls(CAUSES), calls(FIXES), say()])
    graph_agent(QUESTION, HINTS, conn, model=model)
    assert len(model.seen) == 3
    booked = rows()
    assert [row["purpose"] for row in booked] == ["graph agent"] * 3
    assert [row["model"] for row in booked] == [TERRA] * 3
    assert [row["cache_hit"] for row in booked] == [0, 0, 0]


def test_a_second_run_replays_every_round_from_the_cache(conn, tmp_path):
    llm_cache.enable_cache(tmp_path / "cache.sqlite")
    script = [calls(CAUSES, FIXES), say()]
    first = terra_model(script=script)
    one = graph_agent(QUESTION, HINTS, conn, model=first)
    second = terra_model(script=script)
    two = graph_agent(QUESTION, HINTS, conn, model=second)
    assert len(first.seen) == 2 and second.seen == []  # nothing reached the model
    assert two.model_dump() == one.model_dump()
    assert [row["cache_hit"] for row in rows()] == [0, 0, 1, 1]


def test_the_replay_fix_lives_in_the_cache_not_in_the_agent():
    assert not hasattr(agent_module, "_as_first_returned")


def test_the_loop_never_streams(conn):
    class NoStream(type(terra_model())):
        def _stream(self, *args, **kwargs):
            raise AssertionError("the loop must use invoke")

    graph_agent(QUESTION, HINTS, conn, model=NoStream(script=[say()]))


def test_the_default_model_is_terra_at_low_effort(conn, monkeypatch):
    asked = []

    def fake_chat_model(role, reasoning_effort="medium", *, settings=None):
        asked.append((role, reasoning_effort))
        return terra_model(script=[say()])

    monkeypatch.setattr(llm, "chat_model", fake_chat_model)
    monkeypatch.setattr(agent_module, "chat_model", fake_chat_model)
    graph_agent(QUESTION, HINTS, conn)
    assert asked == [("terra", "low")]


def test_settings_are_loaded_once_and_given_to_the_model_and_the_config(
    conn, monkeypatch
):
    loaded, seen = [], {}

    def load():
        loaded.append(1)
        return SETTINGS

    def fake_chat_model(role, reasoning_effort="medium", *, settings=None):
        seen["model"] = settings
        return terra_model(script=[say()])

    def fake_call_config(purpose, settings=None):
        seen["config"] = settings
        return {"metadata": {"purpose": purpose}}

    monkeypatch.setattr(llm, "load_settings", load)
    monkeypatch.setattr(agent_module, "chat_model", fake_chat_model)
    monkeypatch.setattr(agent_module, "call_config", fake_call_config)
    graph_agent(QUESTION, HINTS, conn)
    assert len(loaded) == 1
    assert seen["model"] is SETTINGS and seen["config"] is SETTINGS


def test_settings_that_are_given_are_used_and_nothing_is_loaded(conn, monkeypatch):
    loaded, seen = [], {}
    given = Settings()

    def fake_chat_model(role, reasoning_effort="medium", *, settings=None):
        seen["model"] = settings
        return terra_model(script=[say()])

    def fake_call_config(purpose, settings=None):
        seen["config"] = settings
        return {"metadata": {"purpose": purpose}}

    monkeypatch.setattr(llm, "load_settings", lambda: loaded.append(1) or SETTINGS)
    monkeypatch.setattr(agent_module, "chat_model", fake_chat_model)
    monkeypatch.setattr(agent_module, "call_config", fake_call_config)
    graph_agent(QUESTION, HINTS, conn, settings=given)
    assert loaded == [] and seen["model"] is given and seen["config"] is given


def test_settings_are_loaded_once_when_a_model_is_given_too(conn, monkeypatch):
    loaded = []
    monkeypatch.setattr(llm, "load_settings", lambda: loaded.append(1) or SETTINGS)
    graph_agent(QUESTION, HINTS, conn, model=terra_model(script=[say()]))
    assert len(loaded) == 1


def test_the_prompt_says_the_independent_lookups_go_in_the_first_round(conn):
    prompt = agent_module.SYSTEM_PROMPT
    assert "similar_breakdowns and alarm_history do not depend on other calls" in prompt
    assert "first round with alarm_causes" in prompt
