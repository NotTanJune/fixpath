"""Tests for the resolver, which merges duplicate entries, matches mentions to known
things and merges the final links.

The tests use hand-built input and a scripted decider. They make no network or model
calls and never read data/truth.
"""

import pytest

from app.decide import DecisionError
from app.ingest.resolve import Registry, rank, resolve
from app.ingest.types import ResolvedEdge, ResolvedNode
from app.ledger import BudgetExceeded
from resolve_stubs import (
    ALIASES,
    BH250,
    EXPORT,
    HANDOVER,
    INVENTORY,
    KX500,
    LOG,
    NOTE,
    OLD_STOCK,
    ScriptedDecider,
    edge,
    evidence,
    extraction,
    node,
    parts_fixture,
)


def by_id(resolution, node_id):
    return next(n for n in resolution.nodes if n.id == node_id)


def part_nodes(resolution):
    return {n.id: n for n in resolution.nodes if n.type == "Part"}


# --- Rank ---------------------------------------------------------------------------


def test_rank_follows_the_source_priority():
    ranks = {
        "manual": rank(extraction(BH250, "manual")),
        "csv": rank(extraction(EXPORT, "maintenance_note")),
        "note": rank(extraction(NOTE, "maintenance_note")),
        "inventory": rank(extraction(INVENTORY, "inventory")),
        "handover": rank(extraction(HANDOVER, "handover")),
        "log": rank(extraction(LOG, "log")),
    }
    assert ranks == {
        "manual": 1,
        "csv": 2,
        "note": 3,
        "inventory": 4,
        "handover": 5,
        "log": 6,
    }


# --- Canonical nodes: first non-missing wins ----------------------------------------


def part(doc, doc_type, props, label=None, extractor=None):
    return extraction(
        doc,
        doc_type,
        [node("Part", "SEAL-KIT-50", doc=doc, label=label, props=props)],
        extractor=extractor,
    )


def test_the_higher_ranked_source_wins_whatever_the_input_order():
    manual = part(BH250, "manual", {"description": "Seal kit from the manual"})
    inventory = part(
        INVENTORY, "inventory", {"description": "Seal kit from stock", "bin": "B-12"}
    )
    log = part(LOG, "log", {"description": "Seal kit from a log", "bin": "Z-99"})
    for order in (
        [manual, inventory, log],
        [log, inventory, manual],
        [inventory, log, manual],
    ):
        node_ = by_id(resolve(order, ALIASES, ScriptedDecider()), "SEAL-KIT-50")
        assert node_.props["description"] == "Seal kit from the manual"
        assert node_.props["bin"] == "B-12"


def test_placeholders_do_not_win():
    manual = part(
        BH250, "manual", {"description": "NA", "bin": "-", "unit_cost_sgd": "?"}
    )
    inventory = part(
        INVENTORY,
        "inventory",
        {"description": "Seal kit", "bin": "B-12", "unit_cost_sgd": 520.0},
    )
    props = by_id(
        resolve([manual, inventory], ALIASES, ScriptedDecider()), "SEAL-KIT-50"
    ).props
    assert props["description"] == "Seal kit"
    assert props["bin"] == "B-12"
    assert props["unit_cost_sgd"] == 520.0


def test_zero_is_a_value_and_missing_props_are_left_out():
    manual = part(BH250, "manual", {"lead_time_days": None})
    inventory = part(
        INVENTORY, "inventory", {"qty_on_hand": 0, "stock_as_of": "2026-10-15"}
    )
    props = by_id(
        resolve([manual, inventory], ALIASES, ScriptedDecider()), "SEAL-KIT-50"
    ).props
    assert props["qty_on_hand"] == 0
    assert "lead_time_days" not in props


def test_the_key_is_set_as_the_identity_prop():
    props = by_id(
        resolve([part(INVENTORY, "inventory", {})], ALIASES, ScriptedDecider()),
        "SEAL-KIT-50",
    ).props
    assert props["part_no"] == "SEAL-KIT-50"


def test_label_comes_from_the_first_candidate_that_has_one_other_than_the_key():
    manual = part(BH250, "manual", {}, label="SEAL-KIT-50")
    inventory = part(INVENTORY, "inventory", {}, label="Main cylinder seal kit")
    assert (
        by_id(
            resolve([manual, inventory], ALIASES, ScriptedDecider()), "SEAL-KIT-50"
        ).label
        == "Main cylinder seal kit"
    )
    only = resolve([manual], ALIASES, ScriptedDecider())
    assert by_id(only, "SEAL-KIT-50").label == "SEAL-KIT-50"


def test_evidence_and_sources_are_the_union_in_rank_order():
    manual = part(BH250, "manual", {})
    inventory = part(INVENTORY, "inventory", {})
    merged = by_id(
        resolve([inventory, manual], ALIASES, ScriptedDecider()), "SEAL-KIT-50"
    )
    assert merged.sources == [BH250, INVENTORY]
    assert [e.doc_path for e in merged.evidence] == [BH250, INVENTORY]


def test_list_props_take_the_union_without_case_duplicates():
    one = part(BH250, "manual", {"aliases": ["SK-50", "seal kit"]})
    two = part(INVENTORY, "inventory", {"aliases": ["Seal Kit", "SEALKIT", "SK-50"]})
    merged = by_id(resolve([two, one], ALIASES, ScriptedDecider()), "SEAL-KIT-50")
    assert merged.props["aliases"] == ["SK-50", "seal kit", "SEALKIT"]


def test_other_lists_are_not_unioned():
    one = extraction(
        BH250,
        "manual",
        [node("Procedure", "PROC-HP-04", doc=BH250, props={"steps": ["a", "b"]})],
    )
    two = extraction(
        KX500,
        "manual",
        [node("Procedure", "PROC-HP-04", doc=KX500, props={"steps": ["c"]})],
    )
    merged = by_id(resolve([one, two], ALIASES, ScriptedDecider()), "PROC-HP-04")
    assert merged.props["steps"] == ["a", "b"]


# --- The stock rule -----------------------------------------------------------------


def stock(doc, qty, as_of):
    props = {"qty_on_hand": qty, "stock_as_of": as_of}
    return part(doc, "inventory", props)


def test_the_newest_stock_count_wins_with_its_date():
    new = stock(INVENTORY, 3, "2026-10-15")
    old = stock(OLD_STOCK, 8, "2026-06-30")
    for order in ([new, old], [old, new]):
        merged = by_id(resolve(order, ALIASES, ScriptedDecider()), "SEAL-KIT-50")
        assert (merged.props["qty_on_hand"], merged.props["stock_as_of"]) == (
            3,
            "2026-10-15",
        )


def test_stock_ignores_a_manual_that_has_no_count():
    manual = part(BH250, "manual", {"description": "x"})
    merged = by_id(
        resolve(
            [manual, stock(OLD_STOCK, 8, "2026-06-30")], ALIASES, ScriptedDecider()
        ),
        "SEAL-KIT-50",
    )
    assert (merged.props["qty_on_hand"], merged.props["stock_as_of"]) == (
        8,
        "2026-06-30",
    )


def test_a_count_with_a_date_beats_a_count_without_one():
    dated = stock(OLD_STOCK, 8, "2026-06-30")
    undated = stock(INVENTORY, 1, None)
    merged = by_id(resolve([undated, dated], ALIASES, ScriptedDecider()), "SEAL-KIT-50")
    assert (merged.props["qty_on_hand"], merged.props["stock_as_of"]) == (
        8,
        "2026-06-30",
    )


def test_equal_dates_keep_the_higher_rank():
    a = stock(INVENTORY, 3, "2026-10-15")
    b = part(LOG, "log", {"qty_on_hand": 7, "stock_as_of": "2026-10-15"})
    merged = by_id(resolve([b, a], ALIASES, ScriptedDecider()), "SEAL-KIT-50")
    assert merged.props["qty_on_hand"] == 3


# --- Component, failure mode, procedure and line ------------------------------------


def test_a_non_canonical_component_is_unresolved_and_its_edges_are_dropped():
    manual = extraction(
        BH250,
        "manual",
        [
            node("Asset", "HP-01", doc=BH250),
            node("Component", "main cylinder", doc=BH250, canonical=False),
        ],
        [
            edge(
                ("Asset", "HP-01"),
                "HAS_COMPONENT",
                ("Component", "main cylinder"),
                doc=BH250,
            )
        ],
    )
    resolution = resolve([manual], ALIASES, ScriptedDecider())
    assert [n.id for n in resolution.nodes] == ["HP-01"]
    assert resolution.edges == []
    (item,) = resolution.review
    assert item.kind == "unresolved"
    assert item.doc_paths == [BH250]
    assert item.payload["type"] == "Component"
    assert item.payload["mention"] == "main cylinder"
    assert item.payload["dropped_edges"] == 1


def test_an_edge_to_an_unknown_failure_mode_is_dropped_with_a_review_item():
    manual = extraction(
        BH250,
        "manual",
        [node("AlarmCode", "E-217", doc=BH250)],
        [
            edge(
                ("AlarmCode", "E-217"),
                "INDICATES",
                ("FailureMode", "FM-NOPE"),
                doc=BH250,
            )
        ],
    )
    resolution = resolve([manual], ALIASES, ScriptedDecider())
    assert resolution.edges == []
    assert [i.kind for i in resolution.review] == ["unresolved"]


def test_the_same_failure_mode_id_in_two_manuals_keeps_the_first_and_flags_a_conflict():
    first = extraction(
        BH250,
        "manual",
        [
            node(
                "FailureMode",
                "FM-SEAL-WEAR",
                doc=BH250,
                props={"name": "Seal wear", "mechanism": "worn seals"},
            )
        ],
    )
    second = extraction(
        KX500,
        "manual",
        [
            node(
                "FailureMode",
                "FM-SEAL-WEAR",
                doc=KX500,
                props={
                    "name": "Seal wear",
                    "mechanism": "spindle seal worn",
                    "extra": 1,
                },
            )
        ],
    )
    resolution = resolve([first, second], ALIASES, ScriptedDecider())
    merged = by_id(resolution, "FM-SEAL-WEAR")
    assert merged.props["mechanism"] == "worn seals"
    assert "extra" not in merged.props
    assert merged.sources == [BH250]
    (item,) = resolution.review
    assert item.kind == "conflict"
    assert sorted(item.doc_paths) == sorted([BH250, KX500])
    assert item.payload["id"] == "FM-SEAL-WEAR"


def test_the_losing_manuals_edges_on_the_id_are_dropped_and_counted():
    first = extraction(
        BH250,
        "manual",
        [
            node("FailureMode", "FM-SEAL-WEAR", doc=BH250, props={"name": "Seal wear"}),
            node("AlarmCode", "E-217", doc=BH250),
        ],
        [
            edge(
                ("AlarmCode", "E-217"),
                "INDICATES",
                ("FailureMode", "FM-SEAL-WEAR"),
                doc=BH250,
                props={"likelihood": 0.7},
            )
        ],
    )
    second = extraction(
        KX500,
        "manual",
        [
            node("FailureMode", "FM-SEAL-WEAR", doc=KX500, props={"name": "Seal wear"}),
            node("AlarmCode", "E-300", doc=KX500),
            node("AlarmCode", "E-301", doc=KX500),
        ],
        [
            edge(
                ("AlarmCode", "E-300"),
                "INDICATES",
                ("FailureMode", "FM-SEAL-WEAR"),
                doc=KX500,
            ),
            edge(
                ("AlarmCode", "E-301"),
                "INDICATES",
                ("FailureMode", "FM-SEAL-WEAR"),
                doc=KX500,
            ),
            edge(
                ("AlarmCode", "E-300"),
                "INDICATES",
                ("FailureMode", "FM-OTHER"),
                doc=KX500,
            ),
        ],
    )
    other = node("FailureMode", "FM-OTHER", doc=KX500, props={"name": "Other"})
    second.nodes.append(other)
    resolution = resolve([first, second], ALIASES, ScriptedDecider())
    assert [(e.src, e.dst, e.props) for e in resolution.edges] == [
        ("E-217", "FM-SEAL-WEAR", {"likelihood": 0.7}),
        ("E-300", "FM-OTHER", {}),
    ]
    (item,) = resolution.review
    assert item.kind == "conflict"
    assert item.payload["kept"] == BH250
    assert item.payload["other"] == KX500
    assert item.payload["dropped_edges"] == 2


def test_a_conflict_without_edges_counts_none_dropped():
    first = extraction(BH250, "manual", [node("Component", "HP-01-MAINCYL", doc=BH250)])
    second = extraction(
        KX500, "manual", [node("Component", "HP-01-MAINCYL", doc=KX500)]
    )
    resolution = resolve([first, second], ALIASES, ScriptedDecider())
    (item,) = resolution.review
    assert item.payload["dropped_edges"] == 0


def test_the_same_component_id_twice_in_one_manual_is_one_node_and_no_conflict():
    one = extraction(
        BH250,
        "manual",
        [
            node(
                "Component", "HP-01-MAINCYL", doc=BH250, props={"name": "Main cylinder"}
            )
        ],
    )
    two = extraction(
        BH250,
        "manual",
        [node("Component", "HP-01-MAINCYL", doc=BH250, props={"asset_id": "HP-01"})],
    )
    resolution = resolve([one, two], ALIASES, ScriptedDecider())
    assert resolution.review == []
    assert by_id(resolution, "HP-01-MAINCYL").props["asset_id"] == "HP-01"


# --- Mentions resolved by rule ------------------------------------------------------


def incident_with(edges, doc=EXPORT):
    return extraction(
        doc,
        "maintenance_note",
        [
            node(
                "Incident",
                "WO-0001",
                doc=doc,
                canonical=False,
                props={"date": "2026-05-01"},
            )
        ],
        edges,
    )


def inventory_assets(doc=INVENTORY):
    return extraction(
        doc,
        "inventory",
        [
            node(
                "Asset",
                "HP-01",
                doc=doc,
                label="Hydraulic press",
                props={"id": "HP-01", "name": "Hydraulic press", "aliases": ["HP1"]},
            ),
            node("Asset", "CNC-01", doc=doc, props={"id": "CNC-01"}),
        ],
        extractor="inventory-normalizer",
    )


def edge_targets(resolution, rel):
    return sorted((e.src, e.dst) for e in resolution.edges if e.rel == rel)


def test_an_asset_mention_resolves_by_the_alias_table():
    note = incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "OCCURRED_ON",
                ("Asset", "the press"),
                doc=EXPORT,
            )
        ]
    )
    decider = ScriptedDecider()
    resolution = resolve([inventory_assets(), note], ALIASES, decider)
    assert [e.dst for e in resolution.edges if e.rel == "OCCURRED_ON"] == ["HP-01"]
    assert decider.calls == []
    decision = next(m for m in resolution.merges if m.type == "Asset")
    assert (decision.mention, decision.candidate, decision.p, decision.decision) == (
        "the press",
        "HP-01",
        1.0,
        "rule",
    )
    assert decision.doc_path == EXPORT
    assert decision.backend is None


def test_an_asset_id_that_is_a_known_node_is_not_a_mention():
    note = incident_with(
        [edge(("Incident", "WO-0001"), "OCCURRED_ON", ("Asset", "HP-01"), doc=EXPORT)]
    )
    resolution = resolve([inventory_assets(), note], ALIASES, ScriptedDecider())
    assert [m for m in resolution.merges if m.type == "Asset"] == []


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("ERR 217", "E-217"),
        ("Err:217", "E-217"),
        ("ALM SP-104", "SP-104"),
        ("VT18", "VT-018"),
        ("RB560", "RB-560"),
    ],
)
def test_an_alarm_mention_resolves_by_normalizing(text, code):
    note = incident_with(
        [edge(("Incident", "WO-0001"), "TRIGGERED_BY", ("AlarmCode", text), doc=EXPORT)]
    )
    resolution = resolve([note], ALIASES, ScriptedDecider())
    assert [e.dst for e in resolution.edges if e.rel == "TRIGGERED_BY"] == [code]
    alarm = by_id(resolution, code)
    assert alarm.type == "AlarmCode"
    assert alarm.props["code"] == code
    assert alarm.sources == [EXPORT] or alarm.evidence[0].doc_path == EXPORT
    assert any(
        m.decision == "rule" and m.type == "AlarmCode" for m in resolution.merges
    )


def test_an_alarm_code_a_manual_lists_keeps_the_manual_node():
    manual = extraction(
        BH250,
        "manual",
        [
            node(
                "AlarmCode",
                "E-217",
                doc=BH250,
                props={"code": "E-217", "text": "Main cylinder pressure loss"},
            )
        ],
    )
    note = incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "TRIGGERED_BY",
                ("AlarmCode", "ERR 217"),
                doc=EXPORT,
            )
        ]
    )
    resolution = resolve([manual, note], ALIASES, ScriptedDecider())
    alarm = by_id(resolution, "E-217")
    assert alarm.props["text"] == "Main cylinder pressure loss"
    assert alarm.sources == [BH250]


def test_an_unreadable_alarm_mention_is_unresolved_and_asks_no_decider():
    note = incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "TRIGGERED_BY",
                ("AlarmCode", "alarm 12"),
                doc=EXPORT,
            )
        ]
    )
    decider = ScriptedDecider(1.0)
    resolution = resolve([note], ALIASES, decider)
    assert decider.calls == []
    assert resolution.edges == []
    assert [i.kind for i in resolution.review] == ["unresolved"]


def test_a_part_mention_resolves_by_part_key():
    note = incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "USED",
                ("Part", "SEAL KIT 50"),
                doc=EXPORT,
                props={"qty": 1},
            )
        ]
    )
    decider = ScriptedDecider()
    resolution = resolve([*parts_fixture(), note], ALIASES, decider)
    assert [e.dst for e in resolution.edges if e.rel == "USED"] == ["SEAL-KIT-50"]
    assert decider.calls == []
    assert any(
        m.type == "Part" and m.decision == "rule" and m.p == 1.0
        for m in resolution.merges
    )


def test_a_dashed_part_key_on_an_edge_is_still_a_mention():
    note = incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "USED",
                ("Part", "BRG-6205"),
                doc=EXPORT,
                props={"qty": 1},
            )
        ]
    )
    decider = ScriptedDecider(0.1)
    resolution = resolve([*parts_fixture(), note], ALIASES, decider)
    assert "merge Part" in decider.purposes()
    assert [e for e in resolution.edges if e.rel == "USED"] == []


def test_a_part_key_shared_by_two_canonical_parts_is_not_a_rule_match():
    two = extraction(
        INVENTORY,
        "inventory",
        [
            node("Part", "AB-CD", doc=INVENTORY, label="First part"),
            node("Part", "A-BCD", doc=INVENTORY, label="Second part"),
        ],
    )
    note = incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "USED",
                ("Part", "ABCD"),
                doc=EXPORT,
                props={"qty": 1},
            )
        ]
    )
    decider = ScriptedDecider(0.1)
    resolve([two, note], ALIASES, decider)
    assert "merge Part" in decider.purposes()


# --- Fuzzy candidates and the gates -------------------------------------------------


def used(text, doc=EXPORT):
    return incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "USED",
                ("Part", text),
                doc=doc,
                props={"qty": 2},
                quote=f"{text} x2",
            )
        ],
        doc=doc,
    )


def asked_candidates(decider):
    return [s["candidate"]["id"] for s in decider.states("merge Part")]


def test_hlp46_finds_the_oil_pail():
    decider = ScriptedDecider(0.1)
    resolve([*parts_fixture(), used("HLP46")], ALIASES, decider)
    assert "OIL-HLP46-20L" in asked_candidates(decider)


def test_sk_50_finds_the_seal_kit():
    decider = ScriptedDecider(0.1)
    resolve([*parts_fixture(), used("SK-50")], ALIASES, decider)
    assert "SEAL-KIT-50" in asked_candidates(decider)


def test_the_state_describes_the_mention_and_the_candidate():
    decider = ScriptedDecider(0.1)
    resolve([*parts_fixture(), used("SK-50")], ALIASES, decider)
    state = next(
        s for s in decider.states("merge Part") if s["candidate"]["id"] == "SEAL-KIT-50"
    )
    assert state["mention"] == {
        "type": "Part",
        "text": "SK-50",
        "quote": "SK-50 x2",
        "doc_path": EXPORT,
    }
    assert state["candidate"]["id"] == "SEAL-KIT-50"
    assert state["candidate"]["description"] == "SK-50 seal kit (order no. SEAL-KIT-50)"
    assert len(state["candidate"]["evidence"]) <= 2


def candidate_of(state):
    """The candidate id of a same_entity state, or None for any other question."""
    return state.get("candidate", {}).get("id")


def script_for(candidate, p, other=0.0):
    return lambda purpose, state: p if candidate_of(state) == candidate else other


def test_at_095_the_mention_merges_and_the_alias_is_appended():
    decider = ScriptedDecider(script_for("SEAL-KIT-50", 0.95))
    resolution = resolve([*parts_fixture(), used("SK-50")], ALIASES, decider)
    assert [e.dst for e in resolution.edges if e.rel == "USED"] == ["SEAL-KIT-50"]
    assert by_id(resolution, "SEAL-KIT-50").props["aliases"] == ["SK-50"]
    decision = next(m for m in resolution.merges if m.mention == "SK-50")
    assert (decision.decision, decision.candidate, decision.p, decision.backend) == (
        "merge",
        "SEAL-KIT-50",
        0.95,
        "fake",
    )
    assert resolution.review == []


def test_an_alias_is_not_appended_twice():
    extractions = [*parts_fixture()]
    extractions[1].nodes[0].props["aliases"] = ["sk-50"]
    resolution = resolve(
        [*extractions, used("SK-50")],
        ALIASES,
        ScriptedDecider(script_for("SEAL-KIT-50", 0.95)),
    )
    assert by_id(resolution, "SEAL-KIT-50").props["aliases"] == ["sk-50"]


def test_the_best_candidate_decides():
    def script(purpose, state):
        return {"SEAL-KIT-50": 0.65, "SEAL-RV-25": 0.6}.get(candidate_of(state), 0.2)

    resolution = resolve(
        [*parts_fixture(), used("SK-50")], ALIASES, ScriptedDecider(script)
    )
    (decision,) = [m for m in resolution.merges if m.mention == "SK-50"]
    assert (decision.decision, decision.candidate, decision.p) == (
        "queue",
        "SEAL-KIT-50",
        0.65,
    )


def test_at_the_entity_gate_the_mention_is_merged():
    resolution = resolve(
        [*parts_fixture(), used("SK-50")],
        ALIASES,
        ScriptedDecider(script_for("SEAL-KIT-50", 0.7)),
    )
    decision = next(m for m in resolution.merges if m.mention == "SK-50")
    assert (decision.decision, decision.candidate, decision.p) == (
        "merge",
        "SEAL-KIT-50",
        0.7,
    )
    assert resolution.review == []


def test_below_the_entity_gate_the_mention_is_queued_and_the_edge_dropped():
    decider = ScriptedDecider(script_for("SEAL-KIT-50", 0.65))
    resolution = resolve([*parts_fixture(), used("SK-50")], ALIASES, decider)
    assert [e for e in resolution.edges if e.rel == "USED"] == []
    assert "aliases" not in by_id(resolution, "SEAL-KIT-50").props
    decision = next(m for m in resolution.merges if m.mention == "SK-50")
    assert (decision.decision, decision.candidate, decision.p) == (
        "queue",
        "SEAL-KIT-50",
        0.65,
    )
    (item,) = resolution.review
    assert item.kind == "merge"
    assert item.doc_paths == [EXPORT]
    assert item.confidence == 0.65
    assert item.payload["mention"] == "SK-50"
    assert item.payload["candidate"] == "SEAL-KIT-50"
    assert item.payload["dropped_edges"] == 1


def test_at_03_the_mention_is_unresolved():
    decider = ScriptedDecider(script_for("SEAL-KIT-50", 0.3))
    resolution = resolve([*parts_fixture(), used("SK-50")], ALIASES, decider)
    assert [e for e in resolution.edges if e.rel == "USED"] == []
    decision = next(m for m in resolution.merges if m.mention == "SK-50")
    assert decision.decision == "unresolved"
    assert decision.p == 0.3
    (item,) = resolution.review
    assert item.kind == "unresolved"
    assert item.payload["dropped_edges"] == 1


def test_no_candidate_is_unresolved_with_no_p():
    resolution = resolve(
        [*parts_fixture(), used("zzzzqqqq")], ALIASES, ScriptedDecider(1.0)
    )
    decision = next(m for m in resolution.merges if m.mention == "zzzzqqqq")
    assert (decision.decision, decision.candidate, decision.p) == (
        "unresolved",
        None,
        None,
    )
    assert [i.kind for i in resolution.review] == ["unresolved"]


def test_a_refusal_never_merges():
    resolution = resolve(
        [*parts_fixture(), used("SK-50")], ALIASES, ScriptedDecider(lambda p, s: None)
    )
    assert [e for e in resolution.edges if e.rel == "USED"] == []
    decision = next(m for m in resolution.merges if m.mention == "SK-50")
    assert decision.decision == "unresolved"
    (item,) = resolution.review
    assert item.kind == "merge"
    assert item.payload["reason"] == "no decision"


def test_a_decider_error_becomes_a_review_item_and_the_run_goes_on():
    resolution = resolve(
        [*parts_fixture(), used("SK-50")],
        ALIASES,
        ScriptedDecider(lambda p, s: DecisionError("boom")),
    )
    (item,) = resolution.review
    assert item.payload["reason"] == "decider error: DecisionError"
    assert [e for e in resolution.edges if e.rel == "USED"] == []


def test_a_failed_candidate_is_recorded_when_another_candidate_merges():
    def script(purpose, state):
        if candidate_of(state) == "SEAL-RV-25":
            return DecisionError("boom")
        return 0.95 if candidate_of(state) == "SEAL-KIT-50" else 0.1

    resolution = resolve(
        [*parts_fixture(), used("seal kit")], ALIASES, ScriptedDecider(script)
    )
    assert [e.dst for e in resolution.edges if e.rel == "USED"] == ["SEAL-KIT-50"]
    decision = next(m for m in resolution.merges if m.mention == "seal kit")
    assert (decision.decision, decision.candidate, decision.p) == (
        "merge",
        "SEAL-KIT-50",
        0.95,
    )
    (item,) = resolution.review
    assert item.kind == "merge"
    assert item.payload["reason"] == "a candidate failed to decide"
    assert item.payload["candidate"] == "SEAL-KIT-50"
    assert item.payload["errors"] == [
        {"candidate": "SEAL-RV-25", "reason": "decider error: DecisionError"}
    ]


def test_a_failed_candidate_is_recorded_when_another_candidate_is_queued():
    def script(purpose, state):
        if candidate_of(state) == "SEAL-RV-25":
            return DecisionError("boom")
        return 0.65 if candidate_of(state) == "SEAL-KIT-50" else 0.1

    resolution = resolve(
        [*parts_fixture(), used("seal kit")], ALIASES, ScriptedDecider(script)
    )
    (item,) = resolution.review
    assert item.payload["reason"] == "unsure match"
    assert item.payload["errors"] == [
        {"candidate": "SEAL-RV-25", "reason": "decider error: DecisionError"}
    ]


def test_a_review_item_without_a_failed_candidate_has_no_errors_key():
    resolution = resolve(
        [*parts_fixture(), used("seal kit")],
        ALIASES,
        ScriptedDecider(script_for("SEAL-KIT-50", 0.65)),
    )
    (item,) = resolution.review
    assert "errors" not in item.payload


def test_every_failed_candidate_is_listed_when_all_of_them_fail():
    decider = ScriptedDecider(lambda purpose, state: DecisionError("boom"))
    resolution = resolve([*parts_fixture(), used("seal kit")], ALIASES, decider)
    (item,) = resolution.review
    assert item.payload["reason"] == "decider error: DecisionError"
    asked = asked_candidates(decider)
    assert len(asked) > 1
    assert [e["candidate"] for e in item.payload["errors"]] == asked


def test_a_spent_budget_stops_the_run():
    with pytest.raises(BudgetExceeded):
        resolve(
            [*parts_fixture(), used("SK-50")],
            ALIASES,
            ScriptedDecider(lambda p, s: BudgetExceeded("spent")),
        )


def test_each_distinct_mention_is_resolved_once_from_its_first_occurrence():
    first = used("SK-50", doc=EXPORT)
    second = extraction(
        NOTE,
        "maintenance_note",
        [
            node(
                "Incident",
                f"{NOTE}#1",
                doc=NOTE,
                canonical=False,
                props={"date": "2026-05-02"},
                extractor="sol-text",
            )
        ],
        [
            edge(
                ("Incident", f"{NOTE}#1"),
                "USED",
                ("Part", "SK-50"),
                doc=NOTE,
                props={"qty": 1},
                quote="later quote",
                extractor="sol-text",
            )
        ],
    )
    decider = ScriptedDecider(script_for("SEAL-KIT-50", 0.95))
    resolution = resolve([*parts_fixture(), first, second], ALIASES, decider)
    assert (
        sum(
            1
            for s in decider.states("merge Part")
            if s["candidate"]["id"] == "SEAL-KIT-50"
        )
        == 1
    )
    assert decider.states("merge Part")[0]["mention"]["quote"] == "SK-50 x2"
    assert [m for m in resolution.merges if m.mention == "SK-50"][0].doc_path == EXPORT
    assert len([m for m in resolution.merges if m.mention == "SK-50"]) == 1
    assert len([e for e in resolution.edges if e.rel == "USED"]) == 2


def test_dropped_edges_are_counted_per_mention():
    second = extraction(
        NOTE,
        "maintenance_note",
        [
            node(
                "Incident",
                f"{NOTE}#1",
                doc=NOTE,
                canonical=False,
                props={"date": "2026-05-02"},
                extractor="sol-text",
            )
        ],
        [
            edge(
                ("Incident", f"{NOTE}#1"),
                "USED",
                ("Part", "SK-50"),
                doc=NOTE,
                props={"qty": 1},
                extractor="sol-text",
            )
        ],
    )
    resolution = resolve(
        [*parts_fixture(), used("SK-50"), second],
        ALIASES,
        ScriptedDecider(script_for("SEAL-KIT-50", 0.65)),
    )
    (item,) = resolution.review
    assert item.payload["dropped_edges"] == 2
    assert item.doc_paths == [EXPORT]


def test_asset_mentions_go_to_the_decider_too():
    note = incident_with(
        [
            edge(
                ("Incident", "WO-0001"),
                "OCCURRED_ON",
                ("Asset", "hydraulic ram press"),
                doc=EXPORT,
            )
        ]
    )
    decider = ScriptedDecider(script_for("HP-01", 0.95))
    resolution = resolve([inventory_assets(), note], ALIASES, decider)
    assert "merge Asset" in decider.purposes()
    assert by_id(resolution, "HP-01").props["aliases"] == ["HP1", "hydraulic ram press"]


# --- Non-canonical nodes ------------------------------------------------------------


def test_a_non_canonical_part_node_that_resolves_adds_its_props_to_the_part():
    raw = extraction(
        OLD_STOCK,
        "inventory",
        [
            node(
                "Part",
                "HLP46",
                doc=OLD_STOCK,
                label="HLP46",
                canonical=False,
                props={
                    "part_no": "HLP46",
                    "qty_on_hand": 12,
                    "stock_as_of": "2026-06-30",
                },
                extractor="inventory-normalizer",
            )
        ],
    )
    resolution = resolve(
        [*parts_fixture(), raw],
        ALIASES,
        ScriptedDecider(script_for("OIL-HLP46-20L", 0.95)),
    )
    oil = by_id(resolution, "OIL-HLP46-20L")
    assert oil.props["part_no"] == "OIL-HLP46-20L"
    assert oil.props["qty_on_hand"] == 9
    assert oil.props["stock_as_of"] == "2026-10-15"
    assert oil.label == "Hydraulic oil HLP 46, 20 litre pail"
    assert oil.sources == [INVENTORY, BH250, OLD_STOCK] or oil.sources == [
        BH250,
        INVENTORY,
        OLD_STOCK,
    ]
    assert "HLP46" in oil.props["aliases"]


def test_an_unresolved_non_canonical_node_is_dropped_with_a_review_item():
    raw = extraction(
        OLD_STOCK,
        "inventory",
        [
            node(
                "Part",
                "zzzzqqqq",
                doc=OLD_STOCK,
                canonical=False,
                extractor="inventory-normalizer",
            )
        ],
    )
    resolution = resolve([*parts_fixture(), raw], ALIASES, ScriptedDecider(1.0))
    assert "zzzzqqqq" not in [n.id for n in resolution.nodes]
    (item,) = resolution.review
    assert item.kind == "unresolved"
    assert item.payload["dropped_nodes"] == 1


# --- The final edge merge -----------------------------------------------------------


def test_equal_edges_merge_with_the_first_ranked_props_and_all_the_evidence():
    manual = extraction(
        BH250,
        "manual",
        [node("AlarmCode", "E-217", doc=BH250), node("Asset", "HP-01", doc=BH250)],
        [
            edge(
                ("Asset", "HP-01"),
                "RAISED",
                ("AlarmCode", "E-217"),
                doc=BH250,
                props={"count": 7},
                extractor="sol-manual",
            )
        ],
    )
    other = extraction(
        KX500,
        "manual",
        [node("AlarmCode", "E-217", doc=KX500), node("Asset", "HP-01", doc=KX500)],
        [
            edge(
                ("Asset", "HP-01"),
                "RAISED",
                ("AlarmCode", "E-217"),
                doc=KX500,
                props={"count": 3},
                extractor="sol-manual",
            )
        ],
    )
    resolution = resolve([manual, other], ALIASES, ScriptedDecider())
    (merged,) = resolution.edges
    assert merged.props["count"] == 7
    assert [e.doc_path for e in merged.evidence] == [BH250, KX500]


def test_nodes_and_edges_are_sorted():
    manual = extraction(
        BH250,
        "manual",
        [
            node("AlarmCode", "E-230", doc=BH250),
            node("AlarmCode", "E-217", doc=BH250),
            node("FailureMode", "FM-B", doc=BH250),
            node("FailureMode", "FM-A", doc=BH250),
        ],
        [
            edge(
                ("AlarmCode", "E-230"), "INDICATES", ("FailureMode", "FM-B"), doc=BH250
            ),
            edge(
                ("AlarmCode", "E-217"), "INDICATES", ("FailureMode", "FM-B"), doc=BH250
            ),
            edge(
                ("AlarmCode", "E-217"), "INDICATES", ("FailureMode", "FM-A"), doc=BH250
            ),
        ],
    )
    resolution = resolve([manual], ALIASES, ScriptedDecider())
    assert [n.id for n in resolution.nodes] == sorted(n.id for n in resolution.nodes)
    keys = [(e.src, e.rel, e.dst) for e in resolution.edges]
    assert keys == sorted(keys)


def test_a_valid_edge_between_canonical_nodes_passes_through():
    inventory = inventory_assets()
    line = extraction(
        INVENTORY,
        "inventory",
        [node("Line", "LINE-03", doc=INVENTORY)],
        [
            edge(
                ("Line", "LINE-03"),
                "HAS_MACHINE",
                ("Asset", "HP-01"),
                doc=INVENTORY,
                extractor="inventory-normalizer",
            )
        ],
        extractor="inventory-normalizer",
    )
    resolution = resolve([inventory, line], ALIASES, ScriptedDecider())
    assert [(e.src, e.rel, e.dst) for e in resolution.edges] == [
        ("LINE-03", "HAS_MACHINE", "HP-01")
    ]
    assert resolution.review == []


# --- Registry mode ------------------------------------------------------------------


def existing(node_id, type_, props, label=None, sources=(INVENTORY,)):
    return ResolvedNode(
        id=node_id,
        type=type_,
        label=label or node_id,
        props=props,
        evidence=evidence(sources[0], "old quote"),
        sources=list(sources),
    )


def test_an_empty_registry_changes_nothing():
    manual = part(BH250, "manual", {"description": "x"})
    assert resolve([manual], ALIASES, ScriptedDecider()) == resolve(
        [manual], ALIASES, ScriptedDecider(), Registry.empty()
    )


def test_existing_values_win_and_new_values_fill_and_extend():
    registry = Registry(
        nodes=[
            existing(
                "SEAL-KIT-50",
                "Part",
                {
                    "part_no": "SEAL-KIT-50",
                    "description": "Old description",
                    "aliases": ["SK-50"],
                },
            )
        ],
        edges=[],
    )
    manual = part(
        BH250,
        "manual",
        {"description": "New description", "bin": "B-12", "aliases": ["sk50", "SK-50"]},
    )
    resolution = resolve([manual], ALIASES, ScriptedDecider(), registry)
    merged = by_id(resolution, "SEAL-KIT-50")
    assert merged.props["description"] == "Old description"
    assert merged.props["bin"] == "B-12"
    assert merged.props["aliases"] == ["SK-50", "sk50"]
    assert merged.sources == [INVENTORY, BH250]


def test_a_mention_resolves_against_the_registry():
    registry = Registry(
        nodes=[
            existing(
                "SEAL-KIT-50",
                "Part",
                {
                    "part_no": "SEAL-KIT-50",
                    "description": "Main cylinder seal kit, 50 mm rod",
                },
            )
        ],
        edges=[],
    )
    decider = ScriptedDecider()
    resolution = resolve([used("SEAL KIT 50")], ALIASES, decider, registry)
    assert [e.dst for e in resolution.edges if e.rel == "USED"] == ["SEAL-KIT-50"]
    assert "SEAL-KIT-50" in [n.id for n in resolution.nodes]


def test_registry_edges_are_kept():
    registry = Registry(
        nodes=[
            existing("LINE-03", "Line", {"id": "LINE-03"}),
            existing("HP-01", "Asset", {"id": "HP-01"}),
        ],
        edges=[
            ResolvedEdge(
                src="LINE-03",
                rel="HAS_MACHINE",
                dst="HP-01",
                props={},
                evidence=evidence(INVENTORY),
            )
        ],
    )
    resolution = resolve([], ALIASES, ScriptedDecider(), registry)
    assert [(e.src, e.dst) for e in resolution.edges] == [("LINE-03", "HP-01")]
    assert [n.id for n in resolution.nodes] == ["HP-01", "LINE-03"]


# --- Determinism --------------------------------------------------------------------


def test_the_same_inputs_give_byte_identical_json():
    def run():
        extractions = [*parts_fixture(), used("SK-50"), used("HLP46"), used("zzzzqqqq")]
        decider = ScriptedDecider(
            lambda purpose, state: (
                0.95 if candidate_of(state) in {"SEAL-KIT-50", "OIL-HLP46-20L"} else 0.2
            )
        )
        return resolve(extractions, ALIASES, decider).model_dump_json()

    assert run() == run()
