"""Tests for matching graph ids to truth ids, one to one within each type."""

from eval_stubs import edge, make_graph, make_truth, node

from app.evals.match import match


def fm(id, name):
    return node(id, "FailureMode", name=name)


def test_ids_that_are_the_same_in_both_match_by_equality():
    truth = make_truth(
        [
            node(i, t)
            for i, t in [("HP-01", "Asset"), ("E-217", "AlarmCode"), ("P-1", "Part")]
        ],
        [],
    )
    graph = make_graph(
        [node("HP-01", "Asset"), node("E-217", "AlarmCode"), node("P-2", "Part")], []
    )
    result = match(truth, graph)
    assert result.ids == {"HP-01": "HP-01", "E-217": "E-217"}
    assert result.unmatched_graph["Part"] == ["P-2"]
    assert result.unmatched_truth["Part"] == ["P-1"]


def test_an_equal_id_of_another_type_does_not_match():
    truth = make_truth([node("X-1", "Part")], [])
    graph = make_graph([node("X-1", "Procedure")], [])
    assert match(truth, graph).ids == {}


def test_failure_modes_match_by_their_anchors_where_the_names_differ():
    anchors = [
        node("PROC-1", "Procedure"),
        node("PROC-2", "Procedure"),
        node("E-1", "AlarmCode"),
        node("E-2", "AlarmCode"),
    ]
    truth = make_truth(
        [
            *anchors,
            fm("FM-SEAL-WEAR", "Cylinder seal wear"),
            fm("FM-SENSOR", "Sensor drift"),
        ],
        [
            edge("FM-SEAL-WEAR", "RESOLVED_BY", "PROC-1"),
            edge("E-1", "INDICATES", "FM-SEAL-WEAR"),
            edge("FM-SENSOR", "RESOLVED_BY", "PROC-2"),
            edge("E-2", "INDICATES", "FM-SENSOR"),
        ],
    )
    graph = make_graph(
        [
            *anchors,
            fm("FM-A", "totally different words"),
            fm("FM-B", "other words again"),
        ],
        [
            edge("FM-A", "RESOLVED_BY", "PROC-2"),
            edge("E-2", "INDICATES", "FM-A"),
            edge("FM-B", "RESOLVED_BY", "PROC-1"),
            edge("E-1", "INDICATES", "FM-B"),
        ],
    )
    result = match(truth, graph)
    assert result.ids["FM-A"] == "FM-SENSOR"
    assert result.ids["FM-B"] == "FM-SEAL-WEAR"


def test_failure_modes_with_no_anchor_overlap_stay_unmatched_even_with_equal_names():
    truth = make_truth(
        [node("PROC-1", "Procedure"), fm("FM-T", "Spindle bearing wear")],
        [edge("FM-T", "RESOLVED_BY", "PROC-1")],
    )
    graph = make_graph(
        [node("PROC-1", "Procedure"), fm("FM-G", "Spindle bearing wear")], []
    )
    result = match(truth, graph)
    assert "FM-G" not in result.ids
    assert result.unmatched_graph["FailureMode"] == ["FM-G"]
    assert result.unmatched_truth["FailureMode"] == ["FM-T"]


def test_a_name_breaks_a_tie_between_equal_anchor_overlaps():
    anchors = [node("PROC-1", "Procedure")]
    truth = make_truth(
        [
            *anchors,
            fm("FM-SEAL", "Cylinder seal wear"),
            fm("FM-FILTER", "Coolant filter blocked"),
        ],
        [
            edge("FM-SEAL", "RESOLVED_BY", "PROC-1"),
            edge("FM-FILTER", "RESOLVED_BY", "PROC-1"),
        ],
    )
    graph = make_graph(
        [
            *anchors,
            fm("FM-G1", "coolant filter blockage"),
            fm("FM-G2", "seal wear of the cylinder"),
        ],
        [
            edge("FM-G1", "RESOLVED_BY", "PROC-1"),
            edge("FM-G2", "RESOLVED_BY", "PROC-1"),
        ],
    )
    result = match(truth, graph)
    assert result.ids["FM-G1"] == "FM-FILTER"
    assert result.ids["FM-G2"] == "FM-SEAL"


def component(id, asset, name):
    return node(id, "Component", asset_id=asset, name=name)


def test_components_match_by_name_within_the_same_asset_only():
    truth = make_truth(
        [
            component("T-SPINDLE", "CNC-01", "Spindle"),
            component("T-COOLANT", "CNC-01", "Coolant system"),
            component("T-SPINDLE-2", "CNC-02", "Spindle"),
            component("T-LONE", "CNC-01", "Light curtain"),
        ],
        [],
    )
    graph = make_graph(
        [
            component("G-COOLANT", "CNC-01", "coolant system"),
            component("G-SPINDLE-2", "CNC-02", "Spindle unit"),
            component("G-SPINDLE", "CNC-01", "Spindle"),
            component("G-OTHER", "CNC-02", "Coolant system"),
        ],
        [],
    )
    result = match(truth, graph)
    assert result.ids == {
        "G-COOLANT": "T-COOLANT",
        "G-SPINDLE-2": "T-SPINDLE-2",
        "G-SPINDLE": "T-SPINDLE",
    }
    assert result.unmatched_graph["Component"] == ["G-OTHER"]
    assert result.unmatched_truth["Component"] == ["T-LONE"]


def test_components_below_the_name_score_stay_unmatched():
    truth = make_truth([component("T-1", "CNC-01", "Spindle")], [])
    graph = make_graph([component("G-1", "CNC-01", "Hydraulic pump")], [])
    assert match(truth, graph).ids == {}


def incident(id, date, downtime, symptom):
    return node(id, "Incident", date=date, downtime_min=downtime, symptom_text=symptom)


def test_incidents_match_by_machine_and_date_with_adjacent_days_as_a_trap():
    nodes_t = [
        node("HP-01", "Asset"),
        incident("T-003", "2026-04-18", 215, "pressure hunting, oil running hot"),
        incident("T-004", "2026-04-19", 230, "press losing pressure mid-stroke"),
    ]
    edges_t = [
        edge("T-003", "OCCURRED_ON", "HP-01"),
        edge("T-004", "OCCURRED_ON", "HP-01"),
    ]
    nodes_g = [
        node("HP-01", "Asset"),
        incident("G-1", "2026-04-19", 240, "press losing pressure mid-stroke"),
        incident("G-2", "2026-04-18", 215, "pressure hunting, oil running hot"),
    ]
    edges_g = [edge("G-1", "OCCURRED_ON", "HP-01"), edge("G-2", "OCCURRED_ON", "HP-01")]
    result = match(make_truth(nodes_t, edges_t), make_graph(nodes_g, edges_g))
    assert result.ids["G-1"] == "T-004"
    assert result.ids["G-2"] == "T-003"


def test_equal_symptoms_are_told_apart_by_downtime():
    same = "press losing pressure mid-stroke, oil on the floor"
    truth = make_truth(
        [
            node("HP-01", "Asset"),
            incident("T-005", "2026-04-22", 455, same),
            incident("T-007", "2026-04-22", 525, same),
        ],
        [edge("T-005", "OCCURRED_ON", "HP-01"), edge("T-007", "OCCURRED_ON", "HP-01")],
    )
    graph = make_graph(
        [
            node("HP-01", "Asset"),
            incident("G-5", "2026-04-22", 525, same),
            incident("G-6", "2026-04-22", 455, same),
        ],
        [edge("G-5", "OCCURRED_ON", "HP-01"), edge("G-6", "OCCURRED_ON", "HP-01")],
    )
    result = match(truth, graph)
    assert result.ids["G-5"] == "T-007"
    assert result.ids["G-6"] == "T-005"


def test_incidents_on_another_machine_or_two_days_apart_are_unmatched_on_both_sides():
    truth = make_truth(
        [
            node("HP-01", "Asset"),
            node("CNC-01", "Asset"),
            incident("T-1", "2026-04-18", 100, "a"),
            incident("T-2", "2026-05-01", 100, "b"),
        ],
        [edge("T-1", "OCCURRED_ON", "HP-01"), edge("T-2", "OCCURRED_ON", "HP-01")],
    )
    graph = make_graph(
        [
            node("HP-01", "Asset"),
            node("CNC-01", "Asset"),
            incident("G-1", "2026-04-18", 100, "a"),
            incident("G-2", "2026-05-03", 100, "b"),
            incident("G-3", None, 100, "c"),
        ],
        [
            edge("G-1", "OCCURRED_ON", "CNC-01"),
            edge("G-2", "OCCURRED_ON", "HP-01"),
            edge("G-3", "OCCURRED_ON", "HP-01"),
        ],
    )
    result = match(truth, graph)
    assert not any(i.startswith("G-") for i in result.ids)
    assert result.unmatched_graph["Incident"] == ["G-1", "G-2", "G-3"]
    assert result.unmatched_truth["Incident"] == ["T-1", "T-2"]


def test_a_missing_downtime_still_matches_on_the_symptom():
    truth = make_truth(
        [
            node("HP-01", "Asset"),
            incident("T-1", "2026-04-18", 215, "pressure hunting"),
        ],
        [edge("T-1", "OCCURRED_ON", "HP-01")],
    )
    graph = make_graph(
        [
            node("HP-01", "Asset"),
            incident("G-1", "2026-04-18", None, "pressure hunting"),
        ],
        [edge("G-1", "OCCURRED_ON", "HP-01")],
    )
    assert match(truth, graph).truth_id("G-1") == "T-1"


def test_edges_map_through_their_endpoints_and_an_unmatched_endpoint_gives_none():
    truth = make_truth([node("P-1", "Part"), component("T-C", "CNC-01", "Spindle")], [])
    graph = make_graph(
        [
            node("P-1", "Part"),
            component("G-C", "CNC-01", "Spindle"),
            node("P-9", "Part"),
        ],
        [],
    )
    result = match(truth, graph)
    assert result.map_edge(edge("P-1", "FITS", "G-C")) == ("P-1", "FITS", "T-C")
    assert result.map_edge(edge("P-9", "FITS", "G-C")) is None
    assert result.graph_id("T-C") == "G-C"
    assert result.truth_id("G-C") == "T-C"
