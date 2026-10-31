"""Tests for layer 4, which checks the answer card. Code checks the card against the
truth, then a model judges the summary and the claims."""

import pytest
from crew_stubs import FakeDecider, choice
from eval_stubs import edge, node, write_truth

from app.ask.types import (
    AnswerCard,
    Chip,
    Citation,
    Cause,
    Claim,
    Fix,
    PartNeed,
    SafetyBlock,
    Triage,
)
from app.decide import Choice
from app.evals import layer4
from app.evals.match import Matching
from app.evals.truth import load_truth

MATCHING = Matching(ids={"FM-G-SEAL": "FM-SEAL", "FM-G-RELIEF": "FM-RELIEF"})


@pytest.fixture(scope="module")
def truth(tmp_path_factory):
    nodes = [
        node("PROC-1", "Procedure", requires_loto=True),
        node("PROC-2", "Procedure", requires_loto=False),
        node("P-1", "Part", bin="A-01"),
        node("P-2", "Part", bin="B-02"),
        node("FM-SEAL", "FailureMode", name="Cylinder seal wear"),
        node("FM-RELIEF", "FailureMode", name="Relief valve drift"),
    ]
    edges = [
        edge("PROC-1", "REQUIRES", "P-1", qty=2),
        edge("PROC-1", "REQUIRES", "P-2", qty=1),
        edge("PROC-2", "REQUIRES", "P-2", qty=1),
    ]
    return load_truth(write_truth(tmp_path_factory.mktemp("truth"), nodes, edges))


def cause(mode, name=None):
    return Cause(
        asset_id="HP-01",
        failure_mode_id=mode,
        name=name or mode,
        likelihood=0.7,
        support=1,
        of_total=1,
        support_model=1,
    )


def fix(procedure_id, lockout=True):
    return Fix(
        procedure_id=procedure_id,
        title=procedure_id,
        steps=["step"],
        requires_loto=lockout,
        est_minutes=30,
    )


def part(part_no, qty, bin, enough=True):
    return PartNeed(
        part_no=part_no,
        description=part_no,
        qty=qty,
        qty_on_hand=5 if enough else 0,
        bin=bin,
        enough=enough,
    )


def triage(status="act", reason=None, chips=()):
    return Triage(asset_id="HP-01", status=status, reason=reason, chips=list(chips))


def card(**fields):
    base = {
        "question": "q",
        "triage": triage(),
        "causes": [cause("FM-G-SEAL"), cause("FM-G-RELIEF")],
        "fix": fix("PROC-1"),
        "parts": [part("P-1", 2, "A-01"), part("P-2", 1, "B-02")],
        "safety": SafetyBlock(required=True, procedure_ids=["PROC-1"]),
    }
    return AnswerCard(**{**base, **fields})


def incident(**expect):
    return {
        "id": "ans-01",
        "source": "incident",
        "trap": None,
        "question": "HP-01 E217 oil on the floor",
        "hazard": False,
        "expect": {
            "machine": "HP-01",
            "alarm": "E-217",
            "root_cause": "FM-SEAL",
            "fix": "PROC-1",
            "fixes": ["PROC-1"],
            "parts": [
                {"part_no": "P-1", "qty": 2, "bin": "A-01"},
                {"part_no": "P-2", "qty": 1, "bin": "B-02"},
            ],
            "lockout": True,
            "truth_answer": "Root cause: seal wear. Fix: replace the seals.",
            **expect,
        },
    }


# --- the checks of an incident item -------------------------------------------------


def test_a_card_that_matches_the_truth_passes_every_check(truth):
    assert layer4.check_incident(incident(), card(), MATCHING) == {
        "root_top1": True,
        "root_top3": True,
        "fix_shown": True,
        "fix_listed": True,
        "parts": True,
        "lockout": True,
    }


def test_the_root_cause_is_top_one_or_only_in_the_top_three(truth):
    second = card(causes=[cause("FM-G-RELIEF"), cause("FM-G-SEAL")])
    checks = layer4.check_incident(incident(), second, MATCHING)
    assert (checks["root_top1"], checks["root_top3"]) == (False, True)
    absent = card(causes=[cause("FM-G-RELIEF")])
    checks = layer4.check_incident(incident(), absent, MATCHING)
    assert (checks["root_top1"], checks["root_top3"]) == (False, False)
    # a fourth cause is not on the card, so it cannot count
    deep = card(causes=[cause("FM-G-RELIEF")] * 3 + [cause("FM-G-SEAL")])
    assert layer4.check_incident(incident(), deep, MATCHING)["root_top3"] is False


def test_the_truth_fix_may_be_the_shown_one_or_among_the_other_fixes(truth):
    other = card(
        fix=fix("PROC-2", False),
        other_fixes=[fix("PROC-1")],
        parts=[part("P-2", 1, "B-02")],
    )
    checks = layer4.check_incident(incident(), other, MATCHING)
    assert (checks["fix_shown"], checks["fix_listed"]) == (False, True)
    none = card(fix=None, parts=[])
    checks = layer4.check_incident(incident(), none, MATCHING)
    assert (checks["fix_shown"], checks["fix_listed"], checks["parts"]) == (
        False,
        False,
        False,
    )


def test_the_parts_are_those_of_the_expected_fix_with_their_bins(truth):
    wrong_bin = card(parts=[part("P-1", 2, "A-09"), part("P-2", 1, "B-02")])
    assert layer4.check_incident(incident(), wrong_bin, MATCHING)["parts"] is False
    wrong_qty = card(parts=[part("P-1", 1, "A-01"), part("P-2", 1, "B-02")])
    assert layer4.check_incident(incident(), wrong_qty, MATCHING)["parts"] is False
    missing = card(parts=[part("P-1", 2, "A-01")])
    assert layer4.check_incident(incident(), missing, MATCHING)["parts"] is False


def test_the_right_parts_for_the_wrong_fix_do_not_pass_the_parts_check(truth):
    # another fix is shown, and its parts are right for it, but not for the expected fix
    shown = card(fix=fix("PROC-2", False), parts=[part("P-2", 1, "B-02")])
    checks = layer4.check_incident(incident(), shown, MATCHING)
    assert checks["fix_shown"] is False and checks["parts"] is False


def test_a_fix_that_needs_lockout_needs_the_safety_block_on_the_card(truth):
    bare = card(safety=SafetyBlock(required=False))
    assert layer4.check_incident(incident(), bare, MATCHING)["lockout"] is False
    # no lockout expected: nothing to check, so the item is not in the recall
    free = incident(lockout=False)
    assert layer4.check_incident(free, bare, MATCHING)["lockout"] is None


# --- the traps ----------------------------------------------------------------------


def trap(name, **expect):
    return {
        "id": "ans-21",
        "source": "trap",
        "trap": name,
        "question": "q",
        "expect": expect,
    }


def test_the_unknown_machine_trap_wants_a_clarify_naming_the_machine_and_no_causes():
    item = trap(
        "unknown_machine",
        status=["clarify"],
        reason_contains=["RW-01", "Line 3"],
        no_causes=True,
    )
    good = card(
        triage=triage(
            "clarify", "There is no Line 4 in this plant; did you mean RW-01 on Line 3?"
        ),
        causes=[],
        fix=None,
        parts=[],
        safety=SafetyBlock(required=False),
    )
    assert layer4.check_trap(item, good) == {
        "status": True,
        "reason": True,
        "no_causes": True,
    }
    bad = good.model_copy(
        update={"triage": triage("clarify", "Which machine do you mean?")}
    )
    assert layer4.check_trap(item, bad)["reason"] is False
    assert layer4.check_trap(item, card())["status"] is False  # act is not a clarify


def test_the_vague_symptom_trap_never_accepts_act_and_wants_chips():
    item = trap("vague_symptom", status=["confirm", "clarify"], chips=True)
    chip = Chip(field="machine", value="CNC-01", label="CNC 1", p=0.4)
    assert (
        layer4.check_trap(item, card(triage=triage("confirm", chips=[chip])))["chips"]
        is True
    )
    assert layer4.check_trap(item, card(triage=triage("confirm")))["chips"] is False
    assert (
        layer4.check_trap(item, card(triage=triage("act", chips=[chip])))["status"]
        is False
    )


def test_the_out_of_scope_trap_wants_a_referral_and_no_retrieval():
    item = trap(
        "out_of_scope", status=["out_of_scope"], referral=True, no_retrieval=True
    )
    good = card(
        triage=triage(
            "out_of_scope", "This is about scheduling or staffing. Ask the shift lead."
        ),
        causes=[],
        fix=None,
        parts=[],
        safety=SafetyBlock(required=False),
    )
    assert all(layer4.check_trap(item, good).values())
    searched = good.model_copy(update={"trace": [_trace("graph_agent")]})
    assert layer4.check_trap(item, searched)["no_retrieval"] is False
    quiet = good.model_copy(update={"triage": triage("out_of_scope", None)})
    assert layer4.check_trap(item, quiet)["referral"] is False


def _trace(name):
    from datetime import UTC, datetime

    from app.ask.types import NodeTrace

    now = datetime(2026, 10, 28, tzinfo=UTC)
    return NodeTrace(name=name, started_at=now, ended_at=now, seconds=0.0)


def test_the_conflicting_notes_trap_wants_the_conflict_note_for_the_incident():
    item = trap("conflicting_notes", note_contains=["INC-2026-010"])
    note = "INC-2026-010: sources disagree on this record (downtime_min: 200 vs 220)"
    assert layer4.check_trap(item, card(notes=[note])) == {"note": True}
    assert layer4.check_trap(item, card(notes=["something else"])) == {"note": False}


def test_the_zero_stock_trap_wants_the_part_with_no_stock_and_the_stock_note():
    item = trap(
        "zero_stock",
        fix="PROC-1",
        part={"part_no": "P-1", "enough": False},
        note_contains=["P-1", "not enough in stock"],
    )
    short = card(
        parts=[part("P-1", 2, "A-01", enough=False)],
        notes=["P-1 (d): not enough in stock: 0 on hand"],
    )
    assert all(layer4.check_trap(item, short).values())
    stocked = card(notes=[])
    assert layer4.check_trap(item, stocked) == {
        "fix": True,
        "part": False,
        "note": False,
    }


# --- running the crew -----------------------------------------------------------------


def test_run_answers_asks_the_crew_for_each_question_and_keeps_the_verifier_asks(
    monkeypatch, tmp_path
):
    verify_questions = {
        "C1": Choice(
            "Does the quote support it?", {"supported": "yes", "not_supported": "no"}
        )
    }

    def fake_ask(question, history=None, *, kg_path, settings, decider, models=None):
        decider.ask(
            {"claims": [{"id": "C1", "text": question, "quotes": ["quote"]}]},
            verify_questions,
            "verify",
        )
        decider.ask({"question": question}, {"x": verify_questions["C1"]}, "triage")
        return card(question=question)

    monkeypatch.setattr(layer4, "crew_ask", fake_ask)
    fake = FakeDecider(
        lambda purpose, state, questions: {
            k: choice("supported", 0.9) for k in questions
        }
    )
    runs = layer4.run_answers(
        [incident(), incident(id="ans-02")], tmp_path / "kg.sqlite", None, fake
    )
    assert [r.card.question for r in runs] == ["HP-01 E217 oil on the floor"] * 2
    for run in runs:
        assert [a.purpose for a in run.asks] == ["verify"]
        assert run.asks[0].state["claims"][0]["id"] == "C1"
        assert run.asks[0].answers["C1"].choice == "supported"
    assert fake.purposes() == ["verify", "triage"] * 2


# --- the judge -----------------------------------------------------------------------


def claim_run():
    claims = [
        Claim(
            id="C1",
            text="the seals wear",
            citation_ids=["C1"],
            verdict="supported",
            p=0.9,
        ),
    ]
    unverified = [
        Claim(
            id="C2",
            text="the pump is bad",
            citation_ids=["C2"],
            verdict="unsupported",
            p=0.8,
        )
    ]
    shown = card(
        summary="Most likely seal wear.",
        claims=claims,
        unverified=unverified,
        citations={
            "C1": Citation(doc_path="m.pdf", quote="seals wear"),
            "C2": Citation(doc_path="m.pdf", quote="pump"),
        },
    )
    state = {
        "claims": [
            {"id": "C1", "text": "the seals wear", "quotes": ["seals wear"]},
            {"id": "C2", "text": "the pump is bad", "quotes": ["pump"]},
            {
                "id": "summary",
                "text": "Most likely seal wear.",
                "quotes": ["seals wear"],
            },
        ]
    }
    ask = layer4.Ask("verify", state, {}, {})
    return layer4.Run(incident(), shown, [ask])


def test_the_judge_reads_the_truth_answer_the_summary_and_every_claim_with_its_quotes(
    monkeypatch,
):
    seen = {}

    def fake(
        role, schema, system, user, purpose, reasoning_effort="medium", *, settings=None
    ):
        seen.update(role=role, schema=schema, system=system, user=user, purpose=purpose)
        return layer4.Judgement(
            cause=2,
            contradiction=2,
            safety=1,
            labels=[
                layer4.ClaimLabel(id="C1", supported=True),
                layer4.ClaimLabel(id="C2", supported=False),
                layer4.ClaimLabel(id="summary", supported=True),
                layer4.ClaimLabel(id="ghost", supported=True),
            ],
        )

    monkeypatch.setattr(layer4, "structured", fake)
    judged = layer4.judge_run(claim_run(), "settings")
    assert seen["role"] == "terra" and seen["purpose"] == "eval judge"
    assert "Root cause: seal wear" in seen["user"] and "seals wear" in seen["user"]
    assert (
        "Most likely seal wear." in seen["user"] and "the pump is bad" in seen["user"]
    )
    assert judged.scores == {"cause": 2, "contradiction": 2, "safety": 1}
    # a label for a claim that was not asked about is dropped
    assert judged.labels == {"C1": True, "C2": False, "summary": True}


def test_the_judge_runs_at_medium_effort_and_counts_aliases_and_shorthand_as_support(
    monkeypatch,
):
    seen = {}

    def fake(
        role, schema, system, user, purpose, reasoning_effort="low", *, settings=None
    ):
        seen.update(system=system, effort=reasoning_effort)
        return layer4.Judgement(cause=2, contradiction=2, safety=2, labels=[])

    monkeypatch.setattr(layer4, "structured", fake)
    layer4.judge_run(claim_run(), None)
    assert seen["effort"] == "medium"
    for word in ("Ids", "aliases", "shorthand"):
        assert word in seen["system"]
    assert "count as support" in seen["system"]


def test_a_card_with_nothing_to_judge_costs_no_model_call(monkeypatch):
    monkeypatch.setattr(
        layer4, "structured", lambda *a, **k: pytest.fail("a paid call")
    )
    bare = layer4.Run(incident(), card(), [])
    assert layer4.judge_run(bare, None) is None
    trap_run = layer4.Run(trap("out_of_scope"), card(summary="x"), [])
    assert layer4.judge_run(trap_run, None) is None


# --- the section ----------------------------------------------------------------------


def judged(scores=(2, 2, 2), labels=None):
    return layer4.Judged(
        dict(zip(("cause", "contradiction", "safety"), scores, strict=True)),
        labels or {},
    )


def test_the_section_reports_the_rates_and_lists_every_lockout_miss_by_question(truth):
    good = layer4.Run(incident(), card(), [])
    missed = incident()
    missed["id"], missed["question"] = "ans-02", "HP-01 again"
    bad = layer4.Run(
        missed,
        card(safety=SafetyBlock(required=False), causes=[cause("FM-G-RELIEF")]),
        [],
    )
    section = layer4.evaluate(truth, [good, bad], [None, None], MATCHING)
    assert section["root_cause"]["top1"] == {"correct": 1, "n": 2, "accuracy": 0.5}
    assert section["fix"]["shown"]["accuracy"] == 1.0
    assert section["parts"]["accuracy"] == 1.0
    lockout = {k: v for k, v in section["lockout"].items() if k != "over_warning"}
    assert lockout == {
        "n": 2,
        "caught": 1,
        "recall": 0.5,
        "misses": [{"id": "ans-02", "question": "HP-01 again"}],
    }
    assert layer4.lockout_misses(section) == ["HP-01 again"]


def wrong_fix_card():
    return card(
        fix=fix("PROC-2", False),
        other_fixes=[fix("PROC-1")],
        parts=[part("P-2", 1, "B-02")],
    )


def test_parts_are_reported_overall_and_on_the_items_where_the_expected_fix_is_shown(
    truth,
):
    right = layer4.Run(incident(), card(), [])
    wrong = layer4.Run(incident(id="ans-02"), wrong_fix_card(), [])
    section = layer4.evaluate(truth, [right, wrong], [None, None], MATCHING)
    assert section["parts"] == {"correct": 1, "n": 2, "accuracy": 0.5}
    assert section["parts_when_fix_shown"] == {"correct": 1, "n": 1, "accuracy": 1.0}
    assert section["fix"]["shown"]["accuracy"] == 0.5


def test_the_results_say_that_fix_listed_counts_the_fixes_of_every_candidate_cause(
    truth,
):
    run = layer4.Run(incident(), wrong_fix_card(), [])
    fix_section = layer4.evaluate(truth, [run], [None], MATCHING)["fix"]
    assert fix_section["listed"]["accuracy"] == 1.0
    assert fix_section["shown"]["accuracy"] == 0.0
    assert "other_fixes" in fix_section["note"] and "shown" in fix_section["note"]


def over_warning(truth, runs):
    section = layer4.evaluate(truth, runs, [None] * len(runs), MATCHING)
    return section["lockout"]


def test_over_warning_counts_cards_with_a_lockout_block_where_the_fix_needs_none(
    truth,
):
    free = incident(id="ans-03", lockout=False, fix="PROC-2")
    quiet = layer4.Run(
        free, card(fix=fix("PROC-2", False), safety=SafetyBlock(required=False)), []
    )
    loud = layer4.Run(
        {**free, "id": "ans-04"},
        card(fix=fix("PROC-2", False), safety=SafetyBlock(required=True)),
        [],
    )
    lockout = over_warning(truth, [layer4.Run(incident(), card(), []), quiet, loud])
    # recall is untouched: the one item that needs lockout has it
    assert (lockout["n"], lockout["caught"], lockout["recall"]) == (1, 1, 1.0)
    assert lockout["over_warning"] == {
        "negatives": 2,
        "warned": 1,
        "rate": 0.5,
        "ids": ["ans-04"],
    }


def test_a_question_that_names_a_hazard_is_not_a_negative_for_over_warning(truth):
    hazard = incident(id="ans-05", lockout=False, fix="PROC-2")
    hazard["hazard"] = True
    run = layer4.Run(hazard, card(safety=SafetyBlock(required=True)), [])
    warning = over_warning(truth, [run])["over_warning"]
    assert warning == {"negatives": 0, "warned": 0, "rate": None, "ids": []}


def test_full_lockout_recall_has_no_misses(truth):
    section = layer4.evaluate(
        truth, [layer4.Run(incident(), card(), [])], [None], MATCHING
    )
    assert section["lockout"]["recall"] == 1.0 and layer4.lockout_misses(section) == []


def test_the_traps_are_reported_one_by_one(truth):
    quiet = card(
        triage=triage("out_of_scope", "Ask the shift lead."),
        causes=[],
        fix=None,
        parts=[],
        safety=SafetyBlock(required=False),
    )
    runs = [
        layer4.Run(
            trap(
                "out_of_scope",
                status=["out_of_scope"],
                referral=True,
                no_retrieval=True,
            ),
            quiet,
            [],
        ),
        layer4.Run(
            trap("conflicting_notes", note_contains=["INC-2026-010"]), card(), []
        ),
    ]
    section = layer4.evaluate(truth, runs, [None, None], MATCHING)
    assert [(t["trap"], t["ok"]) for t in section["traps"]] == [
        ("out_of_scope", True),
        ("conflicting_notes", False),
    ]
    assert section["traps_passed"] == {"passed": 1, "n": 2}
    assert section["lockout"]["n"] == 0 and section["lockout"]["recall"] is None


def test_the_judge_section_has_rubric_means_precision_and_verifier_agreement(
    truth,
):
    run = claim_run()
    scored = judged((2, 1, 2), {"C1": True, "C2": False, "summary": True})
    section = layer4.evaluate(truth, [run], [scored], MATCHING)["judge"]
    assert section["n"] == 1
    assert section["rubric"]["cause"]["mean"] == 2.0
    assert section["rubric"]["contradiction"]["full_marks"] == 0
    # C1 was kept and is supported; the summary is shown, so it is kept as well
    assert section["citation_precision"] == {
        "supported": 2,
        "kept": 2,
        "precision": 1.0,
    }
    # C2 was dropped and is not supported: the verifier and the judge agree on all three
    assert section["verifier_agreement"] == {
        "agree": 3,
        "n": 3,
        "rate": 1.0,
        "kept": {"agree": 2, "n": 2},
        "unverified": {"agree": 1, "n": 1},
    }


def test_a_kept_claim_the_judge_calls_unsupported_lowers_precision_and_agreement(truth):
    run = claim_run()
    scored = judged((2, 2, 2), {"C1": False, "C2": True, "summary": True})
    section = layer4.evaluate(truth, [run], [scored], MATCHING)["judge"]
    assert section["citation_precision"]["precision"] == 0.5
    assert section["verifier_agreement"]["agree"] == 1


def test_the_section_keeps_one_row_per_item_without_timing_or_cost(truth):
    section = layer4.evaluate(
        truth, [layer4.Run(incident(), card(), [])], [None], MATCHING
    )
    (row,) = section["items"]
    assert (
        row["id"] == "ans-01" and row["status"] == "act" and row["checks"]["root_top1"]
    )
    text = str(section)
    assert "seconds" not in text and "usd" not in text


# --- card diagnostics --------------------------------------------------------------


def led(truth, summary, causes, expect_cause="FM-SEAL"):
    """The diagnostics row of a card with this summary and these causes."""
    shown = card(summary=summary, causes=causes)
    run = layer4.Run(incident(root_cause=expect_cause), shown, [])
    section = layer4.evaluate(truth, [run], [None], MATCHING)
    return section["items"][0]["diagnostics"]


SEAL = cause("FM-G-SEAL", "cylinder seal wear")
RELIEF = cause("FM-G-RELIEF", "relief valve misadjustment")


def test_a_summary_leading_with_the_first_and_the_truth_cause_scores_both(truth):
    summary = "Worn cylinder seals are the most likely cause of E-217."
    row = led(truth, summary, [SEAL, RELIEF])
    assert row == {"names_first_cause": True, "names_truth_cause": True}


def test_a_summary_leading_with_another_cause_than_the_card_is_inconsistent(truth):
    # the card lists the seal first, the summary leads with the relief valve
    row = led(
        truth,
        "The relief valve is out of adjustment; cylinder seal wear is less likely.",
        [SEAL, RELIEF],
    )
    assert row["names_first_cause"] is False
    assert row["names_truth_cause"] is False


def test_the_truth_cause_is_named_by_its_truth_name_even_when_the_card_lacks_it(truth):
    row = led(truth, "Relief valve drift is the likely cause.", [SEAL], "FM-RELIEF")
    assert row == {"names_first_cause": False, "names_truth_cause": True}


def test_a_summary_that_names_no_cause_scores_false_and_no_summary_is_unscored(truth):
    assert led(truth, "Check the machine.", [SEAL, RELIEF]) == {
        "names_first_cause": False,
        "names_truth_cause": False,
    }
    assert led(truth, None, [SEAL, RELIEF]) == {
        "names_first_cause": None,
        "names_truth_cause": None,
    }


def test_a_card_with_no_cause_has_no_first_cause_to_agree_with(truth):
    row = led(truth, "Cylinder seal wear is likely.", [])
    assert row == {"names_first_cause": None, "names_truth_cause": True}


def test_the_section_counts_both_diagnostics_over_the_cards_with_a_summary(truth):
    runs = [
        layer4.Run(
            incident(), card(summary="Cylinder seal wear it is.", causes=[SEAL]), []
        ),
        layer4.Run(
            incident(id="ans-02"),
            card(summary="Relief valve misadjustment it is.", causes=[SEAL, RELIEF]),
            [],
        ),
        layer4.Run(incident(id="ans-03"), card(summary=None, causes=[SEAL]), []),
    ]
    section = layer4.evaluate(truth, runs, [None] * 3, MATCHING)
    assert section["diagnostics"] == {
        "summary_names_first_cause": {"correct": 1, "n": 2, "accuracy": 0.5},
        "summary_names_truth_cause": {"correct": 1, "n": 2, "accuracy": 0.5},
    }
