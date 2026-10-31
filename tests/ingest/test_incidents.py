"""Tests for incident clustering: how records about the same incident are grouped, how
incident numbers are given, and how merged values and the existing registry are
handled. Hand-built drafts and a scripted decider stand in for real data and models."""

from resolve_stubs import (
    ALIASES,
    BH250,
    EXPORT,
    HANDOVER,
    INVENTORY,
    LOG,
    NOTE,
    ScriptedDecider,
    edge,
    extraction,
    node,
    parts_fixture,
)
from app.ingest.resolve import Registry, resolve
from app.ingest.types import ResolvedEdge, ResolvedNode
from resolve_stubs import evidence

OTHER_EXPORT = "maintenance/maint_log_export_2025.csv"
OTHER_HANDOVER = "handovers/Handover_20260823_A.txt"


def plant():
    """The five machines, as the Machines sheet gives them."""
    return extraction(
        INVENTORY,
        "inventory",
        [node("Asset", tag, doc=INVENTORY, props={"id": tag}) for tag in ALIASES.tags],
        extractor="inventory-normalizer",
    )


def draft_edges(src, machine, alarm, parts, doc, extractor):
    edges = []
    if machine:
        edges.append(
            edge(
                ("Incident", src),
                "OCCURRED_ON",
                ("Asset", machine),
                doc=doc,
                extractor=extractor,
            )
        )
    if alarm:
        edges.append(
            edge(
                ("Incident", src),
                "TRIGGERED_BY",
                ("AlarmCode", alarm),
                doc=doc,
                extractor=extractor,
            )
        )
    for part, qty in parts:
        edges.append(
            edge(
                ("Incident", src),
                "USED",
                ("Part", part),
                doc=doc,
                props={"qty": qty},
                extractor=extractor,
            )
        )
    return edges


def drafts(doc, doc_type, items):
    """An extraction of incident drafts. Each item is a dict with key, date and any
    of shift, downtime, symptom, action, tech, machine, alarm, parts, quote."""
    extractor = "csv-parser" if doc.endswith(".csv") else "sol-text"
    nodes, edges = [], []
    for item in items:
        key = item["key"]
        props = {
            "date": item.get("date"),
            "shift": item.get("shift"),
            "downtime_min": item.get("downtime"),
            "symptom_text": item.get("symptom", f"symptom of {key}"),
            "technician": item.get("tech"),
            "resolution_text": item.get("action"),
        }
        nodes.append(
            node(
                "Incident",
                key,
                doc=doc,
                canonical=False,
                props=props,
                label=props["symptom_text"],
                quote=item.get("quote", f"row {key}"),
                extractor=extractor,
            )
        )
        edges += draft_edges(
            key,
            item.get("machine"),
            item.get("alarm"),
            item.get("parts", []),
            doc,
            extractor,
        )
    return extraction(doc, doc_type, nodes, edges)


def csv(items, doc=EXPORT):
    return drafts(doc, "maintenance_note", items)


def handover(items, doc=HANDOVER):
    for number, item in enumerate(items, start=1):
        item.setdefault("key", f"{doc}#{number}")
    return drafts(doc, "handover", items)


def run(extractions, script=0.0, registry=None):
    decider = ScriptedDecider(script)
    resolution = resolve(
        [plant(), *parts_fixture(), *extractions], ALIASES, decider, registry
    )
    return resolution, decider


def incidents(resolution):
    return [n for n in resolution.nodes if n.type == "Incident"]


def edges_of(resolution, rel, src=None):
    return sorted(
        (e.src, e.dst)
        for e in resolution.edges
        if e.rel == rel and (src is None or e.src == src)
    )


def pair_script(table, default=0.0):
    """A script that answers by the keys of the two records."""

    def script(purpose, state):
        if purpose != "merge Incident":
            return default
        a, b = state["record_a"]["quote"], state["record_b"]["quote"]
        return table.get((a, b), table.get((b, a), default))

    return script


# --- The same-key rule --------------------------------------------------------------


def test_repeated_work_orders_in_one_document_merge_by_rule():
    export = csv(
        [
            {
                "key": "WO-0001",
                "date": "2026-05-01",
                "downtime": 120,
                "machine": "HP-01",
                "parts": [("SEAL-KIT-50", 1)],
            },
            {
                "key": "WO-0001",
                "date": "2026-05-01",
                "downtime": 120,
                "machine": "HP-01",
                "parts": [("SEAL-KIT-50", 1)],
                "tech": "Arun",
            },
        ]
    )
    resolution, decider = run([export])
    (incident,) = incidents(resolution)
    assert incident.props["technician"] == "Arun"
    assert decider.calls == []
    assert [i for i in resolution.review if i.kind == "conflict"] == []
    rule = [m for m in resolution.merges if m.type == "Incident"]
    assert [(m.decision, m.p, m.mention, m.candidate, m.doc_path) for m in rule] == [
        ("rule", 1.0, "WO-0001", "WO-0001", EXPORT)
    ]
    assert len(edges_of(resolution, "USED")) == 1


def test_a_repeat_with_another_downtime_keeps_the_first_and_flags_a_conflict():
    export = csv(
        [
            {
                "key": "WO-0001",
                "date": "2026-05-01",
                "downtime": 120,
                "machine": "HP-01",
            },
            {
                "key": "WO-0001",
                "date": "2026-05-01",
                "downtime": 180,
                "machine": "HP-01",
            },
        ]
    )
    resolution, _ = run([export])
    (incident,) = incidents(resolution)
    assert incident.props["downtime_min"] == 120
    (item,) = [i for i in resolution.review if i.kind == "conflict"]
    assert item.doc_paths == [EXPORT]
    assert item.payload["key"] == "WO-0001"
    assert item.payload["differences"] == {"downtime_min": [120, 180]}


def test_a_repeat_with_another_date_is_a_conflict_too():
    export = csv(
        [
            {"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01"},
            {"key": "WO-0001", "date": "2026-05-02", "machine": "HP-01"},
        ]
    )
    resolution, _ = run([export])
    (item,) = [i for i in resolution.review if i.kind == "conflict"]
    assert item.payload["differences"] == {"date": ["2026-05-01", "2026-05-02"]}


def test_a_repeat_that_fills_a_missing_value_is_no_conflict():
    export = csv(
        [
            {"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01"},
            {
                "key": "WO-0001",
                "date": "2026-05-01",
                "downtime": 90,
                "machine": "HP-01",
            },
        ]
    )
    resolution, _ = run([export])
    assert incidents(resolution)[0].props["downtime_min"] == 90
    assert [i for i in resolution.review if i.kind == "conflict"] == []


# --- Candidate pairs ----------------------------------------------------------------


def test_drafts_more_than_a_day_apart_are_never_asked():
    resolution, decider = run(
        [
            csv([{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01"}]),
            handover([{"date": "2026-05-03", "machine": "HP-01"}]),
        ],
        script=0.99,
    )
    assert decider.purposes() == []
    assert len(incidents(resolution)) == 2


def test_drafts_one_day_apart_are_asked_and_dates_compare_as_dates():
    resolution, decider = run(
        [
            csv([{"key": "WO-0001", "date": "2026-04-30", "machine": "HP-01"}]),
            handover([{"date": "2026-05-01", "machine": "HP-01"}]),
        ],
        script=0.99,
    )
    assert decider.purposes() == ["merge Incident"]
    assert len(incidents(resolution)) == 1


def test_the_window_counts_days_across_a_month_boundary():
    one_day = run(
        [
            csv([{"key": "WO-0001", "date": "2026-02-28", "machine": "HP-01"}]),
            handover([{"date": "2026-03-01", "machine": "HP-01"}]),
        ]
    )[1]
    two_days = run(
        [
            csv([{"key": "WO-0001", "date": "2026-02-28", "machine": "HP-01"}]),
            handover([{"date": "2026-03-02", "machine": "HP-01"}]),
        ]
    )[1]
    assert one_day.purposes() == ["merge Incident"]
    assert two_days.purposes() == []


def test_drafts_from_one_document_are_never_paired():
    resolution, decider = run(
        [
            handover(
                [
                    {"date": "2026-05-01", "machine": "HP-01"},
                    {"date": "2026-05-01", "machine": "HP-01"},
                ]
            )
        ],
        script=0.99,
    )
    assert decider.calls == []
    assert len(incidents(resolution)) == 2


def test_a_draft_without_a_date_is_not_paired_and_gets_no_id():
    resolution, decider = run(
        [
            csv(
                [
                    {
                        "key": "WO-0001",
                        "date": None,
                        "machine": "HP-01",
                        "parts": [("SEAL-KIT-50", 1)],
                    }
                ]
            ),
            handover([{"date": "2026-05-01", "machine": "HP-01"}]),
        ],
        script=0.99,
    )
    assert decider.calls == []
    assert [n.id for n in incidents(resolution)] == ["INC-2026-001"]
    (item,) = [i for i in resolution.review if i.kind == "unresolved"]
    assert item.doc_paths == [EXPORT]
    assert item.payload["reason"] == "no date"


def test_the_state_holds_both_records():
    resolution, decider = run(
        [
            csv(
                [
                    {
                        "key": "WO-0001",
                        "date": "2026-05-01",
                        "machine": "HP-01",
                        "alarm": "E-217",
                        "downtime": 120,
                        "tech": "Arun",
                        "action": "replaced seals",
                        "quote": "WO-0001 row",
                        "parts": [("SEAL-KIT-50", 1)],
                    }
                ]
            ),
            handover(
                [
                    {
                        "date": "2026-05-01",
                        "shift": "B",
                        "machine": "the press",
                        "symptom": "oil leak",
                        "quote": "press leaking",
                    }
                ]
            ),
        ],
        script=0.2,
    )
    (state,) = decider.states("merge Incident")
    a, b = state["record_a"], state["record_b"]
    assert {a["source_type"], b["source_type"]} == {"maintenance_note", "handover"}
    csv_side = a if a["source_type"] == "maintenance_note" else b
    note_side = b if csv_side is a else a
    assert csv_side["machine"] == "HP-01"
    assert csv_side["alarm"] == "E-217"
    assert csv_side["downtime_min"] == 120
    assert csv_side["technician"] == "Arun"
    assert csv_side["action"] == "replaced seals"
    assert csv_side["quote"] == "WO-0001 row"
    assert csv_side["parts"] == [{"part": "SEAL-KIT-50", "qty": 1}]
    assert note_side["shift"] == "B"
    assert note_side["machine"] == "HP-01"
    assert note_side["symptom"] == "oil leak"


# --- The gates ----------------------------------------------------------------------


def two_records():
    return [
        csv(
            [
                {
                    "key": "WO-0001",
                    "date": "2026-05-01",
                    "machine": "HP-01",
                    "downtime": 120,
                }
            ]
        ),
        handover(
            [
                {
                    "date": "2026-05-01",
                    "machine": "HP-01",
                    "shift": "B",
                    "action": "reseated",
                }
            ]
        ),
    ]


def test_at_095_the_records_become_one_incident():
    resolution, _ = run(two_records(), script=0.95)
    (incident,) = incidents(resolution)
    assert incident.id == "INC-2026-001"
    assert sorted(incident.sources) == sorted([EXPORT, HANDOVER])
    merge = next(
        m for m in resolution.merges if m.type == "Incident" and m.decision != "rule"
    )
    assert (merge.decision, merge.p, merge.backend) == ("merge", 0.95, "fake")
    assert resolution.review == []


def test_at_07_the_records_stay_apart_with_a_merge_review_item():
    resolution, _ = run(two_records(), script=0.7)
    assert len(incidents(resolution)) == 2
    (item,) = resolution.review
    assert item.kind == "merge"
    assert sorted(item.doc_paths) == sorted([EXPORT, HANDOVER])
    assert item.confidence == 0.7
    decision = next(m for m in resolution.merges if m.type == "Incident")
    assert decision.decision == "queue"


def test_at_03_the_records_are_different_incidents():
    resolution, _ = run(two_records(), script=0.3)
    assert len(incidents(resolution)) == 2
    assert resolution.review == []
    decision = next(m for m in resolution.merges if m.type == "Incident")
    assert (decision.decision, decision.p) == ("new", 0.3)


def test_a_refusal_never_merges_incidents():
    resolution, _ = run(two_records(), script=lambda purpose, state: None)
    assert len(incidents(resolution)) == 2
    (item,) = resolution.review
    assert item.kind == "merge"
    assert item.payload["reason"] == "no decision"
    assert (
        next(m for m in resolution.merges if m.type == "Incident").decision
        == "unresolved"
    )


def test_every_pair_asked_is_logged():
    resolution, decider = run(two_records(), script=0.3)
    assert len(decider.calls) == 1
    (decision,) = [m for m in resolution.merges if m.type == "Incident"]
    assert decision.doc_path in {EXPORT, HANDOVER}
    assert (decision.doc_path, decision.mention, decision.candidate) in {
        (EXPORT, "WO-0001", f"{HANDOVER}::{HANDOVER}#1"),
        (HANDOVER, f"{HANDOVER}#1", f"{EXPORT}::WO-0001"),
    }


def test_the_second_record_of_a_pair_is_named_with_its_document():
    # the handover is a day earlier, so the work order is the second record
    later = csv([{"key": "WO-0001", "date": "2026-05-02", "machine": "HP-01"}])
    earlier = handover([{"date": "2026-05-01", "machine": "HP-01"}])
    resolution, _ = run([later, earlier], script=0.3)
    (decision,) = [m for m in resolution.merges if m.type == "Incident"]
    assert decision.doc_path == HANDOVER
    assert decision.mention == f"{HANDOVER}#1"
    assert decision.candidate == f"{EXPORT}::WO-0001"


# --- Cannot-link and chaining -------------------------------------------------------


def test_two_work_order_refs_never_share_a_cluster_even_at_099():
    first = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01", "quote": "wo1"}],
        doc=EXPORT,
    )
    second = csv(
        [{"key": "WO-0002", "date": "2026-05-01", "machine": "HP-01", "quote": "wo2"}],
        doc=OTHER_EXPORT,
    )
    note = handover([{"date": "2026-05-01", "machine": "HP-01", "quote": "ho"}])
    script = pair_script(
        {("wo1", "ho"): 0.95, ("wo2", "ho"): 0.99, ("wo1", "wo2"): 0.1}
    )
    resolution, _ = run([first, second, note], script)
    ids = {tuple(sorted(n.sources)) for n in incidents(resolution)}
    assert ids == {(EXPORT,), tuple(sorted([OTHER_EXPORT, HANDOVER]))}
    (item,) = resolution.review
    assert item.kind == "merge"
    assert item.payload["reason"] == "cannot link"
    blocked = [m for m in resolution.merges if m.type == "Incident" and m.p == 0.95]
    assert [m.decision for m in blocked] == ["queue"]


def test_two_records_of_one_document_never_share_a_cluster():
    export = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01", "quote": "wo1"}]
    )
    note = handover(
        [
            {"date": "2026-05-01", "machine": "HP-01", "quote": "h1"},
            {"date": "2026-05-01", "machine": "HP-01", "quote": "h2"},
        ]
    )
    script = pair_script({("wo1", "h1"): 0.99, ("wo1", "h2"): 0.95})
    resolution, _ = run([export, note], script)
    assert len(incidents(resolution)) == 2
    assert [i.payload["reason"] for i in resolution.review] == ["cannot link"]


def test_chaining_links_a_cluster_through_a_middle_record():
    a = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01", "quote": "a"}]
    )
    b = drafts(
        NOTE,
        "maintenance_note",
        [{"key": f"{NOTE}#1", "date": "2026-05-02", "machine": "HP-01", "quote": "b"}],
    )
    c = handover([{"date": "2026-05-03", "machine": "HP-01", "quote": "c"}])
    # a and c are two days apart, so that pair is never asked and cannot contradict
    script = pair_script({("a", "b"): 0.95, ("b", "c"): 0.95})
    resolution, decider = run([a, b, c], script)
    assert len(decider.calls) == 2
    (incident,) = incidents(resolution)
    assert sorted(incident.sources) == sorted([EXPORT, NOTE, HANDOVER])


def test_the_ends_of_a_chain_may_have_been_asked_if_they_passed_the_gate_too():
    a = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01", "quote": "a"}]
    )
    b = drafts(
        NOTE,
        "maintenance_note",
        [{"key": f"{NOTE}#1", "date": "2026-05-01", "machine": "HP-01", "quote": "b"}],
    )
    c = handover([{"date": "2026-05-02", "machine": "HP-01", "quote": "c"}])
    script = pair_script({("a", "b"): 0.95, ("b", "c"): 0.95, ("a", "c"): 0.9})
    resolution, decider = run([a, b, c], script)
    assert len(decider.calls) == 3
    (incident,) = incidents(resolution)
    assert sorted(incident.sources) == sorted([EXPORT, NOTE, HANDOVER])
    assert resolution.review == []


# --- A direct answer vetoes a chained union -----------------------------------------

CONTRADICTED = "contradicted by a direct answer"


def three_records():
    """A (the export), B (a loose note) and C (a handover), all within a day."""
    a = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01", "quote": "a"}]
    )
    b = drafts(
        NOTE,
        "maintenance_note",
        [{"key": f"{NOTE}#1", "date": "2026-05-01", "machine": "HP-01", "quote": "b"}],
    )
    c = handover([{"date": "2026-05-01", "machine": "HP-01", "quote": "c"}])
    return [a, b, c]


def idents(item_records):
    return sorted(tuple(r) for r in item_records)


def test_a_union_is_refused_when_a_direct_answer_contradicts_it():
    # A joins B (0.95) and C joins A (0.93), but B and C were asked and scored 0.75
    script = pair_script({("a", "b"): 0.95, ("a", "c"): 0.93, ("b", "c"): 0.75})
    resolution, decider = run(three_records(), script)
    assert len(decider.calls) == 3
    clusters = sorted(tuple(sorted(n.sources)) for n in incidents(resolution))
    assert clusters == [(HANDOVER,), tuple(sorted([EXPORT, NOTE]))]
    (veto,) = [i for i in resolution.review if i.payload["reason"] == CONTRADICTED]
    assert veto.kind == "merge"
    assert idents(veto.payload["records"]) == sorted(
        [(EXPORT, "WO-0001"), (HANDOVER, f"{HANDOVER}#1")]
    )
    assert veto.payload["p"] == 0.93
    assert veto.confidence == 0.93
    assert idents(veto.payload["contradicted_by"]["records"]) == sorted(
        [(NOTE, f"{NOTE}#1"), (HANDOVER, f"{HANDOVER}#1")]
    )
    assert veto.payload["contradicted_by"]["p"] == 0.75
    assert sorted(veto.doc_paths) == sorted([EXPORT, HANDOVER])
    # the contradicting pair keeps its own queue item
    assert [i.payload["reason"] for i in resolution.review].count("unsure match") == 1


def test_a_vetoed_pair_is_logged_as_queue():
    script = pair_script({("a", "b"): 0.95, ("a", "c"): 0.93, ("b", "c"): 0.75})
    resolution, _ = run(three_records(), script)
    decisions = {
        (m.mention, m.candidate): (m.decision, m.p)
        for m in resolution.merges
        if m.type == "Incident"
    }
    by_p = {p: d for d, p in decisions.values()}
    assert by_p == {0.95: "merge", 0.93: "queue", 0.75: "queue"}


def test_a_refusal_on_the_contradicting_pair_does_not_veto():
    script = pair_script({("a", "b"): 0.95, ("a", "c"): 0.93, ("b", "c"): None})
    resolution, _ = run(three_records(), script)
    (incident,) = incidents(resolution)
    assert sorted(incident.sources) == sorted([EXPORT, NOTE, HANDOVER])
    assert [i.payload["reason"] for i in resolution.review] == ["no decision"]


def test_a_decider_error_on_the_contradicting_pair_does_not_veto():
    from app.decide import DecisionError

    script = pair_script(
        {("a", "b"): 0.95, ("a", "c"): 0.93, ("b", "c"): DecisionError("x")}
    )
    resolution, _ = run(three_records(), script)
    (incident,) = incidents(resolution)
    assert sorted(incident.sources) == sorted([EXPORT, NOTE, HANDOVER])
    assert [i.payload["reason"] for i in resolution.review] == [
        "decider error: DecisionError"
    ]


def test_a_direct_answer_at_the_gate_does_not_veto():
    script = pair_script({("a", "b"): 0.95, ("a", "c"): 0.93, ("b", "c"): 0.9})
    resolution, _ = run(three_records(), script)
    assert len(incidents(resolution)) == 1


def test_a_direct_answer_below_the_queue_gate_vetoes_too():
    script = pair_script({("a", "b"): 0.95, ("a", "c"): 0.93, ("b", "c"): 0.1})
    resolution, _ = run(three_records(), script)
    assert len(incidents(resolution)) == 2
    (veto,) = [i for i in resolution.review if i.payload["reason"] == CONTRADICTED]
    assert veto.payload["contradicted_by"]["p"] == 0.1


def test_the_veto_reads_the_incident_gate(monkeypatch):
    from app.ingest import questions

    monkeypatch.setitem(questions.GATES, "merge_incident", 0.7)
    script = pair_script({("a", "b"): 0.95, ("a", "c"): 0.93, ("b", "c"): 0.75})
    resolution, _ = run(three_records(), script)
    assert len(incidents(resolution)) == 1


def test_a_cannot_link_is_reported_before_a_veto():
    a = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01", "quote": "a"}]
    )
    c = csv(
        [{"key": "WO-0002", "date": "2026-05-01", "machine": "HP-01", "quote": "c"}],
        doc=OTHER_EXPORT,
    )
    b = handover([{"date": "2026-05-01", "machine": "HP-01", "quote": "b"}])
    script = pair_script({("a", "b"): 0.92, ("b", "c"): 0.99})
    resolution, _ = run([a, c, b], script)
    reasons = [i.payload["reason"] for i in resolution.review]
    assert reasons == ["cannot link"]


def test_pairs_are_taken_in_descending_p():
    a = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01", "quote": "a"}]
    )
    c = csv(
        [{"key": "WO-0002", "date": "2026-05-01", "machine": "HP-01", "quote": "c"}],
        doc=OTHER_EXPORT,
    )
    b = handover([{"date": "2026-05-01", "machine": "HP-01", "quote": "b"}])
    # b joins c (0.99 comes first), so b-a is blocked: a and c carry two work orders
    script = pair_script({("a", "b"): 0.92, ("b", "c"): 0.99})
    resolution, _ = run([a, c, b], script)
    clusters = {tuple(sorted(n.sources)) for n in incidents(resolution)}
    assert tuple(sorted([HANDOVER, OTHER_EXPORT])) in clusters
    assert (EXPORT,) in clusters


# --- Numbering ----------------------------------------------------------------------


def test_ids_follow_the_earliest_date():
    resolution, _ = run(
        [
            csv(
                [
                    {"key": "WO-0003", "date": "2026-07-01", "machine": "HP-01"},
                    {"key": "WO-0001", "date": "2026-03-01", "machine": "CNC-01"},
                    {"key": "WO-0002", "date": "2026-05-01", "machine": "CNC-02"},
                ]
            )
        ]
    )
    by_date = {n.props["date"]: n.id for n in incidents(resolution)}
    assert by_date == {
        "2026-03-01": "INC-2026-001",
        "2026-05-01": "INC-2026-002",
        "2026-07-01": "INC-2026-003",
    }


def test_the_same_date_is_ordered_by_rank_then_doc_path():
    resolution, _ = run(
        [
            handover(
                [{"date": "2026-05-01", "machine": "CNC-01", "quote": "h"}],
                doc=HANDOVER,
            ),
            csv(
                [
                    {
                        "key": "WO-0001",
                        "date": "2026-05-01",
                        "machine": "CNC-02",
                        "quote": "c",
                    }
                ]
            ),
            drafts(
                NOTE,
                "maintenance_note",
                [
                    {
                        "key": f"{NOTE}#1",
                        "date": "2026-05-01",
                        "machine": "HP-01",
                        "quote": "n",
                    }
                ],
            ),
            handover(
                [{"date": "2026-05-01", "machine": "LT-01", "quote": "h2"}],
                doc=OTHER_HANDOVER,
            ),
        ]
    )
    machine = {
        n.id: edges_of(resolution, "OCCURRED_ON", n.id)[0][1]
        for n in incidents(resolution)
    }
    assert machine == {
        "INC-2026-001": "CNC-02",  # csv, rank 2
        "INC-2026-002": "HP-01",  # note, rank 3
        "INC-2026-003": "CNC-01",  # handover, rank 5, the earlier path
        "INC-2026-004": "LT-01",
    }


def test_numbering_restarts_each_year():
    resolution, _ = run(
        [
            csv(
                [
                    {"key": "WO-0001", "date": "2025-12-30", "machine": "HP-01"},
                    {"key": "WO-0002", "date": "2026-01-05", "machine": "HP-01"},
                ]
            )
        ]
    )
    assert sorted(n.id for n in incidents(resolution)) == [
        "INC-2025-001",
        "INC-2026-001",
    ]


# --- Merged props and edges ---------------------------------------------------------


def merged_pair():
    export = csv(
        [
            {
                "key": "WO-0001",
                "date": "2026-05-01",
                "downtime": 120,
                "machine": "HP-01",
                "alarm": "E-217",
                "symptom": "oil leak under ram",
                "tech": "Arun",
                "parts": [("SEAL-KIT-50", 1), ("OIL-HLP46-20L", 2)],
            }
        ]
    )
    note = handover(
        [
            {
                "date": "2026-05-01",
                "shift": "B",
                "downtime": 150,
                "machine": "HP-01",
                "alarm": "E-221",
                "symptom": "press leaking",
                "action": "changed seals",
                "parts": [("SEAL-KIT-50", 3), ("SEAL-RV-25", 1)],
            }
        ]
    )
    return export, note


def test_props_follow_rank_and_missing_values_are_filled():
    resolution, _ = run(list(merged_pair()), script=0.95)
    (incident,) = incidents(resolution)
    assert incident.props["symptom_text"] == "oil leak under ram"
    assert incident.props["downtime_min"] == 120
    assert incident.props["technician"] == "Arun"
    assert incident.props["shift"] == "B"
    assert incident.props["resolution_text"] == "changed seals"
    assert incident.props["date"] == "2026-05-01"
    assert incident.props["id"] == "INC-2026-001"
    assert incident.label == "oil leak under ram"


def test_edges_are_unioned_with_quantities_from_the_highest_ranked_member():
    resolution, _ = run(list(merged_pair()), script=0.95)
    inc = "INC-2026-001"
    assert edges_of(resolution, "TRIGGERED_BY", inc) == [(inc, "E-217"), (inc, "E-221")]
    assert edges_of(resolution, "OCCURRED_ON", inc) == [(inc, "HP-01")]
    qty = {e.dst: e.props["qty"] for e in resolution.edges if e.rel == "USED"}
    assert qty == {"SEAL-KIT-50": 1, "OIL-HLP46-20L": 2, "SEAL-RV-25": 1}


def test_evidence_is_the_union_of_the_members():
    resolution, _ = run(list(merged_pair()), script=0.95)
    (incident,) = incidents(resolution)
    assert [e.doc_path for e in incident.evidence] == [EXPORT, HANDOVER]


def test_downtime_stays_missing_when_no_member_has_one():
    resolution, _ = run(
        [csv([{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01"}])]
    )
    (incident,) = incidents(resolution)
    assert "downtime_min" not in incident.props


def test_the_machine_comes_from_the_highest_ranked_member_that_names_one():
    export = csv(
        [{"key": "WO-0001", "date": "2026-05-01", "symptom": "no machine named"}]
    )
    note = handover([{"date": "2026-05-01", "machine": "CNC-02"}])
    resolution, _ = run([export, note], script=0.95)
    assert edges_of(resolution, "OCCURRED_ON") == [("INC-2026-001", "CNC-02")]
    assert [i for i in resolution.review if i.kind == "conflict"] == []


def test_members_that_name_different_machines_raise_a_conflict():
    export = csv([{"key": "WO-0001", "date": "2026-05-01", "machine": "HP-01"}])
    note = handover([{"date": "2026-05-01", "machine": "CNC-02"}])
    resolution, _ = run([export, note], script=0.95)
    assert edges_of(resolution, "OCCURRED_ON") == [("INC-2026-001", "HP-01")]
    (item,) = resolution.review
    assert item.kind == "conflict"
    assert item.payload["incident"] == "INC-2026-001"
    assert item.payload["machines"] == {EXPORT: "HP-01", HANDOVER: "CNC-02"}
    assert sorted(item.doc_paths) == sorted([EXPORT, HANDOVER])


# --- Registry mode ------------------------------------------------------------------


def existing_incident(number=3, date="2026-05-01"):
    inc = f"INC-2026-{number:03d}"
    node_ = ResolvedNode(
        id=inc,
        type="Incident",
        label="old symptom",
        props={
            "id": inc,
            "date": date,
            "shift": "A",
            "downtime_min": 90,
            "symptom_text": "old symptom",
        },
        evidence=evidence(EXPORT, "old row"),
        sources=[EXPORT],
    )
    edges = [
        ResolvedEdge(
            src=inc,
            rel="OCCURRED_ON",
            dst="HP-01",
            props={},
            evidence=evidence(EXPORT, "old row"),
        )
    ]
    return node_, edges


def registry_with_plant():
    asset = ResolvedNode(
        id="HP-01",
        type="Asset",
        label="Press",
        props={"id": "HP-01"},
        evidence=evidence(INVENTORY),
        sources=[INVENTORY],
    )
    old, edges = existing_incident()
    return Registry(nodes=[asset, old], edges=edges)


def test_a_new_draft_merges_into_an_existing_incident_and_keeps_its_id():
    note = handover(
        [
            {
                "date": "2026-05-01",
                "shift": "C",
                "downtime": 100,
                "machine": "HP-01",
                "action": "reseated",
            }
        ]
    )
    resolution = resolve([note], ALIASES, ScriptedDecider(0.95), registry_with_plant())
    (incident,) = incidents(resolution)
    assert incident.id == "INC-2026-003"
    assert incident.props["downtime_min"] == 90  # the existing values win
    assert incident.props["shift"] == "A"
    assert incident.props["resolution_text"] == "reseated"  # a missing value is filled
    assert incident.sources == [EXPORT, HANDOVER]


def test_a_new_incident_takes_the_next_free_number():
    note = handover([{"date": "2026-05-20", "machine": "HP-01"}])
    resolution = resolve([note], ALIASES, ScriptedDecider(0.0), registry_with_plant())
    assert sorted(n.id for n in incidents(resolution)) == [
        "INC-2026-003",
        "INC-2026-004",
    ]
    (new,) = [n for n in incidents(resolution) if n.id == "INC-2026-004"]
    assert new.sources == [HANDOVER]


def test_a_draft_of_a_document_already_in_the_incident_is_not_paired_with_it():
    same_doc = csv(
        [{"key": "WO-0099", "date": "2026-05-01", "machine": "HP-01"}], doc=EXPORT
    )
    decider = ScriptedDecider(0.99)
    resolution = resolve([same_doc], ALIASES, decider, registry_with_plant())
    assert decider.calls == []
    assert len(incidents(resolution)) == 2


def test_existing_incidents_pass_through_with_their_edges():
    resolution = resolve([], ALIASES, ScriptedDecider(), registry_with_plant())
    (incident,) = incidents(resolution)
    assert incident.id == "INC-2026-003"
    assert edges_of(resolution, "OCCURRED_ON") == [("INC-2026-003", "HP-01")]


# --- Errors and determinism ---------------------------------------------------------


def test_a_decider_error_on_a_pair_is_a_review_item_and_the_run_goes_on():
    from app.decide import DecisionError

    resolution, _ = run(two_records(), script=lambda purpose, state: DecisionError("x"))
    assert len(incidents(resolution)) == 2
    (item,) = resolution.review
    assert item.kind == "merge"
    assert item.payload["reason"] == "decider error: DecisionError"


def test_a_spent_budget_stops_the_clustering():
    import pytest

    from app.ledger import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        run(two_records(), script=lambda purpose, state: BudgetExceeded("spent"))


def test_the_same_inputs_give_byte_identical_json():
    def once():
        export, note = merged_pair()
        other = csv(
            [{"key": "WO-0002", "date": "2026-05-02", "machine": "CNC-01"}],
            doc=OTHER_EXPORT,
        )
        script = pair_script({}, default=0.95)
        return run([export, note, other], script)[0].model_dump_json()

    assert once() == once()
