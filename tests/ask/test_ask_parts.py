"""Tests of the part tools: finding the parts a question names, and looking up a part's
stock.
"""

import datetime as dt

import pytest
from crew_stubs import NO_STOCK_PART, build_crew_kg
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app import store
from app.ask import tools


@pytest.fixture
def kg(tmp_path):
    return build_crew_kg(tmp_path / "kg.sqlite")


@pytest.fixture
def conn(kg):
    connection = tools.connect_ro(kg)
    yield connection
    connection.close()


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Is SEAL-KIT-50 in stock?", ["SEAL-KIT-50"]),
        ("is seal-kit-50 in stock", ["SEAL-KIT-50"]),
        ("do we have seal kit 50 left", ["SEAL-KIT-50"]),  # a unique part number
        ("do we have SEALKIT50", ["SEAL-KIT-50"]),
        ("any SK-50 on the shelf?", ["SEAL-KIT-50"]),  # an alias
        ("OIL-HLP46-20L and SEAL-KIT-50 please", ["OIL-HLP46-20L", "SEAL-KIT-50"]),
        ("the SEAL-KIT-50, SK-50 and seal kit 50", ["SEAL-KIT-50"]),  # once
        ("HP-01 is leaking", []),
        ("we need a seal kit", []),
        ("E-217 on the press", []),
        ("", []),
    ],
)
def test_find_parts(conn, text, found):
    assert tools.find_parts(conn, text) == found


def test_a_part_key_that_two_parts_share_is_no_match(tmp_path):
    kg = build_crew_kg(tmp_path / "two.sqlite")
    db = store.connect(kg)
    for part in ("AB-1234", "A-B1234"):
        store.upsert_node(
            db, part, "Part", part, {"part_no": part, "description": part}
        )
    db.close()
    conn = tools.connect_ro(kg)
    try:
        assert tools.find_parts(conn, "do we have ab 1234") == []
        assert tools.find_parts(conn, "do we have AB-1234") == [
            "AB-1234"
        ]  # an exact match still wins
    finally:
        conn.close()


def test_part_stock_holds_the_stock_the_machines_it_fits_and_the_current_sheet_only(
    conn,
):
    stock = tools.part_stock(conn, "SEAL-KIT-50")
    assert (stock.part_no, stock.description) == (
        "SEAL-KIT-50",
        "Main cylinder seal kit",
    )
    assert (stock.qty_on_hand, stock.bin, stock.lead_time_days) == (3, "B-12", 14)
    assert stock.stock_as_of == dt.date(2026, 10, 15)
    assert stock.fits == ["HP-01"]
    assert [c.doc_path for c in stock.citations] == [
        "inventory/spares.xlsx"
    ]  # not the out-of-date sheet


def test_part_stock_with_no_stock_row_and_a_part_that_does_not_exist(conn):
    zero = tools.part_stock(conn, NO_STOCK_PART)
    assert zero.qty_on_hand == 0 and zero.bin == "D-01" and zero.fits == []
    assert tools.part_stock(conn, "NOPE-1") is None
    assert tools.part_stock(conn, " seal-kit-50 ").part_no == "SEAL-KIT-50"
