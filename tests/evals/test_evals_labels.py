"""Tests for layer 2 labels: deciding which candidate pairs are real matches and which
cannot be told."""

from eval_stubs import incident_truth, make_truth, mentions, node, record

from app.evals.labels import label_entity, label_incident, truth_incident


def parts_truth():
    return make_truth(
        [
            node("SEAL-KIT-50", "Part", aliases=["SEAL KIT 50", "SK-50"]),
            node("SEAL-RV-25", "Part", aliases=["RV SEAL"]),
            node("OIL-HLP46-20L", "Part", aliases=[]),
            node("HP-01", "Asset", aliases=["Press 2", "the press"]),
            node("CNC-01", "Asset", aliases=["CNC1"]),
        ],
        [],
        doc_mentions={
            "handovers/a.txt": mentions(
                parts=["SEAL-KIT-50", "SEAL-RV-25"], assets=["HP-01"]
            ),
            "handovers/solo.txt": mentions(
                parts=["OIL-HLP46-20L"], assets=["HP-01", "CNC-01"]
            ),
            "handovers/none.txt": mentions(),
        },
    )


def test_a_mention_whose_key_equals_an_alias_of_the_candidate_is_a_true_pair():
    truth = parts_truth()
    assert (
        label_entity(truth, "Part", "SK-50", "handovers/a.txt", "SEAL-KIT-50") is True
    )
    assert (
        label_entity(truth, "Part", "sealkit50", "handovers/a.txt", "SEAL-KIT-50")
        is True
    )
    assert (
        label_entity(truth, "Part", "rv seal", "handovers/a.txt", "SEAL-RV-25") is True
    )


def test_a_mention_whose_key_belongs_to_another_part_is_a_false_pair():
    truth = parts_truth()
    assert (
        label_entity(truth, "Part", "SK-50", "handovers/a.txt", "SEAL-RV-25") is False
    )


def test_with_no_key_match_the_only_part_of_the_doc_decides():
    truth = parts_truth()
    assert (
        label_entity(
            truth, "Part", "HYD-OIL-20L", "handovers/solo.txt", "OIL-HLP46-20L"
        )
        is True
    )
    assert (
        label_entity(truth, "Part", "HYD-OIL-20L", "handovers/solo.txt", "SEAL-RV-25")
        is False
    )


def test_a_pair_that_neither_rule_settles_is_unlabeled():
    truth = parts_truth()
    # no key match and two parts in the doc
    assert (
        label_entity(truth, "Part", "MYSTERY", "handovers/a.txt", "SEAL-KIT-50") is None
    )
    # no key match and the doc is not in the truth
    assert (
        label_entity(truth, "Part", "MYSTERY", "handovers/unknown.txt", "SEAL-KIT-50")
        is None
    )
    assert (
        label_entity(truth, "Part", "MYSTERY", "handovers/none.txt", "SEAL-KIT-50")
        is None
    )


def test_assets_follow_the_same_rules_on_their_aliases_and_the_docs_assets():
    truth = parts_truth()
    assert label_entity(truth, "Asset", "Press 2", "handovers/a.txt", "HP-01") is True
    assert label_entity(truth, "Asset", "Press 2", "handovers/a.txt", "CNC-01") is False
    assert label_entity(truth, "Asset", "line lead", "handovers/a.txt", "HP-01") is True
    assert (
        label_entity(truth, "Asset", "line lead", "handovers/solo.txt", "HP-01") is None
    )


def test_a_record_of_a_doc_with_one_incident_is_that_incident():
    truth = incident_truth()
    assert truth_incident(truth, record("handovers/one.txt")) == "T-3"
    assert truth_incident(truth, record("handovers/zero.txt")) is None
    assert truth_incident(truth, record("handovers/missing.txt")) is None


def test_an_export_row_maps_by_machine_and_date():
    truth = incident_truth()
    row = record("maintenance/export.csv", date="2026-04-15", machine="HP-01")
    assert truth_incident(truth, row) == "T-3"


def test_the_technician_picks_between_rows_of_one_machine_and_date_by_name_or_short_form():
    truth = incident_truth()
    by_short = record(
        "maintenance/export.csv", date="2026-04-14", machine="HP-01", technician="NA"
    )
    by_name = record(
        "maintenance/export.csv",
        date="2026-04-14",
        machine="HP-01",
        technician="chen jun hao",
    )
    assert truth_incident(truth, by_short) == "T-2"
    assert truth_incident(truth, by_name) == "T-1"


def test_the_nearest_downtime_breaks_what_machine_date_and_technician_leave():
    truth = incident_truth()
    row = record(
        "maintenance/export.csv",
        date="2026-04-14",
        machine="CNC-01",
        technician="JHC",
        downtime_min=110,
    )
    # T-4 and T-5 are both Jun Hao on CNC-01 that day with 100 minutes of downtime
    assert truth_incident(truth, row) is None
    row = record(
        "maintenance/export.csv", date="2026-04-14", machine="HP-01", downtime_min=290
    )
    assert truth_incident(truth, row) == "T-2"


def test_a_row_without_the_means_to_choose_is_unmapped():
    truth = incident_truth()
    row = record("maintenance/export.csv", date="2026-04-14", machine="HP-01")
    assert truth_incident(truth, row) is None
    assert (
        truth_incident(
            truth,
            record("maintenance/export.csv", machine="NOPE-01", date="2026-04-14"),
        )
        is None
    )


def test_a_row_one_day_off_maps_when_no_incident_has_its_date():
    truth = incident_truth()
    row = record("maintenance/export.csv", date="2026-04-16", machine="HP-01")
    assert truth_incident(truth, row) == "T-3"


def test_two_records_are_the_same_incident_when_both_map_to_one_truth_incident():
    truth = incident_truth()
    note = record("handovers/one.txt")
    row = record("maintenance/export.csv", date="2026-04-15", machine="HP-01")
    other = record(
        "maintenance/export.csv", date="2026-04-14", machine="HP-01", technician="NA"
    )
    assert label_incident(truth, note, row) is True
    assert label_incident(truth, note, other) is False
    assert label_incident(truth, note, record("handovers/zero.txt")) is None
