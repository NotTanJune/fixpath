"""Smoke test of the graph tools on the committed knowledge graph. It opens the graph
read-only and calls only the tools, with no model and no embedder. The checks are the
facts the graph is known to hold for alarm E-217 on the hydraulic press.
"""

import sqlite3

import pytest

from app.ask import tools

SEAL_WEAR = "FM-MAIN-CYLINDER-SEAL-WEAR"
RELIEF = "FM-RELIEF-VALVE-MISADJUSTMENT"


@pytest.fixture(scope="module")
def conn():
    connection = tools.connect_ro()
    yield connection
    connection.close()


def test_the_committed_graph_is_opened_read_only(conn):
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM nodes")


def test_e217_on_the_press_points_to_seal_wear_first_and_the_relief_valve_second(conn):
    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    assert [c.failure_mode_id for c in causes] == [SEAL_WEAR, RELIEF]
    assert (causes[0].support, causes[0].of_total, causes[0].likelihood) == (6, 6, 0.7)
    assert (causes[1].support, causes[1].likelihood) == (0, 0.3)
    assert causes[0].citations and causes[0].citations[0].page == 3
    assert {c.asset_id for c in causes} == {"HP-01"}


def all_codes(conn):
    return [
        row["id"]
        for row in conn.execute("SELECT id FROM nodes WHERE type = 'AlarmCode'")
    ]


def all_assets(conn):
    return [
        row["id"] for row in conn.execute("SELECT id FROM nodes WHERE type = 'Asset'")
    ]


def test_support_never_exceeds_the_incidents_with_the_alarm_on_any_machine(conn):
    checked = 0
    for asset in all_assets(conn):
        for code in all_codes(conn):
            for cause in tools.alarm_causes(conn, asset, code):
                assert cause.support <= cause.of_total, (asset, code, cause)
                assert cause.asset_id == asset
                checked += 1
    assert checked >= 10


def test_a_lathe_reports_its_own_count_and_keeps_the_pooled_one(conn):
    (cause,) = [
        c
        for c in tools.alarm_causes(conn, "CNC-01", "SP-104")
        if c.failure_mode_id == "FM-SPINDLE-BEARING-WEAR"
    ]
    assert 0 < cause.support <= cause.of_total
    assert cause.support_model == 7  # counted across CNC-01 and CNC-02
    other = [
        c
        for c in tools.alarm_causes(conn, "CNC-02", "SP-104")
        if c.failure_mode_id == "FM-SPINDLE-BEARING-WEAR"
    ][0]
    assert cause.support + other.support <= cause.support_model


def test_a_near_id_is_not_a_machine_and_a_model_names_all_its_machines(conn):
    for text in ("HP-02", "LT-02", "RW-02"):
        assert tools.find_machine(conn, text) == [], text
    assert [m["id"] for m in tools.find_machine(conn, "KX-500")] == ["CNC-01", "CNC-02"]
    assert [m["id"] for m in tools.find_machine(conn, "BH-250")] == ["HP-01"]
    assert [m["id"] for m in tools.find_machine(conn, "HP-01")] == ["HP-01"]


def test_the_seal_wear_fixes_include_the_seal_kit_job(conn):
    fixes = {f.procedure_id: f for f in tools.fixes_for(conn, SEAL_WEAR)}
    assert "PROC-HP-04" in fixes
    assert fixes["PROC-HP-04"].requires_loto and fixes["PROC-HP-04"].steps
    assert [f.procedure_id for f in tools.fixes_for(conn, RELIEF)] == ["PROC-HP-06"]


def test_the_seal_kit_job_needs_a_seal_kit_with_a_bin_and_stock(conn):
    parts = {p.part_no: p for p in tools.parts_for(conn, "PROC-HP-04")}
    assert {"SEAL-KIT-50", "OIL-HLP46-20L"} <= set(parts)
    kit = parts["SEAL-KIT-50"]
    assert kit.bin and kit.qty_on_hand is not None and kit.lead_time_days is not None
    assert kit.enough == (kit.qty_on_hand >= kit.qty)
    assert kit.stock_as_of is not None


def test_the_parts_cite_the_current_stock_sheet_and_not_the_old_one(conn):
    parts = tools.parts_for(conn, "PROC-HP-04")
    cited = {
        c.doc_path for p in parts for c in p.citations if "inventory/" in c.doc_path
    }
    assert cited and not any("spares_old" in path for path in cited)
    for part in parts:
        for citation in part.citations:
            if "inventory/" in citation.doc_path:
                assert str(part.qty_on_hand) in citation.quote


def test_past_e217_breakdowns_name_a_technician_and_a_resolution(conn):
    found = tools.similar_breakdowns(conn, "HP-01", code="E-217")
    assert 1 <= len(found) <= 5
    assert all(b.resolution_text for b in found)
    assert any(b.technician for b in found)
    assert all(b.root_cause_id for b in found)
