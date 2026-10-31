"""Tests of the synthesizer, which asks the model for a written answer and a list of
claims. The model is a scripted stub, and the facts and passages come from the tools
over the crew test graph.
"""

import pytest
from crew_stubs import hp_facts, hp_passages, open_crew_kg, sol_model
from llm_stubs import SOL, isolated, rows  # noqa: F401  (an autouse fixture)

from app import llm
from app.ask import synthesize as module
from app.ask.synthesize import Synthesis, Synthesized, number_citations, synthesize
from app.ask.types import Citation, GraphFacts, ManualPassages, Triage
from app.config import Settings

QUESTION = "HP-01 stopped with E-217, oil under the press"
TRIAGE = Triage(asset_id="HP-01", alarm_code="E-217", status="act")


@pytest.fixture
def conn(tmp_path):
    connection = open_crew_kg(tmp_path)
    yield connection
    connection.close()


@pytest.fixture
def facts(conn):
    return hp_facts(conn)


@pytest.fixture
def passages(conn):
    return hp_passages(conn)


def prompt(model) -> str:
    return "\n".join(str(m.content) for m in model.seen[0])


# --- the numbering ---


def test_every_citation_of_the_facts_and_passages_gets_an_id_in_order(facts, passages):
    cited = number_citations(facts, passages)
    assert list(cited) == [f"C{i}" for i in range(1, len(cited) + 1)]
    # causes come first, so C1 is the first cause's first citation
    first = facts.causes[0].citations[0]
    assert (cited["C1"].doc_path, cited["C1"].chunk_id) == (
        first.doc_path,
        first.chunk_id,
    )
    chunks = [c.chunk_id for c in cited.values() if c.chunk_id]
    assert len(chunks) == len(set(chunks))  # each chunk is numbered once


def test_a_chunk_that_facts_and_passages_both_hold_is_numbered_once_with_its_full_text(
    facts, passages
):
    cited = number_citations(facts, passages)
    (hp3,) = [(i, c) for i, c in cited.items() if c.chunk_id == "HP-3"]
    assert (
        hp3[1].quote
        == "Alarm E-217 main cylinder pressure low. Check the seals for wear."
    )
    # the fact's own citation of that chunk came first, so it keeps its number
    first = facts.causes[0].citations[0]
    assert first.chunk_id == "HP-3" and cited["C1"].chunk_id == "HP-3"


def test_a_passage_that_no_fact_cites_is_numbered_after_the_facts(facts, passages):
    only = ManualPassages(
        passages=[passages.passages[0].model_copy(update={"chunk_id": "HP-9"})]
    )
    cited = number_citations(GraphFacts(), only)
    assert list(cited) == ["C1"] and cited["C1"].chunk_id == "HP-9"
    assert cited["C1"].page == only.passages[0].page


# --- the call to the model ---


def test_sol_gets_the_question_the_triage_the_facts_and_the_numbered_passages(
    facts, passages
):
    model = sol_model(
        "The seals are the likely cause.", [("Seal wear fits the leak.", ["C1"])]
    )
    synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    text = prompt(model)
    cited = number_citations(facts, passages)
    assert QUESTION in text and "HP-01" in text and "E-217" in text
    assert (
        "main cylinder seal wear" in text and "Replace main cylinder seal kit" in text
    )
    for cid, citation in cited.items():
        if citation.chunk_id == "HP-3":
            assert f"[{cid}]" in text
    assert "Check the seals for wear." in text
    assert "R. Tan" in text  # a past breakdown


def test_sol_is_never_given_the_steps_or_the_parts(facts, passages):
    model = sol_model("Summary.", [("Claim.", ["C1"])])
    synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    text = prompt(model)
    assert "Stop the pump." not in text and "Do the work." not in text
    assert "OIL-HLP46-20L" not in text and "WIPER-CYL-50" not in text


def test_the_default_model_is_sol_at_low_effort_with_the_purpose_synthesize(
    facts, passages, monkeypatch
):
    seen = {}

    def fake(role, schema, system, user, purpose, effort="medium", **kwargs):
        seen.update(role=role, schema=schema, purpose=purpose, effort=effort)
        return Synthesis(summary="S.", claims=[{"text": "C.", "citations": ["C1"]}])

    monkeypatch.setattr(module, "structured", fake)
    result = synthesize(QUESTION, TRIAGE, facts, passages)
    assert seen == {
        "role": "sol",
        "schema": Synthesis,
        "purpose": "synthesize",
        "effort": "low",
    }
    assert result.summary == "S." and [c.id for c in result.claims] == ["claim-1"]


def test_the_settings_of_the_caller_go_to_the_default_call_and_nothing_is_loaded(
    facts, passages, monkeypatch
):
    seen, given = {}, Settings()

    def fake(role, schema, system, user, purpose, effort="medium", **kwargs):
        seen.update(kwargs)
        return Synthesis(summary="S.", claims=[{"text": "C.", "citations": ["C1"]}])

    monkeypatch.setattr(module, "structured", fake)
    monkeypatch.setattr(llm, "load_settings", lambda: pytest.fail("loaded its own"))
    synthesize(QUESTION, TRIAGE, facts, passages, settings=given)
    assert seen == {"settings": given}


def test_the_settings_of_the_caller_go_to_the_call_config_of_a_given_model(
    facts, passages, monkeypatch
):
    seen, given = [], Settings()
    real = module.call_config

    def spy(purpose, settings=None):
        seen.append(settings)
        return real(purpose, given)

    monkeypatch.setattr(module, "call_config", spy)
    model = sol_model("S.", [("C.", ["C1"])])
    synthesize(QUESTION, TRIAGE, facts, passages, model=model, settings=given)
    assert seen == [given]


def test_the_call_is_booked_in_the_ledger_under_synthesize(facts, passages):
    model = sol_model("S.", [("C.", ["C1"])])
    synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    (row,) = rows()
    assert (row["model"], row["purpose"]) == (SOL, "synthesize")


def test_with_nothing_to_cite_sol_is_not_asked(conn):
    model = sol_model("S.", [("C.", ["C1"])])
    result = synthesize(QUESTION, TRIAGE, GraphFacts(), ManualPassages(), model=model)
    assert model.seen == [] and rows() == []
    assert result.summary == "" and result.claims == []
    assert any("no source" in note for note in result.notes)


# --- the claims ---


def test_claims_carry_their_citation_ids_and_numbered_ids(facts, passages):
    model = sol_model(
        "Seal wear is the likely cause.",
        [
            ("Seals wear first on this press.", ["C1", "C2"]),
            ("Check for oil at the ram.", ["c3"]),
        ],
    )
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert result.summary == "Seal wear is the likely cause."
    assert [(c.id, c.text, c.citation_ids) for c in result.claims] == [
        ("claim-1", "Seals wear first on this press.", ["C1", "C2"]),
        ("claim-2", "Check for oil at the ram.", ["C3"]),
    ]
    assert all(c.verdict is None and c.p is None for c in result.claims)
    assert set(result.citations) == set(number_citations(facts, passages))


def test_a_claim_that_cites_an_id_that_does_not_exist_is_dropped_with_a_note(
    facts, passages
):
    model = sol_model(
        "S.",
        [
            ("Good.", ["C1"]),
            ("Invented source.", ["C99"]),
            ("Half real.", ["C1", "C98"]),
            ("No source at all.", []),
        ],
    )
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert [c.text for c in result.claims] == ["Good."]
    joined = " ".join(result.dropped)
    assert "C99" in joined and "C98" in joined and "Invented source." in joined
    assert "No source at all." in joined
    assert (
        result.notes == []
    )  # the text of a dropped claim goes to the trace, not the card


def test_a_claim_that_restates_a_step_or_a_part_is_dropped(facts, passages):
    model = sol_model(
        "S.",
        [
            ("Order SEAL-KIT-50 first.", ["C1"]),
            ("Stop the pump. Do the work.", ["C1"]),
            ("Seal wear is the best fit.", ["C1"]),
        ],
    )
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert [c.text for c in result.claims] == ["Seal wear is the best fit."]
    assert any("restates" in note for note in result.dropped)
    assert result.notes == []


HISTORY_NOTE = "restates the graph's history count, which the card shows"

# claims the checker must drop: four from a rehearsal run (with the apostrophes as the
# model wrote them) and six past-history claims that the judge called unsupported
HISTORY_CLAIMS = [
    "CNC-01\u2019s recorded past incidents with this alarm were attributed to coolant filter blockage, supporting checking the filter first.",
    "Fixture seal wear is the leading VT-018 cause and accounted for all recorded LT-01 incidents with this alarm.",
    "Past RW-01 incidents with this alarm were attributed to light curtain misalignment.",
    "HP-01\u2019s past incidents involving this alarm all had main cylinder seal wear as the cause.",
    "CNC-02\u2019s past incidents with this alarm predominantly involved spindle bearing wear.",
    "Pressure sensor drift caused every recorded past incident with this alarm on LT-01.",
    "All recorded past incidents associated with this alarm on this machine were attributed to fixture seal wear.",
    "Fixture seal wear was the cause in the recorded alarm-linked incident on this machine, supporting checking the seals first.",
    "The supplied machine history attributes all past incidents with this alarm to gripper finger wear.",
    "All recorded past incidents with this alarm on RW-01 were attributed to gripper finger wear.",
]

# claims about what past repairs did, what was seen, or what the manual says: all stay
KEPT_CLAIMS = [
    "Past incidents on CNC-01 with the same symptom were resolved by addressing the tool clamp disc springs.",
    "Previous HP-01 breakdowns with mid-stroke pressure loss and oil on the floor were resolved by replacing the main cylinder seals.",
    "A previous HP-01 breakdown recorded the same dark, milky oil and noisy pump symptoms.",
    "Previous incidents included a flickering part-present signal and gripper slipping, consistent with checking finger wear first.",
    "The manual lists worn fixture seals as the most likely cause of VT-018, with pressure sensor drift as a possible alternative.",
    "RB-551 means the gripper part-present signal was lost; the manual lists worn gripper fingers as the most likely cause.",
    "Previous breakdown records describe the same dropped-part symptom at the deburr station and identify gripper fingers as the issue resolved.",
]


@pytest.mark.parametrize("text", HISTORY_CLAIMS)
def test_a_claim_that_states_the_cause_of_past_incidents_is_dropped(
    facts, passages, text
):
    model = sol_model("S.", [(text, ["C1"]), ("Seal wear is the best fit.", ["C1"])])
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert [c.text for c in result.claims] == ["Seal wear is the best fit."]
    (note,) = [n for n in result.dropped if text in n]
    assert note == f'dropped a claim that {HISTORY_NOTE}: "{text}"'
    assert result.notes == []


@pytest.mark.parametrize("text", KEPT_CLAIMS)
def test_a_claim_about_past_repairs_or_the_manual_stays(facts, passages, text):
    model = sol_model("S.", [(text, ["C1"])])
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert [c.text for c in result.claims] == [text]
    assert result.dropped == []


def test_a_summary_sentence_that_states_the_cause_of_past_incidents_is_dropped(
    facts, passages
):
    model = sol_model(
        "Seal wear is the likely cause. Past incidents with this alarm were attributed to seal wear.",
        [("Seal wear is the best fit.", ["C1"])],
    )
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert result.summary == "Seal wear is the likely cause."
    (note,) = [n for n in result.dropped if "summary" in n]
    assert HISTORY_NOTE in note


def test_claims_never_carry_steps_or_parts_as_data(facts, passages):
    model = sol_model("S.", [("Seal wear fits.", ["C1"])])
    (claim,) = synthesize(QUESTION, TRIAGE, facts, passages, model=model).claims
    assert set(type(claim).model_fields) == {
        "id",
        "text",
        "citation_ids",
        "verdict",
        "p",
        "support",
        "reason",
    }


def test_at_most_six_claims_and_three_sentences_are_kept(facts, passages):
    model = sol_model(
        "One. Two! Three? Four. Five.",
        [(f"Claim {i}.", ["C1"]) for i in range(8)],
    )
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert result.summary == "One. Two! Three?"
    assert len(result.claims) == 6
    assert any("6" in note for note in result.dropped)


def test_the_result_unpacks_to_the_summary_and_the_claims_first(facts, passages):
    model = sol_model("S.", [("C.", ["C1"])])
    summary, claims, *rest = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert summary == "S." and len(claims) == 1 and len(rest) == 3


def test_citations_is_a_dict_of_citation_objects(facts, passages):
    model = sol_model("S.", [("C.", ["C1"])])
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert all(isinstance(c, Citation) for c in result.citations.values())


# --- citations given as one string ---


def test_citations_given_as_one_string_are_split_on_commas(facts, passages):
    model = sol_model(
        "S.",
        [("Both.", ["C1, C2"]), ("Spaced.", [" c3 ,C1 ", "[C2]"]), ("Stray.", [",,"])],
    )
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert [(c.text, c.citation_ids) for c in result.claims] == [
        ("Both.", ["C1", "C2"]),
        ("Spaced.", ["C3", "C1", "C2"]),
    ]
    assert any("Stray." in note for note in result.dropped)


# --- the summary is checked like the claims ---


def test_a_summary_sentence_that_restates_a_part_or_a_step_is_dropped(facts, passages):
    model = sol_model(
        "Replace SEAL-KIT-50 now. Seal wear is the likely cause. Stop the pump. Do the work.",
        [("Seal wear is the best fit.", ["C1"])],
    )
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert result.summary == "Seal wear is the likely cause."
    dropped = [note for note in result.dropped if "summary" in note]
    assert len(dropped) == 3
    assert (
        "restates part SEAL-KIT-50" in dropped[0]
        and '"Replace SEAL-KIT-50 now."' in dropped[0]
    )
    assert all("restates a step of PROC-HP-04" in note for note in dropped[1:])
    assert result.notes == []


def test_a_summary_that_restates_everything_ends_up_empty(facts, passages):
    model = sol_model("Order SEAL-KIT-50.", [("Seal wear is the best fit.", ["C1"])])
    result = synthesize(QUESTION, TRIAGE, facts, passages, model=model)
    assert result.summary == ""


def test_the_dropped_default_is_not_a_list_that_results_share():
    first, second = Synthesized("", [], {}, []), Synthesized("", [], {}, [])
    assert not isinstance(first.dropped, list) and first.dropped == second.dropped == ()
