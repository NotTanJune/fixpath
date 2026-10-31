"""Tests for layer 2 from start to finish, on small hand-built worlds with a scripted
decider."""

import pytest
from eval_stubs import (
    edge,
    incident_truth,
    make_graph,
    make_truth,
    mentions,
    node,
    record,
)
from resolve_stubs import ALIASES, ScriptedDecider
from resolve_stubs import EXPORT as FILE
from resolve_stubs import edge as draft_edge
from resolve_stubs import extraction, parts_fixture

from app.decide import NoDecisionBackend
from app.evals.layer2 import (
    evaluate,
    root_cause_report,
    same_entity_report,
    same_incident_report,
)
from app.evals.match import match
from app.ingest import questions

EXPORT = "maintenance/export.csv"


def parts_world():
    truth = make_truth(
        [
            node("SEAL-KIT-50", "Part", aliases=["SK-50"]),
            node("SEAL-RV-25", "Part", aliases=["RV SEAL"]),
            node("SEAL-FIX-30", "Part", aliases=[]),
            node("OIL-HLP46-20L", "Part", aliases=["HLP46"]),
        ],
        [],
        doc_mentions={EXPORT: mentions(parts=["SEAL-KIT-50", "OIL-HLP46-20L"])},
    )
    graph = make_graph(
        [
            node(
                "SEAL-KIT-50",
                "Part",
                aliases=["SK-50"],
                description="Main cylinder seal kit",
            ),
            node("SEAL-RV-25", "Part", aliases=[], description="Relief valve seal kit"),
            node("SEAL-FIX-30", "Part", aliases=[], description="Fixture seal"),
            node(
                "OIL-HLP46-20L",
                "Part",
                aliases=["HLP46"],
                description="Hydraulic oil HLP 46",
            ),
        ],
        [],
        node_evidence={
            "SEAL-KIT-50": [(EXPORT, "stock SEAL-KIT-50"), (EXPORT, "second")],
            "SEAL-RV-25": [(EXPORT, "stock SEAL-RV-25")],
            "SEAL-FIX-30": [(EXPORT, "stock SEAL-FIX-30")],
            "OIL-HLP46-20L": [(EXPORT, "stock OIL")],
        },
    )
    merges = [
        merge("Part", "SK-50", "SEAL-KIT-50", 0.96, "merge"),
        merge("Part", "HLP46", "OIL-HLP46-20L", 0.89, "queue"),
        merge("Part", "SEALKIT50", "SEAL-KIT-50", 1.0, "rule", backend=None),
        merge("Part", "TAPEPTFE12", None, None, "unresolved", backend=None),
        merge("Part", "MYSTERY", "SEAL-RV-25", 0.8, "queue"),
    ]
    extractions = [
        extraction(
            EXPORT,
            "maintenance_note",
            [],
            [
                draft_edge(
                    ("Incident", "WO-1"),
                    "USED",
                    ("Part", "SK-50"),
                    doc=EXPORT,
                    quote="SK-50 x1",
                ),
                draft_edge(
                    ("Incident", "WO-1"),
                    "USED",
                    ("Part", "HLP46"),
                    doc=EXPORT,
                    quote="HLP46 x1",
                ),
                draft_edge(
                    ("Incident", "WO-1"),
                    "USED",
                    ("Part", "MYSTERY"),
                    doc=EXPORT,
                    quote="odd",
                ),
            ],
        )
    ]
    return truth, graph, merges, extractions


def merge(type, mention, candidate, p, decision, backend="jev", doc=EXPORT):
    return {
        "type": type,
        "doc_path": doc,
        "mention": mention,
        "candidate": candidate,
        "p": p,
        "decision": decision,
        "backend": backend,
    }


def test_logged_part_pairs_are_scored_and_unasked_siblings_are_asked_like_the_resolver_asks():
    truth, graph, merges, extractions = parts_world()
    decider = ScriptedDecider(0.2)
    report = same_entity_report(truth, graph, merges, extractions, ALIASES, decider)

    # the resolver has no canonical part to ask about here, so only the same-family
    # siblings of the true SK-50 part are asked: SEAL-RV-25 and SEAL-FIX-30
    assert decider.purposes() == ["eval merge Part"] * 2
    asked = {
        (s["mention"]["text"], s["candidate"]["id"])
        for s in decider.states("eval merge Part")
    }
    assert asked == {("SK-50", "SEAL-RV-25"), ("SK-50", "SEAL-FIX-30")}
    state = next(
        s
        for s in decider.states("eval merge Part")
        if s["candidate"]["id"] == "SEAL-RV-25"
    )
    other = graph.nodes["SEAL-RV-25"]
    assert state == questions.entity_state(
        type="Part",
        text="SK-50",
        quote="SK-50 x1",
        doc_path=EXPORT,
        candidate_id=other.id,
        label=other.label,
        description=other.props["description"],
        aliases=other.props["aliases"],
        quotes=[e.quote for e in graph.node_evidence[other.id]],
    )

    assert report["pairs"] == {"labeled": 4, "true": 2, "false": 2, "unlabeled": 1}
    assert report["sources"] == {"logged": 2, "resolver_asked": 0, "siblings": 2}
    assert report["no_candidate"] == 1
    assert report["rules"] == {
        "Part": {"n": 1, "correct": 1, "wrong": 0, "unlabeled": 0}
    }
    assert report["unlabeled"] == [
        {
            "type": "Part",
            "doc_path": EXPORT,
            "mention": "MYSTERY",
            "candidate": "SEAL-RV-25",
            "p": 0.8,
        }
    ]
    at_gate = report["at_gate"]
    # the logged 0.89 pair is a match now that the entity gate is 0.7
    assert (at_gate["gate"], at_gate["tp"], at_gate["fp"], at_gate["fn"]) == (
        0.7,
        2,
        0,
        0,
    )
    assert at_gate["precision"] == 1.0 and at_gate["recall"] == 1.0
    assert [(n["kind"], n["candidate"]) for n in report["negatives"]] == [
        ("sibling", "SEAL-FIX-30"),
        ("sibling", "SEAL-RV-25"),
    ]
    assert all(
        n["p"] == 0.2 and n["true_candidate"] == "SEAL-KIT-50"
        for n in report["negatives"]
    )
    assert report["max_false_p"] == 0.2 and report["min_true_p"] == 0.89


def resolver_world():
    """The inventory and manual parts, an export that mentions one part by a loose
    name, and a part in the graph that the resolver does not know."""
    truth = make_truth(
        [
            node("SEAL-KIT-50", "Part", aliases=[]),
            node("SEAL-RV-25", "Part", aliases=[]),
            node("SEAL-FIX-30", "Part", aliases=[]),
            node("OIL-HLP46-20L", "Part", aliases=[]),
        ],
        [],
        doc_mentions={FILE: mentions(parts=["SEAL-KIT-50"])},
    )
    graph = make_graph(
        [
            node(
                "SEAL-KIT-50",
                "Part",
                aliases=["seal kit main cyl"],
                description="Main cylinder seal kit",
            ),
            node("SEAL-RV-25", "Part", aliases=[], description="Relief valve seal kit"),
            node("SEAL-FIX-30", "Part", aliases=[], description="Fixture seal"),
            node(
                "OIL-HLP46-20L", "Part", aliases=[], description="Hydraulic oil HLP 46"
            ),
        ],
        [],
        node_evidence={
            "SEAL-KIT-50": [(FILE, "stock SEAL-KIT-50")],
            "SEAL-RV-25": [(FILE, "stock SEAL-RV-25")],
            "SEAL-FIX-30": [(FILE, "stock SEAL-FIX-30")],
            "OIL-HLP46-20L": [(FILE, "stock OIL")],
        },
    )
    merges = [
        merge("Part", "seal kit main cyl", "SEAL-KIT-50", 0.95, "merge", doc=FILE)
    ]
    extractions = [
        *parts_fixture(),
        extraction(
            FILE,
            "maintenance_note",
            [],
            [
                draft_edge(
                    ("Incident", "WO-1"),
                    "USED",
                    ("Part", "seal kit main cyl"),
                    doc=FILE,
                    quote="seal kit main cyl x1",
                )
            ],
        ),
    ]
    return truth, graph, merges, extractions


def scripted_by_candidate(**p_by_candidate):
    return ScriptedDecider(
        lambda purpose, state: p_by_candidate[state["candidate"]["id"]]
    )


def test_the_runner_ups_the_resolver_asked_are_the_negatives_and_only_unasked_siblings_are_paid():
    truth, graph, merges, extractions = resolver_world()
    replay = scripted_by_candidate(
        **{
            "SEAL-KIT-50": 0.95,
            "SEAL-RV-25": 0.3,
            "OIL-HLP46-20L": 0.1,
            "FLT-HYD-10": 0.1,
            "BRG-6205-2RS": 0.1,
        }
    )
    live = ScriptedDecider(0.2)
    report = same_entity_report(
        truth, graph, merges, extractions, ALIASES, live, replay_decider=replay
    )
    # the resolver asked about both seal kits for the mention
    assert {s["candidate"]["id"] for s in replay.states("merge Part")} >= {
        "SEAL-KIT-50",
        "SEAL-RV-25",
    }
    # only the sibling that it did not ask costs a question of the eval's own
    assert live.purposes() == ["eval merge Part"]
    assert live.states("eval merge Part")[0]["candidate"]["id"] == "SEAL-FIX-30"
    assert (
        report["sources"]["resolver_asked"] == 1 and report["sources"]["siblings"] == 1
    )
    by_candidate = {n["candidate"]: n for n in report["negatives"]}
    assert (
        by_candidate["SEAL-RV-25"]["kind"] == "resolver"
        and by_candidate["SEAL-RV-25"]["p"] == 0.3
    )
    assert (
        by_candidate["SEAL-FIX-30"]["kind"] == "sibling"
        and by_candidate["SEAL-FIX-30"]["p"] == 0.2
    )
    assert set(by_candidate) == {"SEAL-RV-25", "SEAL-FIX-30"}
    # the logged true pair is scored once, with the probability it was logged with
    assert report["pairs"] == {"labeled": 3, "true": 1, "false": 2, "unlabeled": 0}
    assert report["max_false_p"] == 0.3 and report["min_true_p"] == 0.95


def test_the_negative_set_does_not_depend_on_the_order_of_the_run_or_on_a_seed():
    truth, graph, merges, extractions = resolver_world()

    def run():
        replay = scripted_by_candidate(
            **{
                "SEAL-KIT-50": 0.95,
                "SEAL-RV-25": 0.3,
                "OIL-HLP46-20L": 0.1,
                "FLT-HYD-10": 0.1,
                "BRG-6205-2RS": 0.1,
            }
        )
        return same_entity_report(
            truth,
            graph,
            merges,
            extractions,
            ALIASES,
            ScriptedDecider(0.2),
            replay_decider=replay,
        )["negatives"]

    assert run() == run()


def test_an_unasked_sibling_is_not_chosen_when_the_truth_says_the_mention_names_it():
    truth, graph, merges, extractions = resolver_world()
    truth.nodes["SEAL-FIX-30"].props["aliases"].append("seal kit main cyl")
    decider = ScriptedDecider(0.2)
    same_entity_report(
        truth,
        graph,
        merges,
        extractions,
        ALIASES,
        decider,
        replay_decider=scripted_by_candidate(
            **{
                "SEAL-KIT-50": 0.95,
                "SEAL-RV-25": 0.3,
                "OIL-HLP46-20L": 0.1,
                "FLT-HYD-10": 0.1,
                "BRG-6205-2RS": 0.1,
            }
        ),
    )
    assert decider.purposes() == []


def test_a_question_the_replay_decider_cannot_answer_stops_the_report():
    truth, graph, merges, extractions = resolver_world()
    failing = ScriptedDecider(
        lambda purpose, state: NoDecisionBackend("not in the cache")
    )
    with pytest.raises(NoDecisionBackend):
        same_entity_report(
            truth,
            graph,
            merges,
            extractions,
            ALIASES,
            ScriptedDecider(0.2),
            replay_decider=failing,
        )


def test_a_negative_that_the_decider_accepts_is_a_false_positive():
    truth, graph, merges, extractions = parts_world()
    decider = ScriptedDecider(
        lambda purpose, state: 0.95 if state["candidate"]["id"] == "SEAL-RV-25" else 0.1
    )
    report = same_entity_report(truth, graph, merges, extractions, ALIASES, decider)
    assert report["at_gate"]["fp"] == 1
    assert report["at_gate"]["precision"] == pytest.approx(2 / 3)
    assert report["recommendation"]["gate"] is None


def test_no_decision_on_a_negative_is_not_a_merge():
    truth, graph, merges, extractions = parts_world()
    decider = ScriptedDecider(lambda purpose, state: None)
    report = same_entity_report(truth, graph, merges, extractions, ALIASES, decider)
    assert report["at_gate"]["fp"] == 0
    assert report["bands"]["undecided"]["false"] == 2


def incident_merges(extra=()):
    note = "handovers/one.txt"
    first = f"{note}#1"

    def pair(other, p, decision):
        return {
            "type": "Incident",
            "doc_path": note,
            "mention": first,
            "candidate": f"{EXPORT}::{other}",
            "p": p,
            "decision": decision,
            "backend": "jev",
        }

    return [
        pair("WO-3", 0.95, "merge"),
        pair("WO-1", 0.3, "new"),
        pair("WO-2", 0.7, "queue"),
        pair("WO-9", 0.6, "queue"),
        {**pair("WO-3", 1.0, "rule"), "backend": None, "candidate": first},
        *extra,
    ]


def incident_records():
    note = "handovers/one.txt"
    rows = {
        "WO-1": record(
            EXPORT, "WO-1", date="2026-04-14", machine="HP-01", technician="JHC"
        ),
        "WO-2": record(
            EXPORT, "WO-2", date="2026-04-14", machine="HP-01", technician="NA"
        ),
        "WO-3": record(EXPORT, "WO-3", date="2026-04-15", machine="HP-01"),
        "WO-9": record(EXPORT, "WO-9", date="2026-04-14", machine="HP-01"),
    }
    found = {(EXPORT, key): row for key, row in rows.items()}
    found[note, f"{note}#1"] = record(note, f"{note}#1")
    return found


def test_incident_pairs_are_labeled_by_the_truth_incident_of_both_records():
    report = same_incident_report(
        incident_truth(), incident_merges(), incident_records()
    )
    assert report["pairs"] == {"labeled": 3, "true": 1, "false": 2, "unlabeled": 1}
    assert report["rules"] == {"decisions": 1}
    at_gate = report["at_gate"]
    assert (at_gate["gate"], at_gate["tp"], at_gate["fp"], at_gate["fn"]) == (
        0.9,
        1,
        0,
        0,
    )
    assert report["bands"]["merge"] == {"true": 1, "false": 0}
    assert report["bands"]["queue"] == {"true": 0, "false": 1}
    assert report["bands"]["none"] == {"true": 0, "false": 1}
    assert report["unlabeled"] == [
        {
            "doc_path": "handovers/one.txt",
            "mention": "handovers/one.txt#1",
            "candidate": f"{EXPORT}::WO-9",
            "p": 0.6,
        }
    ]
    # the decision-level view agrees here: nothing was refused
    decision_level = report["decision_level"]
    assert decision_level["refused"] == []
    assert decision_level["at_gate"]["tp"] == 1 and decision_level["at_gate"]["fp"] == 0
    recommendation = report["recommendation"]
    assert recommendation["basis"] == "decision_level"
    assert (recommendation["gate"], recommendation["tp"], recommendation["fp"]) == (
        0.95,
        1,
        0,
    )
    assert recommendation["unbound"] is True
    assert recommendation["gap"] == {"max_false_p": 0.7, "min_true_p": 0.95}


def test_a_false_pair_that_the_veto_refused_is_not_a_merge_in_the_decision_level_view():
    refused = {
        "type": "Incident",
        "doc_path": "handovers/one.txt",
        "mention": "handovers/one.txt#1",
        "candidate": f"{EXPORT}::WO-1",
        "p": 0.96,
        "decision": "queue",
        "backend": "jev",
    }
    report = same_incident_report(
        incident_truth(), incident_merges(extra=[refused]), incident_records()
    )
    # p-only: the 0.96 false pair is a merge at the 0.9 gate and at every gate up to 0.95
    assert report["at_gate"]["fp"] == 1
    assert report["p_only_recommendation"]["gate"] is None
    # decision level: only decision merge counts, so the same pair is a queue and a non-merge
    view = report["decision_level"]
    assert (view["at_gate"]["tp"], view["at_gate"]["fp"], view["at_gate"]["fn"]) == (
        1,
        0,
        0,
    )
    assert view["at_gate"]["precision"] == 1.0 and view["at_gate"]["recall"] == 1.0
    assert view["refused"] == [
        {
            "doc_path": "handovers/one.txt",
            "mention": "handovers/one.txt#1",
            "candidate": f"{EXPORT}::WO-1",
            "p": 0.96,
            "decision": "queue",
            "label": False,
        }
    ]
    row = {r["gate"]: r for r in view["sweep"]}[0.9]
    assert (row["tp"], row["fp"]) == (view["at_gate"]["tp"], view["at_gate"]["fp"])
    # the recommendation is the decision-level one and says who held the false pair back
    recommendation = report["recommendation"]
    assert (
        recommendation["basis"] == "decision_level" and recommendation["gate"] == 0.95
    )
    assert recommendation["refused_false_pairs"] == {"count": 1, "max_p": 0.96}
    assert "veto" in recommendation["note"] and "cannot-link" in recommendation["note"]


def root_world():
    machine = [
        node("HP-01", "Asset"),
        node("PROC-1", "Procedure"),
        node("PROC-2", "Procedure"),
    ]
    truth = make_truth(
        [
            *machine,
            node("T-FM-A", "FailureMode", name="Seal wear"),
            node("T-FM-B", "FailureMode", name="Oil contamination"),
            node(
                "T-1", "Incident", date="2026-04-14", downtime_min=100, symptom_text="a"
            ),
            node(
                "T-2", "Incident", date="2026-05-14", downtime_min=200, symptom_text="b"
            ),
            node(
                "T-3", "Incident", date="2026-06-14", downtime_min=300, symptom_text="c"
            ),
            node(
                "T-4", "Incident", date="2026-07-14", downtime_min=400, symptom_text="d"
            ),
        ],
        [
            edge("T-FM-A", "RESOLVED_BY", "PROC-1"),
            edge("T-FM-B", "RESOLVED_BY", "PROC-2"),
            *[edge(i, "OCCURRED_ON", "HP-01") for i in ("T-1", "T-2", "T-3", "T-4")],
            edge("T-1", "ROOT_CAUSE", "T-FM-A"),
            edge("T-2", "ROOT_CAUSE", "T-FM-B"),
            edge("T-3", "ROOT_CAUSE", "T-FM-A"),
            edge("T-4", "ROOT_CAUSE", "T-FM-A"),
        ],
    )
    graph = make_graph(
        [
            *machine,
            node("FM-A", "FailureMode", name="seal wear"),
            node("FM-B", "FailureMode", name="oil contamination"),
            node(
                "G-1", "Incident", date="2026-04-14", downtime_min=100, symptom_text="a"
            ),
            node(
                "G-2", "Incident", date="2026-05-14", downtime_min=200, symptom_text="b"
            ),
            node(
                "G-3", "Incident", date="2026-06-14", downtime_min=300, symptom_text="c"
            ),
            node(
                "G-4", "Incident", date="2026-07-14", downtime_min=400, symptom_text="d"
            ),
            node(
                "G-5", "Incident", date="2026-08-14", downtime_min=500, symptom_text="e"
            ),
        ],
        [
            edge("FM-A", "RESOLVED_BY", "PROC-1"),
            edge("FM-B", "RESOLVED_BY", "PROC-2"),
            *[edge(f"G-{n}", "OCCURRED_ON", "HP-01") for n in range(1, 6)],
            edge("G-1", "ROOT_CAUSE", "FM-A"),
            edge("G-2", "ROOT_CAUSE", "FM-A"),
            edge("G-3", "ROOT_CAUSE", "FM-A"),
            edge("G-5", "ROOT_CAUSE", "FM-B"),
        ],
        edge_evidence={
            ("G-1", "ROOT_CAUSE", "FM-A"): [("handovers/x.txt", "q", 0.95, "linker")],
            ("G-2", "ROOT_CAUSE", "FM-A"): [("handovers/x.txt", "q", 0.8, "linker")],
            ("G-3", "ROOT_CAUSE", "FM-A"): [("handovers/x.txt", "q", 0.76, "linker")],
            ("G-5", "ROOT_CAUSE", "FM-B"): [("handovers/x.txt", "q", 0.9, "linker")],
        },
    )
    return truth, graph


def test_root_cause_links_are_correct_when_the_failure_mode_matches_the_truth_root_cause():
    truth, graph = root_world()
    report = root_cause_report(truth, graph, match(truth, graph))
    # G-1 right (0.95), G-2 wrong (0.8), G-3 right (0.76), G-4 matched with no link,
    # G-5 has no truth incident
    assert report["pairs"] == {"labeled": 4, "true": 3, "false": 1, "unlabeled": 1}
    at_gate = report["at_gate"]
    assert (at_gate["gate"], at_gate["tp"], at_gate["fp"], at_gate["fn"]) == (
        0.75,
        2,
        1,
        1,
    )
    assert report["bands"]["link"] == {"true": 2, "false": 1}
    assert report["bands"]["undecided"] == {"true": 1, "false": 0}
    assert report["sources"] == {"links": 4, "matched_incidents_without_a_link": 1}


def test_evaluate_gathers_the_three_kinds_with_the_gates():
    truth, graph, merges, extractions = parts_world()
    report = {"files": [{"doc_path": EXPORT, "merges": merges}]}
    result = evaluate(truth, graph, report, extractions, ALIASES, ScriptedDecider(0.2))
    assert set(result) == {"gates", "same_entity", "same_incident", "root_cause"}
    assert result["gates"] == questions.GATES
    assert result["same_entity"]["pairs"]["true"] == 2
    assert result["same_incident"]["pairs"]["labeled"] == 0
    assert result["root_cause"]["pairs"]["labeled"] == 0
