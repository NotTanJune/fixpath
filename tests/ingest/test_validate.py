"""Tests for the validator. Each rule gets a small bad item. They also check that
dropping an item drops what depends on it, and that a warning keeps its item. The
input is hand-built, with no network or model."""

from app.ingest.types import EvidenceDraft, Resolution, ResolvedEdge, ResolvedNode
from app.ingest.validate import Flag, Validated, validate

CSV = "maintenance/maint_log_export_2026.csv"


def ev(quote="a quote", doc=CSV):
    return [
        EvidenceDraft(doc_path=doc, quote=quote, extractor="csv-parser", confidence=0.9)
    ]


def rn(node_id, node_type, props, evidence="default"):
    return ResolvedNode(
        id=node_id,
        type=node_type,
        label=node_id,
        props=props,
        evidence=ev() if evidence == "default" else evidence,
        sources=[CSV],
    )


def re_(src, rel, dst, props=None, evidence="default"):
    return ResolvedEdge(
        src=src,
        rel=rel,
        dst=dst,
        props=props or {},
        evidence=ev() if evidence == "default" else evidence,
    )


def asset(node_id="HP-01"):
    return rn(
        node_id,
        "Asset",
        {
            "id": node_id,
            "name": "Press",
            "machine_type": "press",
            "vendor": "Bramwell Hydraulics",
            "model": "BH-250",
        },
    )


def line():
    return rn("LINE-01", "Line", {"id": "LINE-01", "name": "Line 1"})


def alarm(code="E-217"):
    return rn(
        code,
        "AlarmCode",
        {"code": code, "text": "Oil pressure low", "severity": "warning"},
    )


def part(part_no="SEAL-KIT-50"):
    return rn(part_no, "Part", {"part_no": part_no, "description": "Seal kit"})


def incident(number=1, **props):
    node_id = f"INC-2026-{number:03d}"
    return rn(
        node_id,
        "Incident",
        {
            "id": node_id,
            "date": "2026-08-22",
            "shift": "B",
            "downtime_min": 90,
            "symptom_text": "oil under the ram",
            **props,
        },
    )


def resolution(nodes, edges=()):
    return Resolution(nodes=list(nodes), edges=list(edges))


def rules(validated, severity=None):
    return [
        (f.rule, f.subject)
        for f in validated.flags
        if severity is None or f.severity == severity
    ]


def kept_ids(validated):
    return [n.id for n in validated.nodes]


def good_pair():
    """A press and its alarm, joined by RAISED: nothing to flag."""
    return [asset(), alarm()], [
        re_(
            "HP-01",
            "RAISED",
            "E-217",
            {
                "count": 3,
                "first_seen": "2026-08-01T00:00:00+00:00",
                "last_seen": "2026-08-22T00:00:00+00:00",
            },
        )
    ]


# --- a clean resolution -------------------------------------------------------


def test_a_clean_resolution_passes_untouched_with_no_flags():
    nodes, edges = good_pair()
    result = validate(resolution(nodes, edges))
    assert isinstance(result, Validated)
    assert kept_ids(result) == ["HP-01", "E-217"]
    assert [(e.src, e.rel, e.dst) for e in result.edges] == [
        ("HP-01", "RAISED", "E-217")
    ]
    assert result.flags == []


def test_a_flag_has_a_rule_a_severity_a_subject_and_a_detail():
    result = validate(resolution([rn("hp-01", "Asset", {})]))
    flag = result.flags[0]
    assert isinstance(flag, Flag)
    assert flag.severity == "error"
    assert flag.rule and flag.subject == "hp-01" and flag.detail


def test_the_input_resolution_is_left_as_it_was():
    nodes, edges = good_pair()
    original = resolution([*nodes, rn("bad id", "Asset", {})], edges)
    before = original.model_dump()
    validate(original)
    assert original.model_dump() == before


# --- node errors --------------------------------------------------------------


def test_an_unknown_node_type_is_dropped():
    result = validate(resolution([rn("X-1", "Gizmo", {"id": "X-1"})]))
    assert kept_ids(result) == []
    assert rules(result, "error") == [("unknown_type", "X-1")]


def test_an_id_that_fails_its_pattern_is_dropped():
    bad = rn("hp1", "Asset", asset().props | {"id": "hp1"})
    result = validate(resolution([bad]))
    assert kept_ids(result) == []
    assert rules(result, "error") == [("bad_id", "hp1")]


def test_props_that_fail_the_props_model_are_dropped():
    bad = rn(
        "HP-01", "Asset", {"id": "HP-01", "name": "Press"}
    )  # lacks required fields
    result = validate(resolution([bad]))
    assert kept_ids(result) == []
    assert rules(result, "error") == [("bad_props", "HP-01")]


def test_an_identity_prop_that_differs_from_the_node_id_is_dropped():
    bad = rn("HP-01", "Asset", asset().props | {"id": "HP-02"})
    result = validate(resolution([bad]))
    assert kept_ids(result) == []
    assert rules(result, "error") == [("identity_mismatch", "HP-01")]


def test_the_identity_prop_of_an_alarm_is_code_and_of_a_part_is_part_no():
    bad_alarm = rn("E-217", "AlarmCode", alarm().props | {"code": "E-218"})
    bad_part = rn("SEAL-KIT-50", "Part", part().props | {"part_no": "SEAL-KIT-51"})
    result = validate(resolution([bad_alarm, bad_part]))
    assert kept_ids(result) == []
    assert rules(result, "error") == [
        ("identity_mismatch", "E-217"),
        ("identity_mismatch", "SEAL-KIT-50"),
    ]


def test_a_node_with_no_evidence_is_dropped():
    result = validate(resolution([rn("LINE-01", "Line", line().props, evidence=[])]))
    assert kept_ids(result) == []
    assert rules(result, "error") == [("no_evidence", "LINE-01")]


def test_an_empty_evidence_quote_is_dropped():
    result = validate(
        resolution([rn("LINE-01", "Line", line().props, evidence=ev(""))])
    )
    assert kept_ids(result) == []
    assert rules(result, "error") == [("bad_quote", "LINE-01")]


def test_a_blank_evidence_quote_counts_as_empty():
    result = validate(
        resolution([rn("LINE-01", "Line", line().props, evidence=ev("   \n"))])
    )
    assert rules(result, "error") == [("bad_quote", "LINE-01")]


def test_an_evidence_quote_over_300_characters_is_dropped():
    long = EvidenceDraft.model_construct(
        doc_path=CSV, page=None, quote="x" * 301, extractor="csv-parser", confidence=0.9
    )
    result = validate(
        resolution([rn("LINE-01", "Line", line().props, evidence=[long])])
    )
    assert kept_ids(result) == []
    assert rules(result, "error") == [("bad_quote", "LINE-01")]


def test_a_quote_of_exactly_300_characters_passes():
    result = validate(
        resolution([rn("LINE-01", "Line", line().props, evidence=ev("x" * 300))])
    )
    assert kept_ids(result) == ["LINE-01"]
    assert rules(result, "error") == []


# --- edge errors --------------------------------------------------------------


def test_a_relation_the_ontology_refuses_for_the_endpoint_types_is_dropped():
    nodes, _ = good_pair()
    result = validate(resolution(nodes, [re_("E-217", "RAISED", "HP-01")]))
    assert result.edges == []
    assert kept_ids(result) == ["HP-01", "E-217"]
    assert ("relation_not_allowed", "E-217|RAISED|HP-01") in rules(result, "error")


def test_an_unknown_relation_is_dropped():
    nodes, _ = good_pair()
    result = validate(resolution(nodes, [re_("HP-01", "LOVES", "E-217")]))
    assert result.edges == []
    assert ("relation_not_allowed", "HP-01|LOVES|E-217") in rules(result, "error")


def test_an_edge_to_a_node_that_is_not_in_the_resolution_is_dropped():
    nodes, _ = good_pair()
    result = validate(resolution(nodes, [re_("HP-01", "RAISED", "E-999")]))
    assert result.edges == []
    assert ("missing_endpoint", "HP-01|RAISED|E-999") in rules(result, "error")


def test_edge_props_that_fail_the_relation_props_model_are_dropped():
    nodes, _ = good_pair()
    result = validate(
        resolution(nodes, [re_("HP-01", "RAISED", "E-217", {"count": 3})])
    )
    assert result.edges == []
    assert ("bad_edge_props", "HP-01|RAISED|E-217") in rules(result, "error")


def test_an_edge_with_no_evidence_is_dropped():
    nodes = [line(), asset()]
    result = validate(
        resolution(nodes, [re_("LINE-01", "HAS_MACHINE", "HP-01", evidence=[])])
    )
    assert result.edges == []
    assert ("no_evidence", "LINE-01|HAS_MACHINE|HP-01") in rules(result, "error")


def test_an_edge_with_an_empty_quote_is_dropped():
    nodes = [line(), asset()]
    result = validate(
        resolution(nodes, [re_("LINE-01", "HAS_MACHINE", "HP-01", evidence=ev(""))])
    )
    assert result.edges == []
    assert ("bad_quote", "LINE-01|HAS_MACHINE|HP-01") in rules(result, "error")


def test_a_relation_without_a_props_model_keeps_its_edge():
    result = validate(
        resolution([line(), asset()], [re_("LINE-01", "HAS_MACHINE", "HP-01")])
    )
    assert [(e.src, e.rel, e.dst) for e in result.edges] == [
        ("LINE-01", "HAS_MACHINE", "HP-01")
    ]


# --- the cascade --------------------------------------------------------------


def test_dropping_a_node_drops_its_edges_each_with_its_own_flag():
    bad_asset = rn("HP-01", "Asset", asset().props | {"id": "HP-02"})
    nodes = [line(), bad_asset, alarm(), incident(1)]
    edges = [
        re_("LINE-01", "HAS_MACHINE", "HP-01"),
        re_(
            "HP-01",
            "RAISED",
            "E-217",
            {
                "count": 1,
                "first_seen": "2026-08-01T00:00:00+00:00",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
        ),
        re_("INC-2026-001", "OCCURRED_ON", "HP-01"),
        re_("INC-2026-001", "TRIGGERED_BY", "E-217"),
    ]
    result = validate(resolution(nodes, edges))
    assert kept_ids(result) == ["LINE-01", "E-217", "INC-2026-001"]
    assert [(e.src, e.rel, e.dst) for e in result.edges] == [
        ("INC-2026-001", "TRIGGERED_BY", "E-217")
    ]
    errors = rules(result, "error")
    assert errors[0] == ("identity_mismatch", "HP-01")
    dropped = [subject for rule, subject in errors if rule == "dropped_endpoint"]
    assert dropped == [
        "LINE-01|HAS_MACHINE|HP-01",
        "HP-01|RAISED|E-217",
        "INC-2026-001|OCCURRED_ON|HP-01",
    ]


def test_every_dropped_item_has_one_error_flag_at_least():
    nodes = [rn("hp1", "Asset", {}), line()]
    edges = [re_("LINE-01", "HAS_MACHINE", "hp1")]
    result = validate(resolution(nodes, edges))
    assert len(rules(result, "error")) == 2
    assert result.nodes[0].id == "LINE-01"


# --- warnings -----------------------------------------------------------------


def test_a_node_with_no_edges_is_kept_with_a_warning():
    nodes, edges = good_pair()
    result = validate(resolution([*nodes, part()], edges))
    assert "SEAL-KIT-50" in kept_ids(result)
    assert rules(result, "warning") == [("no_edges", "SEAL-KIT-50")]
    assert rules(result, "error") == []


def test_a_node_whose_only_edges_were_dropped_gets_the_no_edges_warning():
    nodes, _ = good_pair()
    result = validate(resolution(nodes, [re_("HP-01", "RAISED", "E-217", {})]))
    assert kept_ids(result) == ["HP-01", "E-217"]
    assert sorted(rules(result, "warning")) == [
        ("no_edges", "E-217"),
        ("no_edges", "HP-01"),
    ]


def linked_incident(**props):
    nodes = [asset(), incident(1, **props)]
    return resolution(nodes, [re_("INC-2026-001", "OCCURRED_ON", "HP-01")])


def test_an_incident_with_a_machine_shift_and_downtime_has_no_warnings():
    result = validate(linked_incident())
    assert result.flags == []


def test_an_incident_with_no_occurred_on_gets_a_warning_and_is_kept():
    result = validate(resolution([incident(1)]))
    assert kept_ids(result) == ["INC-2026-001"]
    assert ("incident_no_machine", "INC-2026-001") in rules(result, "warning")


def test_an_incident_with_no_shift_gets_a_warning_and_is_kept():
    result = validate(linked_incident(shift=None))
    assert "INC-2026-001" in kept_ids(result)
    assert rules(result) == [("incident_no_shift", "INC-2026-001")]
    assert result.flags[0].severity == "warning"


def test_an_incident_with_no_downtime_gets_a_warning_and_is_kept():
    result = validate(linked_incident(downtime_min=None))
    assert "INC-2026-001" in kept_ids(result)
    assert rules(result) == [("incident_no_downtime", "INC-2026-001")]


def test_an_incident_without_the_shift_or_downtime_keys_still_validates():
    inc = incident(1)
    inc.props.pop("shift")
    inc.props.pop("downtime_min")
    result = validate(
        resolution([asset(), inc], [re_("INC-2026-001", "OCCURRED_ON", "HP-01")])
    )
    assert "INC-2026-001" in kept_ids(result)
    assert sorted(rules(result, "warning")) == [
        ("incident_no_downtime", "INC-2026-001"),
        ("incident_no_shift", "INC-2026-001"),
    ]


def test_downtime_outside_0_to_1440_gets_a_warning_and_is_kept():
    for minutes in (-1, 1441):
        result = validate(linked_incident(downtime_min=minutes))
        assert "INC-2026-001" in kept_ids(result)
        assert rules(result) == [("downtime_range", "INC-2026-001")]
    for minutes in (0, 1440):
        assert validate(linked_incident(downtime_min=minutes)).flags == []


def test_flags_come_in_node_then_edge_then_warning_order():
    nodes = [rn("hp1", "Asset", {}), part(), line()]
    edges = [re_("LINE-01", "HAS_MACHINE", "hp1")]
    result = validate(resolution(nodes, edges))
    assert [f.rule for f in result.flags] == [
        "bad_id",
        "dropped_endpoint",
        "no_edges",
        "no_edges",
    ]
    assert [f.severity for f in result.flags] == [
        "error",
        "error",
        "warning",
        "warning",
    ]
