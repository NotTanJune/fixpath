"""Tests for the linker, which links incidents to their root causes and counts how
often each alarm has had each cause. The tests use hand-built input and a scripted
decider. They make no network or model calls and never read data/truth."""

import pytest

from app.decide import DecisionError
from app.ingest.link import link
from app.ingest.types import EvidenceDraft, Resolution, ResolvedEdge, ResolvedNode
from app.ledger import BudgetExceeded
from resolve_stubs import ScriptedDecider, choice_answer

HO = "handovers/Handover_20260822_B.txt"
CSV = "maintenance/maint_log_export_2026.csv"
CSV_QUOTE = "2026-08-22 | HP1 | E217 oil under ram | replaced main seals"
SEAL, OIL, BEARING = "FM-SEAL-WEAR", "FM-OIL-CONTAM", "FM-SPINDLE-BEARING"


def ev(doc=CSV, quote="q"):
    return [
        EvidenceDraft(doc_path=doc, quote=quote, extractor="csv-parser", confidence=0.9)
    ]


def rn(node_id, node_type, label=None, **props):
    return ResolvedNode(
        id=node_id,
        type=node_type,
        label=label or node_id,
        props=props,
        evidence=ev(),
        sources=[CSV],
    )


def re_(src, rel, dst, **props):
    return ResolvedEdge(src=src, rel=rel, dst=dst, props=props, evidence=ev())


def incident(number, evidence_quote=CSV_QUOTE, **props):
    inc = f"INC-2026-{number:03d}"
    return ResolvedNode(
        id=inc,
        type="Incident",
        label=props.get("symptom_text", inc),
        props={
            "id": inc,
            "date": "2026-08-22",
            "symptom_text": "oil under the ram",
            **props,
        },
        evidence=ev(CSV, evidence_quote),
        sources=[CSV, HO],
    )


def plant(extra_nodes=(), extra_edges=()) -> Resolution:
    nodes = [
        rn("CNC-01", "Asset"),
        rn("HP-01", "Asset"),
        rn("LT-01", "Asset"),
        rn("CNC-01-SPDL", "Component", asset_id="CNC-01"),
        rn("HP-01-MAINCYL", "Component", asset_id="HP-01"),
        rn("HP-01-PUMP", "Component", asset_id="HP-01"),
        rn(
            SEAL,
            "FailureMode",
            "Main cylinder seal wear",
            name="Main cylinder seal wear",
            mechanism="worn seals",
        ),
        rn(
            OIL,
            "FailureMode",
            "Contaminated oil",
            name="Contaminated oil",
            mechanism="dirty oil",
        ),
        rn(
            BEARING,
            "FailureMode",
            "Spindle bearing wear",
            name="Spindle bearing wear",
            mechanism="worn bearing",
        ),
        rn(
            "PROC-HP-04",
            "Procedure",
            "Replace main cylinder seal kit",
            title="Replace main cylinder seal kit",
        ),
        rn(
            "PROC-HP-05",
            "Procedure",
            "Change hydraulic oil and filter",
            title="Change hydraulic oil and filter",
        ),
        rn(
            "E-217",
            "AlarmCode",
            "Main cylinder pressure loss",
            code="E-217",
            text="Main cylinder pressure loss",
        ),
        rn(
            "E-221",
            "AlarmCode",
            "Hydraulic oil temperature high",
            code="E-221",
            text="Hydraulic oil temperature high",
        ),
        incident(1),
        incident(2),
        incident(3, symptom_text="spindle noise"),
        incident(4, symptom_text="no machine named"),
        incident(5, symptom_text="leak tester drifts"),
        *extra_nodes,
    ]
    edges = [
        re_("HP-01", "HAS_COMPONENT", "HP-01-MAINCYL"),
        re_("HP-01", "HAS_COMPONENT", "HP-01-PUMP"),
        re_("CNC-01", "HAS_COMPONENT", "CNC-01-SPDL"),
        re_(SEAL, "AFFECTS", "HP-01-MAINCYL"),
        re_(OIL, "AFFECTS", "HP-01-PUMP"),
        re_(BEARING, "AFFECTS", "CNC-01-SPDL"),
        re_(SEAL, "RESOLVED_BY", "PROC-HP-04"),
        re_(OIL, "RESOLVED_BY", "PROC-HP-05"),
        re_("E-217", "INDICATES", SEAL, likelihood=0.7, support=0),
        re_("E-217", "INDICATES", OIL, likelihood=0.3, support=0),
        re_("E-221", "INDICATES", OIL, likelihood=0.7, support=0),
        re_("INC-2026-001", "OCCURRED_ON", "HP-01"),
        re_("INC-2026-001", "TRIGGERED_BY", "E-217"),
        re_("INC-2026-002", "OCCURRED_ON", "HP-01"),
        re_("INC-2026-002", "TRIGGERED_BY", "E-217"),
        re_("INC-2026-003", "OCCURRED_ON", "CNC-01"),
        re_("INC-2026-005", "OCCURRED_ON", "LT-01"),
        re_("INC-2026-001", "USED", "SEAL-KIT-50", qty=1),
        *extra_edges,
    ]
    return Resolution(
        nodes=sorted(nodes, key=lambda n: n.id),
        edges=sorted(edges, key=lambda e: (e.src, e.rel, e.dst)),
    )


def only(resolution, ids):
    """The plant with only the given incidents kept."""
    drop = {f"INC-2026-{n:03d}" for n in range(1, 6)} - {
        f"INC-2026-{n:03d}" for n in ids
    }
    return Resolution(
        nodes=[n for n in resolution.nodes if n.id not in drop],
        edges=[e for e in resolution.edges if e.src not in drop],
    )


def pick(choice, p, others=None):
    probabilities = {choice: p, **(others or {})}
    return choice_answer(choice, probabilities)


def root_causes(resolution):
    return sorted((e.src, e.dst) for e in resolution.edges if e.rel == "ROOT_CAUSE")


def run(resolution, answer, ids=(1,)):
    decider = ScriptedDecider(
        answer if callable(answer) else (lambda purpose, state: answer)
    )
    return link(only(resolution, ids), decider), decider


# --- The options --------------------------------------------------------------------


def test_the_options_are_the_failure_modes_of_the_machine_plus_unknown():
    _, decider = run(plant(), pick("unknown", 0.9), ids=(1, 3))
    options = {}
    for purpose, state, questions in decider.calls:
        options[state["incident"]["machine"]] = questions["root_cause"].options
    assert list(options["HP-01"]) == [OIL, SEAL, "unknown"]
    assert list(options["CNC-01"]) == [BEARING, "unknown"]


def test_an_option_is_described_by_its_name_and_the_procedures_that_fix_it():
    _, decider = run(plant(), pick("unknown", 0.9))
    options = decider.calls[0][2]["root_cause"].options
    assert "Main cylinder seal wear" in options[SEAL]
    assert "Replace main cylinder seal kit" in options[SEAL]
    assert "Contaminated oil" in options[OIL]
    assert "Change hydraulic oil and filter" in options[OIL]


def test_the_question_is_asked_with_the_link_purpose():
    _, decider = run(plant(), pick("unknown", 0.9))
    assert decider.purposes() == ["link root cause"]
    assert list(decider.calls[0][2]) == ["root_cause"]


def test_the_state_holds_the_incident_and_its_alarm_with_the_manual_causes():
    _, decider = run(plant(), pick("unknown", 0.9))
    state = decider.calls[0][1]
    assert state["incident"]["machine"] == "HP-01"
    assert state["incident"]["alarm"] == "E-217"
    assert state["incident"]["symptom"] == "oil under the ram"
    assert state["incident"]["parts"] == [{"part": "SEAL-KIT-50", "qty": 1}]
    (alarm,) = state["alarms"]
    assert alarm["code"] == "E-217"
    assert alarm["text"] == "Main cylinder pressure loss"
    assert {c["failure_mode"] for c in alarm["causes"]} == {
        "Main cylinder seal wear",
        "Contaminated oil",
    }


# --- The gates ----------------------------------------------------------------------


def test_at_08_a_root_cause_is_linked_with_linker_evidence():
    resolution, _ = run(plant(), pick(SEAL, 0.8))
    assert root_causes(resolution) == [("INC-2026-001", SEAL)]
    edge = next(e for e in resolution.edges if e.rel == "ROOT_CAUSE")
    (evidence,) = edge.evidence
    assert evidence.extractor == "linker"
    assert evidence.confidence == 0.8
    assert evidence.quote == CSV_QUOTE
    assert evidence.doc_path == CSV
    assert resolution.review == []


def test_at_the_link_gate_exactly_it_links():
    resolution, _ = run(plant(), pick(SEAL, 0.75))
    assert root_causes(resolution) == [("INC-2026-001", SEAL)]


def test_at_06_the_top_two_options_go_to_review():
    answer = pick(SEAL, 0.6, {OIL: 0.3, "unknown": 0.1})
    resolution, _ = run(plant(), answer)
    assert root_causes(resolution) == []
    (item,) = resolution.review
    assert item.kind == "link"
    assert item.confidence == 0.6
    assert item.payload["incident"] == "INC-2026-001"
    assert item.payload["options"] == [
        {"failure_mode": SEAL, "p": 0.6},
        {"failure_mode": OIL, "p": 0.3},
    ]
    assert item.doc_paths == [CSV, HO]


def test_at_03_nothing_is_linked_and_nothing_is_queued():
    resolution, _ = run(plant(), pick(SEAL, 0.3, {OIL: 0.3, "unknown": 0.4}))
    assert root_causes(resolution) == []
    assert resolution.review == []


def test_unknown_links_nothing_even_when_sure():
    resolution, _ = run(plant(), pick("unknown", 0.95))
    assert root_causes(resolution) == []
    assert resolution.review == []


def test_a_refusal_is_no_decision():
    resolution, _ = run(plant(), lambda purpose, state: None)
    assert root_causes(resolution) == []
    (item,) = resolution.review
    assert (item.kind, item.payload["reason"]) == ("link", "no decision")


def test_a_choice_outside_the_options_is_no_decision():
    resolution, _ = run(plant(), pick("FM-MADE-UP", 0.99))
    assert root_causes(resolution) == []
    assert resolution.review[0].payload["reason"] == "no decision"


def test_a_decider_error_is_a_review_item_and_the_run_goes_on():
    resolution, decider = run(
        plant(), lambda purpose, state: DecisionError("x"), ids=(1, 2)
    )
    assert len(decider.calls) == 2
    assert [i.payload["reason"] for i in resolution.review] == [
        "decider error: DecisionError"
    ] * 2


def test_a_spent_budget_stops_the_run():
    with pytest.raises(BudgetExceeded):
        run(plant(), lambda purpose, state: BudgetExceeded("spent"))


def test_an_incident_without_a_machine_goes_to_review_and_is_not_asked():
    resolution, decider = run(plant(), pick(SEAL, 0.9), ids=(4,))
    assert decider.calls == []
    (item,) = resolution.review
    assert item.kind == "link"
    assert item.payload["incident"] == "INC-2026-004"
    assert item.payload["reason"] == "no machine"


def test_a_machine_with_no_failure_modes_goes_to_review():
    resolution, decider = run(plant(), pick(SEAL, 0.9), ids=(5,))
    assert decider.calls == []
    assert resolution.review[0].payload["reason"] == "no failure mode for the machine"


def test_an_incident_that_has_a_root_cause_is_not_asked_again():
    extra = [re_("INC-2026-001", "ROOT_CAUSE", OIL)]
    base = plant(extra_edges=extra)
    resolution, decider = run(base, pick(SEAL, 0.9))
    assert decider.calls == []
    assert root_causes(resolution) == [("INC-2026-001", OIL)]


def test_the_input_resolution_is_not_changed():
    base = only(plant(), (1,))
    before = base.model_dump_json()
    link(base, ScriptedDecider(pick(SEAL, 0.9)))
    assert base.model_dump_json() == before


# --- History ------------------------------------------------------------------------


def support(resolution):
    return {(e.src, e.dst): e.props for e in resolution.edges if e.rel == "INDICATES"}


def test_support_counts_the_incidents_with_that_alarm_and_root_cause():
    resolution, _ = run(plant(), pick(SEAL, 0.9), ids=(1, 2))
    props = support(resolution)
    assert props[("E-217", SEAL)] == {"likelihood": 0.7, "support": 2}
    assert props[("E-217", OIL)] == {"likelihood": 0.3, "support": 0}
    assert props[("E-221", OIL)] == {"likelihood": 0.7, "support": 0}


def test_support_includes_root_causes_that_were_already_there():
    extra = [re_("INC-2026-002", "ROOT_CAUSE", OIL)]
    resolution, _ = run(plant(extra_edges=extra), pick(SEAL, 0.9), ids=(1, 2))
    props = support(resolution)
    assert props[("E-217", SEAL)]["support"] == 1
    assert props[("E-217", OIL)]["support"] == 1


def test_a_history_pair_that_no_manual_lists_is_a_conflict_and_adds_no_edge():
    extra = [re_("INC-2026-001", "ROOT_CAUSE", BEARING)]
    resolution, _ = run(plant(extra_edges=extra), pick(SEAL, 0.9), ids=(1,))
    assert ("E-217", BEARING) not in support(resolution)
    (item,) = [i for i in resolution.review if i.kind == "conflict"]
    assert item.payload == {
        "reason": "history pair not in any manual",
        "alarm": "E-217",
        "failure_mode": BEARING,
        "count": 1,
    }


def test_incidents_without_an_alarm_add_no_support():
    resolution, _ = run(plant(), pick(BEARING, 0.9), ids=(3,))
    assert root_causes(resolution) == [("INC-2026-003", BEARING)]
    assert all(props["support"] == 0 for props in support(resolution).values())


def test_edges_stay_sorted_after_linking():
    resolution, _ = run(plant(), pick(SEAL, 0.9), ids=(1, 2))
    keys = [(e.src, e.rel, e.dst) for e in resolution.edges]
    assert keys == sorted(keys)


def test_the_same_inputs_give_byte_identical_json():
    def once():
        return run(plant(), pick(SEAL, 0.9), ids=(1, 2, 3, 4, 5))[0].model_dump_json()

    assert once() == once()
