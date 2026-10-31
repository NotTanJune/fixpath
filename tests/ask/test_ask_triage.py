"""Tests for triage, the first step of answering a question. It reads the question and
works out which machine, alarm and kind of request it is about. A scripted decider
stands in for the real one. The tests cover the single decider call, the confidence
rules that choose between acting, confirming and asking, and the ids found in the text."""

import pytest
from crew_stubs import (
    FakeDecider,
    build_crew_kg,
    choice,
    noul,
    open_crew_kg,
    refusal,
    triage_answers,
)
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app import store
from app.ask import tools
from app.ask.triage import GATES, found_ids, triage
from app.decide import Choice, DecisionError, Noul
from app.ledger import BudgetExceeded

QUESTION = "HP-01 stopped with E-217, oil under the press"


@pytest.fixture
def conn(tmp_path):
    connection = open_crew_kg(tmp_path)
    yield connection
    connection.close()


def decider(**answers):
    return FakeDecider({"triage": triage_answers(**answers)})


def run(conn, fake, question=QUESTION, history=None):
    return triage(question, history, conn, fake)


# --- the one ask -----------------------------------------------------------------


def test_the_five_questions_go_in_one_ask_with_the_purpose_triage(conn):
    fake = decider()
    run(conn, fake)
    ((purpose, state, questions),) = fake.calls
    assert purpose == "triage"
    assert list(questions) == [
        "machine",
        "alarm",
        "root_cause",
        "intent",
        "safety_critical",
    ]
    machine, alarm, cause, intent, safety = questions.values()
    assert all(isinstance(q, Choice) for q in (machine, alarm, cause, intent))
    assert isinstance(safety, Noul)
    assert "isolating" in safety.instructions and "hazard" in safety.instructions


def test_the_machine_options_are_the_assets_described_plus_unknown(conn):
    fake = decider()
    run(conn, fake)
    options = fake.calls[0][2]["machine"].options
    assert list(options) == ["CNC-01", "CNC-02", "HP-01", "LT-01", "RW-01", "unknown"]
    assert "Hydraulic press" in options["HP-01"] and "the press" in options["HP-01"]
    assert "machine" in options["HP-01"]  # its type
    assert "Lathe 1" in options["CNC-01"]


def test_the_alarm_options_are_the_codes_with_their_text_plus_none(conn):
    fake = decider()
    run(conn, fake)
    options = fake.calls[0][2]["alarm"].options
    assert list(options) == ["E-214", "E-217", "E-221", "none"]
    assert "Pressure low" in options["E-217"]


def test_the_root_cause_options_name_the_machine_they_affect(conn):
    fake = decider()
    run(conn, fake)
    options = fake.calls[0][2]["root_cause"].options
    assert list(options)[-1] == "unknown"
    assert "main cylinder seal wear" in options["FM-SEAL-WEAR"]
    assert "Hydraulic press" in options["FM-SEAL-WEAR"]
    # a failure mode of the shared lathe manual affects both lathes
    assert (
        "Lathe" in options["FM-SPINDLE-WEAR"]
        and "Second lathe" in options["FM-SPINDLE-WEAR"]
    )


def test_the_intent_options(conn):
    fake = decider()
    run(conn, fake)
    options = fake.calls[0][2]["intent"].options
    assert list(options) == ["troubleshoot", "find_part", "history", "out_of_scope"]


# --- the state --------------------------------------------------------------------


def test_the_state_holds_the_question_the_last_four_turns_and_the_ids_found(conn):
    fake = decider()
    history = [f"turn {i}" for i in range(6)]
    run(conn, fake, history=history)
    state = fake.calls[0][1]
    assert state["question"] == QUESTION
    assert state["history"] == ["turn 2", "turn 3", "turn 4", "turn 5"]
    assert state["found"] == {"machines": ["HP-01"], "alarms": ["E-217"]}


def test_the_state_with_no_history_has_an_empty_list(conn):
    fake = decider()
    run(conn, fake, history=None)
    assert fake.calls[0][1]["history"] == []


def test_code_finds_machines_through_aliases_and_alarms_in_any_spelling(conn):
    assert found_ids("Lathe 1 shows E217", conn) == {
        "machines": ["CNC-01"],
        "alarms": ["E-217"],
    }
    assert found_ids("press 2 says ERR 221 again", conn) == {
        "machines": ["HP-01"],
        "alarms": ["E-221"],
    }
    assert found_ids("the press is leaking", conn) == {
        "machines": ["HP-01"],
        "alarms": [],
    }
    assert found_ids("something is wrong", conn) == {"machines": [], "alarms": []}
    # a code that no alarm node holds is not an id of the graph
    assert found_ids("alarm E-999", conn)["alarms"] == []
    # a model number names every machine of the model
    assert found_ids("KX-500 is down", conn)["machines"] == ["CNC-01", "CNC-02"]


# --- the gates --------------------------------------------------------------------


def test_the_gates_are_in_one_place():
    assert GATES == {"act": 0.75, "confirm": 0.4, "chip": 0.15}


def test_a_sure_answer_acts_and_gives_the_hints(conn):
    result = run(conn, decider())
    assert result.status == "act"
    assert (result.asset_id, result.alarm_code, result.failure_mode_id) == (
        "HP-01",
        "E-217",
        "FM-SEAL-WEAR",
    )
    assert (
        result.intent == "troubleshoot" and result.chips == [] and result.reason is None
    )
    assert result.safety_critical == pytest.approx(0.1)
    assert result.confidence == {
        "machine": 0.97,
        "alarm": 0.95,
        "root_cause": 0.9,
        "intent": 0.95,
        "safety_critical": pytest.approx(0.9),
    }


def test_the_edges_of_the_gates(conn):
    just_act = run(conn, decider(machine=choice("HP-01", 0.75)))
    assert just_act.status == "act" and just_act.asset_id == "HP-01"
    just_confirm = run(conn, decider(machine=choice("HP-01", 0.7499)))
    assert just_confirm.status == "confirm" and just_confirm.asset_id == "HP-01"
    low_confirm = run(conn, decider(machine=choice("HP-01", 0.4)))
    assert low_confirm.status == "confirm" and low_confirm.asset_id == "HP-01"
    clarify = run(conn, decider(machine=choice("HP-01", 0.3999)))
    assert clarify.status == "clarify" and clarify.asset_id is None


def test_a_machine_in_the_confirm_band_keeps_the_guess_and_chips_from_015(conn):
    machine = choice("HP-01", 0.6, {"CNC-01": 0.2, "CNC-02": 0.14, "LT-01": 0.06})
    result = run(conn, decider(machine=machine))
    assert result.status == "confirm" and result.asset_id == "HP-01"
    assert [(c.field, c.value, c.p) for c in result.chips] == [
        ("machine", "HP-01", 0.6),
        ("machine", "CNC-01", 0.2),
    ]
    assert result.chips[0].label == "Hydraulic press (HP-01)"


def test_a_machine_below_the_clarify_gate_gets_the_top_three_options_as_chips(conn):
    machine = choice(
        "unknown",
        0.3,
        {"CNC-01": 0.25, "HP-01": 0.2, "CNC-02": 0.1, "LT-01": 0.1, "RW-01": 0.05},
    )
    result = run(conn, decider(machine=machine))
    assert result.status == "clarify" and result.asset_id is None
    assert [c.value for c in result.chips] == ["CNC-01", "HP-01", "CNC-02"]
    assert all(c.field == "machine" for c in result.chips)
    assert result.reason and "machine" in result.reason.lower()


def test_unknown_machine_at_act_is_none_and_asks_which_machine(conn):
    machine = choice(
        "unknown", 0.9, {"RW-01": 0.06, "CNC-01": 0.02, "HP-01": 0.01, "LT-01": 0.01}
    )
    result = run(conn, decider(machine=machine))
    assert result.status == "clarify" and result.asset_id is None
    assert [c.value for c in result.chips] == ["RW-01", "CNC-01", "HP-01"]


def test_none_alarm_at_act_is_none_and_still_acts(conn):
    result = run(conn, decider(alarm=choice("none", 0.9)))
    assert result.alarm_code is None and result.status == "act" and result.chips == []


def test_unknown_root_cause_at_act_is_none(conn):
    result = run(conn, decider(root_cause=choice("unknown", 0.9)))
    assert result.failure_mode_id is None and result.status == "act"


def test_an_alarm_in_the_confirm_band_gets_chips_and_the_crew_still_runs(conn):
    alarm = choice("E-217", 0.55, {"E-221": 0.3, "E-214": 0.1, "none": 0.05})
    result = run(conn, decider(alarm=alarm))
    assert result.status == "confirm" and result.alarm_code == "E-217"
    assert [(c.value, c.p) for c in result.chips] == [("E-217", 0.55), ("E-221", 0.3)]
    assert result.chips[1].label == "E-221 Oil hot"


def test_an_alarm_below_the_clarify_gate_gets_clarify_chips_but_the_machine_stays(conn):
    alarm = choice("none", 0.3, {"E-217": 0.28, "E-221": 0.22, "E-214": 0.2})
    result = run(conn, decider(alarm=alarm))
    assert result.status == "confirm" and result.asset_id == "HP-01"
    assert result.alarm_code is None
    assert [c.value for c in result.chips if c.field == "alarm"] == [
        "E-217",
        "E-221",
        "E-214",
    ]


def test_a_root_cause_in_the_confirm_band_keeps_the_guess_with_chips(conn):
    cause = choice(
        "FM-SEAL-WEAR", 0.71, {"FM-RELIEF-MISADJUSTED": 0.2, "FM-PUMP-WEAR": 0.05}
    )
    result = run(conn, decider(root_cause=cause))
    assert result.status == "confirm" and result.failure_mode_id == "FM-SEAL-WEAR"
    assert [c.value for c in result.chips] == ["FM-SEAL-WEAR", "FM-RELIEF-MISADJUSTED"]
    assert result.chips[0].label == "main cylinder seal wear"


def test_the_safety_flag_is_the_yes_probability_and_gates_like_the_others(conn):
    flagged = run(conn, decider(safety=noul(0.9)))
    assert flagged.safety_critical == pytest.approx(0.9) and flagged.status == "act"
    unsure = run(conn, decider(safety=noul(0.6)))
    assert unsure.status == "confirm" and unsure.safety_critical == pytest.approx(0.6)
    assert [(c.field, c.value) for c in unsure.chips] == [
        ("safety_critical", "yes"),
        ("safety_critical", "no"),
    ]
    confident_no = run(conn, decider(safety=noul(0.02)))
    assert confident_no.status == "act"


def test_an_intent_in_the_confirm_band_never_runs_as_out_of_scope(conn):
    intent = choice("out_of_scope", 0.5, {"troubleshoot": 0.4, "history": 0.1})
    result = run(conn, decider(intent=intent))
    assert result.status == "confirm" and result.intent == "troubleshoot"
    assert [c.value for c in result.chips] == ["out_of_scope", "troubleshoot"]


# --- out of scope -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "who"),
    [
        ("Who is on the night shift roster next week?", "shift lead"),
        ("Can you swap my shift with Ravi's?", "shift lead"),
        ("What is the PPE policy for the press bay?", "EHS"),
        ("Order ten more filters for the stores", "stores"),
        ("Tell me a joke", "shift lead"),
    ],
)
def test_out_of_scope_at_act_stops_and_names_who_to_ask(conn, question, who):
    fake = decider(intent=choice("out_of_scope", 0.9))
    result = run(conn, fake, question=question)
    assert result.status == "out_of_scope" and result.intent == "out_of_scope"
    assert who in result.reason


def test_out_of_scope_wins_over_an_unknown_machine(conn):
    fake = decider(machine=choice("unknown", 0.9), intent=choice("out_of_scope", 0.8))
    assert (
        run(conn, fake, question="who covers the night shift").status == "out_of_scope"
    )


# --- no decision -------------------------------------------------------------------


def test_a_refused_machine_falls_back_to_the_machine_code_found_and_confirms(conn):
    result = run(conn, decider(machine=refusal(), alarm=refusal()))
    assert result.status == "confirm"
    assert (result.asset_id, result.alarm_code) == ("HP-01", "E-217")
    assert result.confidence["machine"] is None and result.confidence["alarm"] is None


def test_a_refused_machine_with_nothing_found_clarifies(conn):
    answers = dict(machine=refusal(), alarm=choice("none", 0.9))
    result = run(conn, decider(**answers), question="it stopped and there is oil")
    assert result.status == "clarify" and result.asset_id is None


def test_two_machines_found_in_the_text_are_not_a_fallback(conn):
    result = run(
        conn, decider(machine=refusal()), question="Lathe 1 and the press both stopped"
    )
    assert result.status == "clarify" and result.asset_id is None


def test_a_decision_error_on_the_whole_ask_uses_the_ids_code_found(conn):
    fake = FakeDecider(
        lambda purpose, state, questions: DecisionError("backend failed")
    )
    result = run(conn, fake)
    assert result.status == "confirm"
    assert (result.asset_id, result.alarm_code, result.failure_mode_id) == (
        "HP-01",
        "E-217",
        None,
    )
    assert result.intent == "troubleshoot"
    assert set(result.confidence.values()) == {None}


def test_a_decision_error_with_nothing_found_clarifies(conn):
    fake = FakeDecider(
        lambda purpose, state, questions: DecisionError("backend failed")
    )
    result = run(conn, fake, question="strange noise")
    assert result.status == "clarify" and "could not read the question" in result.reason


def test_a_budget_stop_is_not_swallowed(conn):
    fake = FakeDecider(lambda purpose, state, questions: BudgetExceeded("spent"))
    with pytest.raises(BudgetExceeded):
        run(conn, fake)


def test_an_answer_that_is_missing_or_unusable_is_no_decision(conn):
    answers = triage_answers()
    del answers["alarm"]
    answers["machine"] = choice("HP-01", float("nan"))
    result = run(conn, FakeDecider({"triage": answers}))
    assert result.confidence["machine"] is None and result.confidence["alarm"] is None
    assert result.asset_id == "HP-01" and result.alarm_code == "E-217"  # from the text
    assert result.status == "confirm"


# --- a line the plant does not have --------------------------------------------------

LINE_4 = "Line 4 robot is down"


def test_an_unknown_line_clarifies_with_the_top_machines_and_a_reason(conn):
    machine = choice(
        "RW-01", 0.86, {"HP-01": 0.06, "CNC-01": 0.03, "CNC-02": 0.02, "LT-01": 0.01}
    )
    result = run(conn, decider(machine=machine), question=LINE_4)
    assert result.status == "clarify" and result.asset_id is None
    assert [(c.field, c.value, c.p) for c in result.chips] == [
        ("machine", "RW-01", 0.86),
        ("machine", "HP-01", 0.06),
        ("machine", "CNC-01", 0.03),
    ]
    assert (
        result.reason
        == "There is no Line 4 in this plant; did you mean RW-01 on Line 1?"
    )


def test_an_unknown_line_with_an_exact_asset_id_confirms(conn):
    result = run(
        conn,
        decider(machine=choice("HP-01", 0.95, {"CNC-01": 0.02})),
        question="HP-01 on Line 4 leaking",
    )
    assert result.status == "confirm" and result.asset_id == "HP-01"
    assert (
        result.reason
        == "There is no Line 4 in this plant; did you mean HP-01 on Line 1?"
    )
    assert [c.value for c in result.chips if c.field == "machine"][0] == "HP-01"


def test_an_unknown_line_with_an_exact_id_and_a_decider_error_confirms_too(conn):
    fake = FakeDecider(lambda purpose, state, questions: DecisionError("down"))
    result = run(conn, fake, question="HP-01 on Line 4 leaking")
    assert result.status == "confirm" and result.asset_id == "HP-01"
    assert "no Line 4" in result.reason


def test_a_position_in_a_program_is_not_a_line_of_the_plant(conn):
    question = "alarm on line 120 of the program on CNC-01"
    result = run(conn, decider(machine=choice("CNC-01", 0.95)), question=question)
    assert (
        result.status == "act" and result.asset_id == "CNC-01" and result.reason is None
    )
    result = run(
        conn,
        decider(machine=choice("CNC-01", 0.95)),
        question="CNC-01 stops on line 12 of the log with E-217",
    )
    assert result.status == "act"


def test_a_line_the_plant_has_is_fine(conn):
    result = run(
        conn, decider(machine=choice("RW-01", 0.9)), question="Line 1 robot is down"
    )
    assert result.status == "act" and result.asset_id == "RW-01"


def test_only_the_current_question_is_checked_never_the_history(conn):
    result = run(
        conn,
        decider(machine=choice("RW-01", 0.9)),
        question="RW-01 is down again",
        history=[LINE_4, "which machine do you mean?"],
    )
    assert result.status == "act" and result.asset_id == "RW-01"


def test_an_unknown_line_without_probabilities_offers_the_machines_code_found(conn):
    result = run(conn, decider(machine=refusal()), question="Line 4 KX-500 is down")
    assert result.status == "clarify"
    assert [c.value for c in result.chips] == ["CNC-01", "CNC-02"]
    assert result.reason.startswith("There is no Line 4 in this plant")


def test_an_unknown_line_does_not_override_out_of_scope(conn):
    fake = decider(intent=choice("out_of_scope", 0.95))
    result = run(conn, fake, question="Who covers the night shift on Line 4?")
    assert result.status == "out_of_scope"


# --- a clarify with no chips ---------------------------------------------------------


def test_a_clarify_with_no_chips_offers_the_machines_code_found(conn):
    result = run(conn, decider(machine=refusal()), question="KX-500 is down")
    assert result.status == "clarify"
    assert [(c.field, c.value, c.p) for c in result.chips] == [
        ("machine", "CNC-01", None),
        ("machine", "CNC-02", None),
    ]
    assert result.chips[0].label == "Lathe (CNC-01)"


def test_the_reason_never_shows_an_exception_class(conn):
    fake = FakeDecider(
        lambda purpose, state, questions: DecisionError("backend failed")
    )
    result = run(conn, fake, question="KX-500 is down")
    assert result.status == "clarify"
    assert result.reason == "KX-500 is CNC-01 and CNC-02. Which one?"
    assert (
        "DecisionError" not in result.reason and "decider" not in result.reason.lower()
    )


# --- the decision against the one id in the text ----------------------------------------


def test_the_machine_id_the_technician_typed_wins_over_a_different_pick(conn):
    machine = choice("CNC-01", 0.9, {"CNC-02": 0.05})
    result = run(conn, decider(machine=machine), question="CNC-02 stopped")
    assert result.status == "confirm" and result.asset_id == "CNC-02"
    assert [(c.field, c.value, c.p) for c in result.chips if c.field == "machine"] == [
        ("machine", "CNC-02", 0.05),
        ("machine", "CNC-01", 0.9),
    ]
    assert result.reason == (
        "The question names CNC-02, but I read CNC-01. Which is right?"
    )


def test_the_typed_id_wins_when_an_unknown_line_is_named_too(conn):
    machine = choice("RW-01", 0.97, {"HP-01": 0.01})
    result = run(conn, decider(machine=machine), question="HP-01 on Line 4 leaking")
    assert result.status == "confirm" and result.asset_id == "HP-01"
    assert [c.value for c in result.chips if c.field == "machine"] == ["HP-01", "RW-01"]
    assert result.reason == (
        "There is no Line 4 in this plant; did you mean HP-01 on Line 1? "
        "The question names HP-01, but I read RW-01."
    )


def test_an_alarm_that_differs_from_the_one_code_in_the_text_confirms_with_both(conn):
    alarm = choice("E-221", 0.9, {"E-217": 0.05})
    result = run(conn, decider(alarm=alarm), question="HP-01 stopped with E-217")
    assert result.status == "confirm" and result.alarm_code == "E-221"
    assert [c.value for c in result.chips if c.field == "alarm"] == ["E-221", "E-217"]
    assert "E-217" in result.reason


def test_a_decision_that_matches_the_text_still_acts(conn):
    assert run(conn, decider()).status == "act"
    # two ids in the text are no single id to compare with
    result = run(
        conn,
        decider(machine=choice("CNC-01", 0.9)),
        question="CNC-01 and CNC-02 stopped",
    )
    assert result.status == "act"


# --- a part question with no machine ---------------------------------------------


def test_a_part_question_with_no_machine_offers_the_machines_the_part_fits(conn):
    machine = choice("unknown", 0.9, {"CNC-01": 0.05})
    answers = dict(
        machine=machine, alarm=choice("none", 0.9), intent=choice("find_part", 0.95)
    )
    result = run(conn, decider(**answers), question="Is SEAL-KIT-50 in stock?")
    assert result.status == "clarify" and result.asset_id is None
    assert [(c.field, c.value) for c in result.chips] == [("machine", "HP-01")]
    assert result.reason == (
        "Stock for SEAL-KIT-50 is below. Pick a machine to see the fix it is used in."
    )


def test_a_part_that_fits_nothing_leaves_the_plain_clarify(conn):
    answers = dict(machine=choice("unknown", 0.9), alarm=choice("none", 0.9))
    result = run(conn, decider(**answers), question="Is OIL-HLP46-20L in stock?")
    assert result.status == "clarify" and result.reason == "Which machine do you mean?"


# --- who to ask ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "who"),
    [
        ("holiday leave roster for stores", "shift lead"),
        ("Can I take leave if stores are closed", "shift lead"),
        ("in order to fix the press, who signs off", "shift lead"),
        ("Which supplier do I use to reorder filters", "stores"),
        ("the storeroom schedule", "shift lead"),
        ("what is the compliance audit date", "EHS"),
    ],
)
def test_the_referral_matches_whole_words_and_checks_the_shift_lead_before_stores(
    conn, question, who
):
    result = run(conn, decider(intent=choice("out_of_scope", 0.9)), question=question)
    assert result.status == "out_of_scope" and who in result.reason


# --- "no alarm" in the text means no alarm ------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "tool not clamping after change, machine stops on CNC1 w/ no alarm",
        "Lathe 1 stops and there is no alarm code on the screen",
        "Press 2 stopped, without an alarm",
        "CNC-01 keeps stopping, no codes showing",
        "Lathe 1 stops. No Alarms at all",
    ],
)
def test_a_question_that_says_there_is_no_alarm_has_no_alarm(conn, question):
    alarm = choice("E-217", 0.54, {"E-221": 0.3, "none": 0.1})
    fake = decider(machine=choice("CNC-01", 0.97), alarm=alarm)
    result = run(conn, fake, question=question)
    assert result.alarm_code is None
    assert [c for c in result.chips if c.field == "alarm"] == []
    assert result.confidence["alarm"] == 1.0  # the text ruled the alarm out
    assert result.status == "act" and result.reason is None


def test_a_ruled_out_alarm_does_not_hide_other_doubts(conn):
    machine = choice("CNC-01", 0.6, {"CNC-02": 0.3})
    result = run(conn, decider(machine=machine), question="Lathe 1 stops w/ no alarm")
    assert result.status == "confirm" and result.alarm_code is None
    assert result.reason == "Not sure about: machine."


@pytest.mark.parametrize(
    "question",
    [
        "the no-alarm light is on and the press is leaking",
        "HP-01 stopped with an alarm code on the panel",
        "HP-01 stopped, nothing else",
    ],
)
def test_other_wording_keeps_the_alarm_the_decider_picked(conn, question):
    result = run(conn, decider(), question=question)
    assert result.alarm_code == "E-217"
    assert result.confidence["alarm"] == pytest.approx(0.95)


def test_a_code_written_in_the_text_is_not_ruled_out_by_the_words_no_alarm(conn):
    question = "HP-01 showed E-217 earlier, no alarm now"
    result = run(conn, decider(), question=question)
    assert result.alarm_code == "E-217"
    assert result.confidence["alarm"] == pytest.approx(0.95)


# --- an alarm with no machine offers the machines that raise it -----------------------

OTHERS = {"LT-01": 0.02, "CNC-01": 0.02, "CNC-02": 0.01}
UNKNOWN_MACHINE = choice("unknown", 0.94, OTHERS)


def machines(result):
    return [c.value for c in result.chips if c.field == "machine"]


@pytest.fixture
def kg_path(tmp_path):
    return build_crew_kg(tmp_path / "kg.sqlite")


def open_with_raisers(kg_path, *pairs):
    """Make more machines raise E-217, as (machine, count) pairs, and open the graph."""
    rw = store.connect(kg_path)
    for asset, count in pairs:
        store.upsert_edge(rw, asset, "RAISED", "E-217", {"count": count})
    rw.close()
    return tools.connect_ro(kg_path)


def test_an_alarm_raised_by_one_machine_names_that_machine(conn):
    result = run(conn, decider(machine=UNKNOWN_MACHINE), question="E217 again")
    assert result.status == "confirm" and result.asset_id == "HP-01"
    assert result.reason == "E-217 is only raised by HP-01."
    assert machines(result) == ["HP-01"]
    assert result.confidence["machine"] is None  # the graph chose it, not the decider


def test_an_alarm_raised_by_two_machines_offers_those_two_by_raise_count(kg_path):
    ro = open_with_raisers(kg_path, ("CNC-02", 40))
    try:
        result = run(ro, decider(machine=UNKNOWN_MACHINE), question="E217 again")
    finally:
        ro.close()
    assert result.status == "clarify" and result.asset_id is None
    assert machines(result) == ["CNC-02", "HP-01"]
    assert result.reason == "E-217 is raised by CNC-02 and HP-01. Which one?"


def test_at_most_three_raisers_are_offered(kg_path):
    ro = open_with_raisers(kg_path, ("CNC-01", 1), ("CNC-02", 2), ("LT-01", 3))
    try:
        result = run(ro, decider(machine=UNKNOWN_MACHINE), question="E217 again")
    finally:
        ro.close()
    assert result.status == "clarify"
    assert machines(result) == ["HP-01", "LT-01", "CNC-02"]
    assert result.reason == "E-217 is raised by HP-01, LT-01 and CNC-02. Which one?"


def test_the_alarm_found_in_the_text_counts_when_the_decider_doubts_it(conn):
    alarm = choice("none", 0.3, {"E-217": 0.28, "E-221": 0.22, "E-214": 0.2})
    fake = decider(machine=UNKNOWN_MACHINE, alarm=alarm)
    result = run(conn, fake, question="E217 again")
    assert result.status == "confirm" and result.asset_id == "HP-01"


def test_an_alarm_nobody_raised_leaves_the_decider_chips(conn):
    machine = choice("unknown", 0.9, {"RW-01": 0.06, "CNC-01": 0.02, "HP-01": 0.01})
    fake = decider(machine=machine, alarm=choice("E-221", 0.9))
    result = run(conn, fake, question="oil hot again")
    assert result.status == "clarify"
    assert machines(result) == ["RW-01", "CNC-01", "HP-01"]


def test_no_alarm_known_leaves_the_machine_chips_alone(conn):
    machine = choice("unknown", 0.9, {"RW-01": 0.06, "CNC-01": 0.02, "HP-01": 0.01})
    fake = decider(machine=machine, alarm=choice("none", 0.9))
    result = run(conn, fake, question="it stopped")
    assert result.status == "clarify"
    assert machines(result) == ["RW-01", "CNC-01", "HP-01"]


def test_a_machine_the_text_names_keeps_the_old_clarify_chips(conn):
    machine = choice(
        "unknown", 0.9, {"RW-01": 0.06, "CNC-01": 0.02, "HP-01": 0.01, "LT-01": 0.01}
    )
    result = run(conn, decider(machine=machine))  # the text names HP-01 and E-217
    assert result.status == "clarify"
    assert [c.value for c in result.chips] == ["RW-01", "CNC-01", "HP-01"]


# --- a model name that covers two machines is never acted on -------------------------

SHARED = "KX-500 is CNC-01 and CNC-02. Which one?"


def test_a_model_name_for_two_machines_clarifies_even_when_the_pick_is_one_of_them(
    conn,
):
    # with nothing else to go on, a pick between two machines of one model is a guess
    machine = choice("CNC-01", 0.9, {"CNC-02": 0.08})
    question = "the KX-500 on the left stopped"
    result = run(conn, decider(machine=machine), question=question)
    assert result.status == "clarify" and result.asset_id is None
    assert machines(result) == ["CNC-01", "CNC-02"]
    assert result.reason == SHARED


def test_an_earlier_turn_that_names_one_of_them_confirms_it(conn):
    fake = decider(machine=choice("CNC-01", 0.9))
    history = ["technician: CNC-02 again", "assistant: Which alarm does it show?"]
    result = run(conn, fake, question="the KX-500 stopped", history=history)
    assert result.status == "confirm" and result.asset_id == "CNC-02"
    assert machines(result) == ["CNC-01", "CNC-02"]


def test_an_earlier_turn_that_names_both_still_clarifies(conn):
    fake = decider(machine=choice("CNC-01", 0.9))
    history = ["technician: CNC-01 and CNC-02 are both slow today"]
    result = run(conn, fake, question="the KX-500 stopped", history=history)
    assert result.status == "clarify" and result.asset_id is None


def test_a_model_name_for_two_machines_clarifies_when_the_pick_is_another(conn):
    fake = decider(machine=choice("HP-01", 0.9))
    result = run(conn, fake, question="the KX-500 is making noise")
    assert result.status == "clarify" and result.asset_id is None
    assert machines(result) == ["CNC-01", "CNC-02"]
    assert result.reason == SHARED


def test_a_model_name_with_no_pick_clarifies_with_both(conn):
    fake = decider(machine=choice("unknown", 0.9))
    result = run(conn, fake, question="KX-500 is down")
    assert result.status == "clarify"
    assert machines(result) == ["CNC-01", "CNC-02"]
    assert result.reason == SHARED


def test_a_vendor_name_covering_two_machines_is_treated_the_same(conn):
    fake = decider(machine=choice("CNC-02", 0.95))
    result = run(conn, fake, question="Kessler Werkzeugmaschinen stopped")
    assert result.status == "clarify" and result.asset_id is None
    assert result.reason == "Kessler Werkzeugmaschinen is CNC-01 and CNC-02. Which one?"


def test_an_exact_asset_id_beside_the_model_name_settles_it(conn):
    fake = decider(machine=choice("CNC-01", 0.9))
    result = run(conn, fake, question="CNC-01 the KX-500 stopped")
    assert result.status == "act" and result.asset_id == "CNC-01"


def test_a_name_of_one_machine_beside_the_model_name_settles_it(conn):
    fake = decider(machine=choice("CNC-01", 0.9))
    result = run(conn, fake, question="Lathe 1 the KX-500 stopped")
    assert result.status == "act" and result.asset_id == "CNC-01"


def test_a_model_with_one_machine_is_not_ambiguous(conn):
    fake = decider(machine=choice("HP-01", 0.9))
    result = run(conn, fake, question="the BH-250 stopped")
    assert result.status == "act" and result.asset_id == "HP-01"


def test_the_alarm_disagreement_reason_follows_the_model_reason(conn):
    alarm = choice("E-221", 0.9, {"E-217": 0.05})
    fake = decider(machine=choice("CNC-01", 0.9), alarm=alarm)
    result = run(conn, fake, question="the KX-500 shows E-217")
    assert result.status == "clarify" and result.asset_id is None
    assert result.reason == (
        f"{SHARED} The question names E-217, but I read E-221. Which is right?"
    )
