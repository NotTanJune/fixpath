"""Tests of the graph tools the agents use to look things up, on the small test graph.
Each section covers one tool, such as finding a machine, listing the causes of an alarm,
or finding similar past breakdowns.
"""

import datetime as dt
import sqlite3

import pytest
from ask_stubs import HP_MANUAL, _evidence, build_kg, open_kg
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app import store
from app.ask import tools
from app.ask.types import PathStep


@pytest.fixture
def conn(tmp_path):
    connection = open_kg(tmp_path)
    yield connection
    connection.close()


# --- read-only connection ---


def test_connect_ro_cannot_write(conn):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM nodes")
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0


def test_connect_ro_does_not_create_a_missing_database(tmp_path):
    missing = tmp_path / "none.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        tools.connect_ro(missing)
    assert not missing.exists()


def test_connect_ro_picks_up_a_database_that_was_swapped(tmp_path):
    first = tools.connect_ro(build_kg(tmp_path / "kg.sqlite"))
    first.close()
    (tmp_path / "kg.sqlite").unlink()
    with pytest.raises(sqlite3.OperationalError):
        tools.connect_ro(tmp_path / "kg.sqlite")
    again = tools.connect_ro(build_kg(tmp_path / "kg.sqlite"))
    assert again.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0
    again.close()


# --- find_machine ---


def test_find_machine_matches_an_id_a_name_or_an_alias_exactly_ignoring_case(conn):
    for text in ("hp-01", "HP-01", "Hydraulic Press", "the press", " Press 2 "):
        found = tools.find_machine(conn, text)
        assert [machine["id"] for machine in found] == ["HP-01"], text
        assert found[0]["score"] == 100


def test_find_machine_falls_back_to_a_fuzzy_match_and_lists_the_best_first(conn):
    found = tools.find_machine(conn, "hydrulic press")
    assert [machine["id"] for machine in found] == ["HP-01"]
    assert 80 <= found[0]["score"] < 100
    assert found[0]["name"] == "Hydraulic press"
    assert found[0]["model"] == "BH-250"


@pytest.mark.parametrize("text", ["HP-02", "hp-02", "HP02", "CNC-03", "XYZ-9", "LT-02"])
def test_find_machine_gives_no_fuzzy_hit_for_text_shaped_like_an_asset_id(conn, text):
    # HP-02 is one character from HP-01, but a machine id is not a typo to fix
    assert tools.find_machine(conn, text) == []


def test_find_machine_still_matches_an_asset_id_that_exists(conn):
    assert [m["id"] for m in tools.find_machine(conn, "cnc-02")] == ["CNC-02"]
    assert [m["id"] for m in tools.find_machine(conn, " HP-01 ")] == ["HP-01"]


def test_find_machine_stays_fuzzy_for_a_name_that_is_not_shaped_like_an_id(conn):
    found = tools.find_machine(conn, "Press 3")  # close to the nickname "Press 2"
    assert [m["id"] for m in found] == ["HP-01"]
    assert 80 <= found[0]["score"] < 100


def test_find_machine_matches_a_model_and_lists_every_asset_of_it(conn):
    found = tools.find_machine(conn, "kx-500")
    assert [(m["id"], m["score"], m["matched"], m["model"]) for m in found] == [
        ("CNC-01", 100.0, "model", "KX-500"),
        ("CNC-02", 100.0, "model", "KX-500"),
    ]
    assert [m["id"] for m in tools.find_machine(conn, "BH-250")] == ["HP-01"]


def test_find_machine_matches_a_vendor_name_and_lists_its_assets(conn):
    found = tools.find_machine(conn, "kessler werkzeugmaschinen")
    assert [(m["id"], m["score"], m["matched"]) for m in found] == [
        ("CNC-01", 100.0, "vendor"),
        ("CNC-02", 100.0, "vendor"),
    ]


def test_find_machine_returns_only_exact_matches_when_there_are_some(conn):
    assert [m["id"] for m in tools.find_machine(conn, "lathe 1")] == ["CNC-01"]


def test_find_machine_returns_nothing_below_the_cutoff(conn):
    assert tools.find_machine(conn, "forklift") == []
    assert tools.find_machine(conn, "   ") == []


# --- alarm_causes ---


def test_alarm_causes_keeps_only_failure_modes_of_that_machine(conn):
    ids = [c.failure_mode_id for c in tools.alarm_causes(conn, "HP-01", "E-217")]
    assert "FM-SPINDLE-WEAR" not in ids
    assert [c.failure_mode_id for c in tools.alarm_causes(conn, "CNC-01", "E-217")] == [
        "FM-SPINDLE-WEAR"
    ]


def test_alarm_causes_orders_by_support_then_likelihood_then_id(conn):
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    # across the plant the pump has the most support (6), but on this machine it was
    # never the cause: 2 seal wear incidents, 1 relief valve, 0 pump
    assert [(c.failure_mode_id, c.support, c.likelihood) for c in causes] == [
        ("FM-SEAL-WEAR", 2, 0.7),
        ("FM-RELIEF-MISADJUSTED", 1, 0.95),
        ("FM-PUMP-WEAR", 0, 0.4),
    ]


def test_support_is_counted_on_this_machine_and_the_pooled_count_is_kept(conn):
    causes = {c.failure_mode_id: c for c in tools.alarm_causes(conn, "HP-01", "E-217")}
    assert {m: (c.support, c.of_total, c.support_model) for m, c in causes.items()} == {
        "FM-SEAL-WEAR": (2, 3, 6),
        "FM-RELIEF-MISADJUSTED": (1, 3, 1),
        "FM-PUMP-WEAR": (0, 3, 6),
    }
    assert {c.asset_id for c in causes.values()} == {"HP-01"}


def test_two_machines_of_one_model_do_not_share_their_history(conn):
    # the graph counts the spindle wear support (9) across both lathes
    (cnc1,) = tools.alarm_causes(conn, "CNC-01", "E-217")
    (cnc2,) = tools.alarm_causes(conn, "CNC-02", "E-217")
    assert (cnc1.asset_id, cnc1.support, cnc1.of_total, cnc1.support_model) == (
        "CNC-01",
        0,
        0,
        9,
    )
    assert (cnc2.asset_id, cnc2.support, cnc2.of_total, cnc2.support_model) == (
        "CNC-02",
        1,
        1,
        9,
    )


def test_support_never_exceeds_the_incidents_with_the_alarm(conn):
    for asset in ("HP-01", "CNC-01", "CNC-02"):
        for code in ("E-217", "E-214", "E-221"):
            for cause in tools.alarm_causes(conn, asset, code):
                assert cause.support <= cause.of_total, (asset, code, cause)


def test_alarm_causes_counts_the_incidents_that_started_with_the_alarm(conn):
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    assert {c.of_total for c in causes} == {3}
    assert causes[0].name == "main cylinder seal wear"


def test_alarm_causes_cites_the_evidence_with_manual_chunks_first(conn):
    top = tools.alarm_causes(conn, "HP-01", "E-217")[0]
    assert [(c.doc_path, c.page, c.chunk_id, c.quote) for c in top.citations] == [
        ("manuals/hp.pdf", 3, "HP-3", "Check the seals"),
        ("notes/note.txt", None, None, "note says seals"),
    ]


def test_citations_put_manual_documents_first_whatever_the_chunk(conn):
    relief = [
        c
        for c in tools.alarm_causes(conn, "HP-01", "E-217")
        if c.failure_mode_id == "FM-RELIEF-MISADJUSTED"
    ][0]
    # the note quote has a chunk id and the manual quote has none, so the document type decides
    assert [(c.doc_path, c.chunk_id, c.quote) for c in relief.citations] == [
        ("manuals/hp.pdf", None, "relief valve table"),
        ("notes/note.txt", "NOTE-1", "note says relief"),
    ]


def test_alarm_causes_records_the_edges_it_read(conn):
    top = tools.alarm_causes(conn, "HP-01", "E-217")[0]
    assert top.path == [
        PathStep(src="E-217", rel="INDICATES", dst="FM-SEAL-WEAR"),
        PathStep(src="FM-SEAL-WEAR", rel="AFFECTS", dst="HP-01-MAINCYL"),
        PathStep(src="HP-01", rel="HAS_COMPONENT", dst="HP-01-MAINCYL"),
    ]


def test_alarm_causes_is_empty_for_an_unknown_machine_or_alarm(conn):
    assert tools.alarm_causes(conn, "XX-99", "E-217") == []
    assert tools.alarm_causes(conn, "HP-01", "E-999") == []
    assert tools.alarm_causes(conn, "hp-01", " e-217 ")  # ids are tidied before the lookup


# --- fixes_for ---


def test_fixes_for_returns_the_resolving_procedures(conn):
    (fix,) = tools.fixes_for(conn, "FM-SEAL-WEAR")
    assert (fix.procedure_id, fix.title, fix.requires_loto, fix.est_minutes) == (
        "PROC-HP-04",
        "Replace main cylinder seal kit",
        True,
        180,
    )
    assert fix.steps == ["Stop the pump.", "Do the work."]
    assert fix.manual_ref == "4.1"
    assert fix.path == [
        PathStep(src="FM-SEAL-WEAR", rel="RESOLVED_BY", dst="PROC-HP-04")
    ]
    assert [(c.page, c.quote) for c in fix.citations] == [
        (5, "Replace main cylinder seal kit"),
        (5, "seal kit"),
    ]
    assert tools.fixes_for(conn, "FM-NOPE") == []


def test_fixes_for_and_parts_for_upper_case_their_ids_like_the_other_tools(conn):
    assert tools.fixes_for(conn, " fm-seal-wear ") == tools.fixes_for(
        conn, "FM-SEAL-WEAR"
    )
    assert tools.fixes_for(conn, "fm-seal-wear")[0].path[0].src == "FM-SEAL-WEAR"
    assert tools.parts_for(conn, " proc-hp-04 ") == tools.parts_for(conn, "PROC-HP-04")
    assert tools.parts_for(conn, "proc-hp-04")[0].path[0].src == "PROC-HP-04"


# --- parts_for ---


def test_parts_for_joins_the_requirement_with_the_stock(conn):
    parts = {p.part_no: p for p in tools.parts_for(conn, "PROC-HP-04")}
    assert list(parts) == ["OIL-HLP46-20L", "SEAL-KIT-50", "WIPER-CYL-50"]
    seal = parts["SEAL-KIT-50"]
    assert (seal.qty, seal.qty_on_hand, seal.bin, seal.lead_time_days) == (
        1,
        3,
        "B-12",
        14,
    )
    assert seal.stock_as_of == dt.date(2026, 10, 15)
    assert seal.description == "Main cylinder seal kit"


def test_enough_compares_the_stock_with_the_quantity_and_is_false_when_unknown(conn):
    parts = {p.part_no: p.enough for p in tools.parts_for(conn, "PROC-HP-04")}
    assert parts == {"OIL-HLP46-20L": False, "SEAL-KIT-50": True, "WIPER-CYL-50": False}
    wiper = [
        p for p in tools.parts_for(conn, "PROC-HP-04") if p.part_no == "WIPER-CYL-50"
    ][0]
    assert wiper.qty_on_hand is None and wiper.bin is None


def test_parts_for_cites_the_requirement_and_the_current_stock_sheet_only(conn):
    seal = [
        p for p in tools.parts_for(conn, "PROC-HP-04") if p.part_no == "SEAL-KIT-50"
    ][0]
    # the old spares file holds the same bin (B12) with an old count, so it is not cited
    assert [(c.doc_path, c.quote) for c in seal.citations] == [
        ("manuals/hp.pdf", "SEAL-KIT-50"),
        (
            "inventory/spares.xlsx",
            "SEAL-KIT-50 | Main cylinder seal kit | 3 | pc | B12 | 14",
        ),
    ]
    assert seal.path == [PathStep(src="PROC-HP-04", rel="REQUIRES", dst="SEAL-KIT-50")]


def test_parts_for_a_procedure_with_no_parts_is_empty(conn):
    assert tools.parts_for(conn, "PROC-HP-06") == []


# --- similar_breakdowns ---


def ids(breakdowns):
    return [b.incident_id for b in breakdowns]


def test_similar_breakdowns_scores_alarm_and_root_cause_and_puts_newer_first_on_a_tie(
    conn,
):
    found = tools.similar_breakdowns(
        conn, "HP-01", code="E-217", failure_mode_id="FM-SEAL-WEAR"
    )
    # incidents 002 and 001 match both, 004 matches the cause, 003 the alarm, and 005 neither
    assert ids(found) == [
        "INC-2026-002",
        "INC-2026-001",
        "INC-2026-004",
        "INC-2026-003",
    ]


def test_similar_breakdowns_only_looks_at_the_machine(conn):
    assert "INC-2026-006" not in ids(tools.similar_breakdowns(conn, "HP-01"))
    assert ids(tools.similar_breakdowns(conn, "CNC-01")) == ["INC-2026-006"]


def test_similar_breakdowns_with_no_criteria_lists_the_newest_first(conn):
    assert ids(tools.similar_breakdowns(conn, "HP-01")) == [
        "INC-2026-005",
        "INC-2026-004",
        "INC-2026-003",
        "INC-2026-002",
        "INC-2026-001",
    ]


def test_similar_breakdowns_adds_symptom_similarity_to_the_score(conn):
    found = tools.similar_breakdowns(conn, "HP-01", symptom="oil on the floor")
    # incident 001 holds every word, 004 shares "oil", and the others are too different to count
    assert ids(found) == ["INC-2026-001", "INC-2026-004"]
    both = tools.similar_breakdowns(
        conn, "HP-01", code="E-217", symptom="oil on the floor"
    )
    # incident 001 scores 2.0; 002 and 003 score 1 on the alarm alone and 003 is newer;
    # 004 has only the symptom (0.61)
    assert ids(both) == ["INC-2026-001", "INC-2026-003", "INC-2026-002", "INC-2026-004"]


def test_similar_breakdowns_respects_the_limit(conn):
    assert len(tools.similar_breakdowns(conn, "HP-01", limit=2)) == 2
    assert tools.similar_breakdowns(conn, "HP-01", limit=0) == []


def test_a_breakdown_carries_who_fixed_it_and_what_it_used(conn):
    (first,) = tools.similar_breakdowns(conn, "HP-01", symptom="mid-stroke", limit=1)
    assert first.incident_id == "INC-2026-001"
    assert (first.date, first.shift, first.downtime_min) == (
        dt.date(2026, 4, 19),
        "C",
        240,
    )
    assert (first.technician, first.resolution_text) == (
        "R. Tan",
        "Replaced main cylinder seal kit",
    )
    assert first.root_cause_id == "FM-SEAL-WEAR"
    assert first.parts_used == {"OIL-HLP46-20L": 1, "SEAL-KIT-50": 1}
    assert [(c.doc_path, c.quote) for c in first.citations] == [
        ("notes/note.txt", "note about INC-2026-001")
    ]
    assert PathStep(src="INC-2026-001", rel="TRIGGERED_BY", dst="E-217") in first.path
    assert (
        PathStep(src="INC-2026-001", rel="ROOT_CAUSE", dst="FM-SEAL-WEAR") in first.path
    )
    assert PathStep(src="INC-2026-001", rel="OCCURRED_ON", dst="HP-01") in first.path
    assert PathStep(src="INC-2026-001", rel="USED", dst="SEAL-KIT-50") in first.path


def test_a_breakdown_may_lack_a_shift_a_downtime_and_a_technician(conn):
    found = {b.incident_id: b for b in tools.similar_breakdowns(conn, "HP-01")}
    third = found["INC-2026-003"]
    assert (
        third.shift,
        third.downtime_min,
        third.technician,
        third.resolution_text,
    ) == (
        None,
        None,
        None,
        None,
    )
    assert found["INC-2026-004"].root_cause_id == "FM-SEAL-WEAR"
    assert not any(s.rel == "TRIGGERED_BY" for s in found["INC-2026-004"].path)


# --- alarm_history ---


def test_alarm_history_reads_the_raised_edge_and_counts_the_incidents(conn):
    history = tools.alarm_history(conn, "HP-01", "E-217")
    assert (history.asset_id, history.code, history.count, history.incidents) == (
        "HP-01",
        "E-217",
        15,
        3,
    )
    assert history.first_seen == dt.datetime(2026, 4, 18, 17, 19, tzinfo=dt.UTC)
    assert history.last_seen == dt.datetime(2026, 9, 28, 9, 51, 40, tzinfo=dt.UTC)
    assert [(c.doc_path, c.quote) for c in history.citations] == [
        ("logs/hp.log", "ERR 217")
    ]
    assert history.path == [PathStep(src="HP-01", rel="RAISED", dst="E-217")]


def test_alarm_history_of_an_alarm_the_machine_never_raised(conn):
    history = tools.alarm_history(conn, "HP-01", "E-221")
    assert (history.count, history.first_seen, history.last_seen) == (0, None, None)
    assert history.incidents == 1
    assert history.path == [] and history.citations == []


def test_the_tools_never_write(conn):
    before = conn.execute("PRAGMA data_version").fetchone()[0]
    tools.alarm_causes(conn, "HP-01", "E-217")
    tools.similar_breakdowns(conn, "HP-01", code="E-217")
    assert conn.execute("PRAGMA data_version").fetchone()[0] == before


# --- cause_on ---


def test_cause_on_reads_a_failure_mode_that_affects_a_component_of_the_machine(conn):
    cause = tools.cause_on(conn, "HP-01", "FM-SEAL-WEAR")
    assert cause.asset_id == "HP-01" and cause.failure_mode_id == "FM-SEAL-WEAR"
    assert cause.name == "main cylinder seal wear"
    # the machine's incidents: three had this cause, of five in all
    assert (cause.support, cause.of_total) == (3, 5)
    assert cause.likelihood == pytest.approx(3 / 5)
    assert cause.support_model == 0
    assert cause.path == [
        PathStep(src="FM-SEAL-WEAR", rel="AFFECTS", dst="HP-01-MAINCYL"),
        PathStep(src="HP-01", rel="HAS_COMPONENT", dst="HP-01-MAINCYL"),
    ]


def test_cause_on_takes_ids_in_any_case(conn):
    assert tools.cause_on(conn, " hp-01 ", "fm-seal-wear") == tools.cause_on(
        conn, "HP-01", "FM-SEAL-WEAR"
    )


def test_cause_on_is_none_when_the_mode_affects_no_component_of_the_machine(conn):
    assert tools.cause_on(conn, "HP-01", "FM-SPINDLE-WEAR") is None
    assert tools.cause_on(conn, "CNC-01", "FM-SEAL-WEAR") is None
    assert tools.cause_on(conn, "HP-01", "FM-NOPE") is None
    assert tools.cause_on(conn, "XX-99", "FM-SEAL-WEAR") is None


def test_cause_on_counts_the_machines_own_incidents_only(conn):
    # the spindle failure belongs to the lathe manual, so both lathes have it
    one = tools.cause_on(conn, "CNC-01", "FM-SPINDLE-WEAR")
    two = tools.cause_on(conn, "CNC-02", "FM-SPINDLE-WEAR")
    assert (one.support, one.of_total) == (1, 1)
    assert (two.support, two.of_total) == (1, 1)
    assert one.path[0] == PathStep(
        src="FM-SPINDLE-WEAR", rel="AFFECTS", dst="CNC-01-SPINDLE"
    )


def test_cause_on_a_machine_with_no_incidents_has_zero_likelihood(tmp_path):
    path = build_kg(tmp_path / "kg.sqlite")
    rw = store.connect(path)
    asset = {"id": "RW-09", "name": "Robot", "machine_type": "cell", "vendor": "V"}
    asset["model"] = "M"
    store.upsert_node(rw, "RW-09", "Asset", "Robot", asset)
    arm = {"id": "RW-09-ARM", "name": "Arm", "asset_id": "RW-09"}
    store.upsert_node(rw, "RW-09-ARM", "Component", "Arm", arm)
    store.upsert_edge(rw, "RW-09", "HAS_COMPONENT", "RW-09-ARM")
    store.upsert_edge(rw, "FM-SEAL-WEAR", "AFFECTS", "RW-09-ARM")
    rw.close()
    conn = tools.connect_ro(path)
    try:
        cause = tools.cause_on(conn, "RW-09", "FM-SEAL-WEAR")
    finally:
        conn.close()
    assert (cause.support, cause.of_total, cause.likelihood) == (0, 0, 0.0)


def test_cause_on_cites_the_evidence_of_the_affects_edge_and_the_failure_mode(tmp_path):
    path = build_kg(tmp_path / "kg.sqlite")
    rw = store.connect(path)
    edge = "FM-SEAL-WEAR|AFFECTS|HP-01-MAINCYL"
    _evidence(rw, "edge", edge, HP_MANUAL, "HP-3", 3, "seals wear at the ram")
    _evidence(rw, "node", "FM-SEAL-WEAR", HP_MANUAL, "HP-5", 5, "seal wear is common")
    rw.close()
    conn = tools.connect_ro(path)
    try:
        cause = tools.cause_on(conn, "HP-01", "FM-SEAL-WEAR")
    finally:
        conn.close()
    quotes = [c.quote for c in cause.citations]
    assert quotes == ["seals wear at the ram", "seal wear is common"]
    assert cause.citations[0].chunk_id == "HP-3" and cause.citations[0].page == 3
