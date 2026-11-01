"""Tests for the due-soon predictions and the example service terms."""

import datetime as dt

from app import upkeep
from app.config import KG_PATH

D = dt.date


def test_repeats_within_a_week_are_one_episode():
    dates = [
        D(2026, 4, 14),
        D(2026, 4, 19),
        D(2026, 4, 23),
        D(2026, 6, 20),
        D(2026, 6, 27),
    ]
    assert upkeep.episodes(dates) == [D(2026, 4, 14), D(2026, 6, 20)]


def test_the_due_date_is_the_last_episode_plus_the_median_gap():
    starts = [D(2026, 1, 1), D(2026, 1, 31), D(2026, 3, 2), D(2026, 4, 11)]
    got = upkeep.predict(starts, today=D(2026, 4, 20))
    assert got["gap_days"] == 30
    assert got["due"] == "2026-05-11"
    assert got["status"] == "later"


def test_status_is_overdue_or_due_soon_by_date():
    starts = [D(2026, 1, 1), D(2026, 1, 11), D(2026, 1, 21)]
    assert upkeep.predict(starts, today=D(2026, 2, 5))["status"] == "overdue"
    assert upkeep.predict(starts, today=D(2026, 1, 25))["status"] == "due_soon"


def test_fewer_than_three_episodes_give_no_prediction():
    got = upkeep.predict([D(2026, 1, 1), D(2026, 3, 1)], today=D(2026, 3, 5))
    assert got == {"status": "not_enough_history", "gap_days": None, "due": None}


def test_only_gradual_causes_are_predicted():
    assert upkeep.is_gradual("main cylinder seal wear")
    assert upkeep.is_gradual("pressure sensor drift")
    assert not upkeep.is_gradual("light curtain misalignment")


def test_the_committed_graph_gives_cited_predictions():
    data = upkeep.upkeep(KG_PATH)
    assert data["as_of"] == "2026-09-28"
    assert data["items"], "expected predictions from the committed graph"
    statuses = [item["status"] for item in data["items"]]
    order = ["overdue", "due_soon", "later", "not_enough_history"]
    assert statuses == sorted(statuses, key=order.index)
    for item in data["items"]:
        assert upkeep.is_gradual(item["cause_label"])
        assert item["breakdowns"]
        assert all(b["citations"] for b in item["breakdowns"])
    seal = next(i for i in data["items"] if i["cause"] == "FM-MAIN-CYLINDER-SEAL-WEAR")
    assert seal["machine"] == "HP-01" and len(seal["breakdowns"]) == 9


def test_terms_cover_every_machine_with_measured_uptime():
    terms = upkeep.upkeep(KG_PATH)["terms"]
    assert [t["machine"] for t in terms] == [
        "CNC-01",
        "CNC-02",
        "HP-01",
        "LT-01",
        "RW-01",
    ]
    assert all(0 < t["measured_uptime"] <= 1 for t in terms)
