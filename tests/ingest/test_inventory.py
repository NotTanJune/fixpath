"""Tests for reading the spares inventory and the alias table.

They use the real workbook and the old stock list, plus small hand-written files for
odd cases. No test calls a model; a call would fail the test. Column header mapping
is tested in test_headers.py.
"""

import re
from pathlib import Path

import pytest
from ingest_stubs import (
    OLD_COUNT,
    SPARES_HEADERS,
    UNKNOWN_MACHINES,
    UNKNOWN_SPARES,
    WORKBOOK,
    count_by_relation,
    count_by_type,
    fake_model,  # noqa: F401, F811  (a fixture)
    machine_rows,
    machines_answer,
    no_model,  # noqa: F401  (an autouse fixture)
    nodes_of,
    refuse,
    spares_answer,
    spares_rows,
    write_workbook,
)

from app import llm
from app.ingest import parsers
from app.ingest.headers import HeaderChoice, HeaderMap
from app.ingest.normalize import AliasTable, MachineEntry, part_key
from app.ingest.types import Extraction


# --- The alias table ------------------------------------------------------------


def test_the_alias_table_resolves_every_tag_alias_and_serial_of_the_machines_sheet():
    table = parsers.build_alias_table(WORKBOOK)
    rows = machine_rows()
    assert len(rows) == 5
    assert table.tags == [row[0] for row in rows]
    for tag, _, _, _, serial, _, also_called in rows:
        assert table.asset_for(tag) == tag
        assert table.asset_for(serial) == tag
        names = [name.strip() for name in also_called.split(",")]
        assert len(names) >= 4
        for name in names:
            assert table.asset_for(name) == tag
            assert table.asset_for(name.upper()) == tag
            assert table.asset_for(name.lower()) == tag
        assert table.aliases(tag) == names


def test_the_alias_table_knows_the_models():
    table = parsers.build_alias_table(WORKBOOK)
    models = {row[3]: [] for row in machine_rows()}
    for row in machine_rows():
        models[row[3]].append(row[0])
    assert len(models) == 4
    for model, tags in models.items():
        assert table.assets_for_model(model) == tags
    assert table.assets_for_model("KX-500") == ["CNC-01", "CNC-02"]


def test_the_machines_headers_match_whatever_their_case(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title=None,
        headers_row=SPARES_HEADERS,
        rows=[],
        machines=[
            ["CNC-01", "Mill, 5-axis", "Maker", "KX-1", "S-1", "Line 3", "m1, m 1"]
        ],
        machine_headers=[
            "ASSET TAG",
            "description",
            "MAKE",
            "model",
            "Serial No",
            "LOCATION",
            "Also Called",
        ],
    )
    table = parsers.build_alias_table(path)
    assert table.asset_for("M 1") == "CNC-01"
    assert table.asset_for("s-1") == "CNC-01"


def test_a_workbook_with_no_machines_sheet_gives_an_empty_table(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx", title=None, headers_row=SPARES_HEADERS, rows=[]
    )
    assert parsers.build_alias_table(path).tags == []


BAD_TAG_MACHINES = [
    ["CNC-01", "Mill, 5-axis", "Maker", "KX-1", "S-1", "Line 3", "mill one"],
    ["machine one", "Mill, 3-axis", "Maker", "KX-2", "S-2", "Line 3", "mill two"],
]


def test_a_tag_that_is_not_a_valid_asset_id_stays_out_of_the_alias_table(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title=None,
        headers_row=SPARES_HEADERS,
        rows=[],
        machines=BAD_TAG_MACHINES,
    )
    table = parsers.build_alias_table(path)
    assert table.tags == ["CNC-01"]
    assert table.asset_for("S-2") is None
    assert table.asset_for("mill two") is None
    assert table.asset_for("S-1") == "CNC-01"


def test_a_tag_that_is_not_a_valid_asset_id_is_noted_and_not_canonical(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title=None,
        headers_row=SPARES_HEADERS,
        rows=[],
        machines=BAD_TAG_MACHINES,
    )
    extraction = parsers.parse_inventory(path)
    assets = {n.ref.key: n.canonical for n in nodes_of(extraction, "Asset")}
    assert assets == {"CNC-01": True, "machine one": False}
    assert any(
        "machine one" in note and "not a valid asset id" in note
        for note in extraction.notes
    )


# --- Inventory: the real files --------------------------------------------------


@pytest.fixture(scope="module")
def workbook_extraction() -> Extraction:
    return parsers.parse_inventory(WORKBOOK)


@pytest.fixture(scope="module")
def old_extraction() -> Extraction:
    return parsers.parse_inventory(OLD_COUNT)


def expected_quantity(cell) -> int | None:
    if cell is None:
        return None
    if isinstance(cell, int):
        return cell
    if cell == "-":
        return 0
    return int(re.fullmatch(r"(\d+) pcs", cell).group(1))


def expected_days(cell) -> int:
    if isinstance(cell, int):
        return cell
    amount, unit = cell.split()
    return int(amount) * (7 if unit == "wks" else 1)


def test_the_workbook_yields_forty_parts_with_normalized_values(workbook_extraction):
    extraction = workbook_extraction
    assert extraction.doc_path == "inventory/spares_2026-10.xlsx"
    assert extraction.doc_type == "inventory"
    assert extraction.extractor == "inventory-normalizer"
    parts = nodes_of(extraction, "Part")
    rows = spares_rows()
    assert len(parts) == len(rows) == 40
    for node, row in zip(parts, rows, strict=True):
        _, description, qty, _, bin_, lead, cost, _ = row
        props = node.props
        assert props["description"] == description
        assert props["qty_on_hand"] == expected_quantity(qty)
        assert re.fullmatch(r"[A-F]-\d{2}", props["bin"])
        assert props["bin"].replace("-", "") == bin_.replace("-", "")
        assert props["lead_time_days"] == expected_days(lead)
        assert props["unit_cost_sgd"] == float(cost)
        assert isinstance(props["unit_cost_sgd"], float)
        assert props["stock_as_of"] == "2026-10-15"
        assert props["part_no"] == node.ref.key


def test_the_stock_count_date_comes_from_the_title_row(workbook_extraction):
    dates = {
        node.props["stock_as_of"] for node in nodes_of(workbook_extraction, "Part")
    }
    assert dates == {"2026-10-15"}


def test_a_part_is_canonical_exactly_when_its_number_has_a_separator(
    workbook_extraction,
):
    parts = nodes_of(workbook_extraction, "Part")
    for node, row in zip(parts, spares_rows(), strict=True):
        raw = row[0]
        dashed = re.sub(r"[\s-]+", "-", raw.strip().upper())
        if "-" in dashed:
            assert node.canonical
            assert node.ref.key == dashed
        else:
            assert not node.canonical
            assert node.ref.key == raw
            assert node.label == raw


def test_the_canonical_and_the_raw_part_numbers_both_occur(workbook_extraction):
    flags = {node.canonical for node in nodes_of(workbook_extraction, "Part")}
    assert flags == {True, False}


def test_the_workbook_quantities_cover_a_number_pcs_and_a_blank(workbook_extraction):
    raw = [row[2] for row in spares_rows()]
    assert any(isinstance(cell, int) for cell in raw)
    assert any(isinstance(cell, str) and "pcs" in cell for cell in raw)
    assert any(cell is None for cell in raw)
    parsed = [n.props["qty_on_hand"] for n in nodes_of(workbook_extraction, "Part")]
    assert parsed.count(None) == sum(1 for cell in raw if cell is None)


def test_each_part_row_carries_its_cells_as_the_evidence_quote(workbook_extraction):
    for node, row in zip(
        nodes_of(workbook_extraction, "Part"), spares_rows(), strict=True
    ):
        (evidence,) = node.evidence
        assert evidence.doc_path == "inventory/spares_2026-10.xlsx"
        assert evidence.extractor == "inventory-normalizer"
        assert 0 < evidence.confidence <= 1
        assert evidence.quote.startswith(row[0])
        assert row[1] in evidence.quote
        assert len(evidence.quote) <= 300


def test_the_machines_sheet_yields_five_canonical_assets(workbook_extraction):
    assets = nodes_of(workbook_extraction, "Asset")
    rows = machine_rows()
    assert [node.ref.key for node in assets] == [row[0] for row in rows]
    for node, (tag, description, make, model, _, _, also_called) in zip(
        assets, rows, strict=True
    ):
        name, machine_type = description.split(", ", 1)
        assert node.canonical
        assert node.label == name
        assert node.props == {
            "id": tag,
            "name": name,
            "machine_type": machine_type,
            "vendor": make,
            "model": model,
            "aliases": [alias.strip() for alias in also_called.split(",")],
        }
        assert node.evidence[0].quote.startswith(tag)


def test_the_robot_cell_description_keeps_the_commas_of_its_machine_type(
    workbook_extraction,
):
    robot = next(
        n for n in nodes_of(workbook_extraction, "Asset") if n.ref.key == "RW-01"
    )
    assert robot.props["name"] == "Robot workcell"
    assert robot.props["machine_type"] == "Load, unload and deburr robot cell"


def test_the_location_column_gives_one_line_and_five_has_machine_edges(
    workbook_extraction,
):
    lines = nodes_of(workbook_extraction, "Line")
    assert len(lines) == 1
    line = lines[0]
    assert (line.ref.key, line.label, line.canonical) == ("LINE-03", "Line 3", True)
    assert line.props == {"id": "LINE-03", "name": "Line 3"}
    edges = [e for e in workbook_extraction.edges if e.rel == "HAS_MACHINE"]
    assert [(e.src.type, e.src.key, e.dst.type) for e in edges] == [
        ("Line", "LINE-03", "Asset")
    ] * 5
    assert [e.dst.key for e in edges] == [row[0] for row in machine_rows()]
    assert all(e.evidence for e in edges)


def test_the_workbook_extraction_counts(workbook_extraction):
    assert count_by_type(workbook_extraction) == {"Line": 1, "Asset": 5, "Part": 40}
    assert count_by_relation(workbook_extraction) == {"HAS_MACHINE": 5}
    assert workbook_extraction.chunks == []
    assert workbook_extraction.notes == []


def test_the_old_count_yields_forty_parts_with_its_own_date(old_extraction):
    assert old_extraction.doc_path == "inventory/spares_old.csv"
    assert old_extraction.doc_type == "inventory"
    parts = nodes_of(old_extraction, "Part")
    assert len(parts) == 40
    lines = OLD_COUNT.read_text().splitlines()[2:]
    assert len(lines) == 40
    for node, line in zip(parts, lines, strict=True):
        _, description, on_hand, loc = line.split(";")
        assert node.props["description"] == description
        assert node.props["qty_on_hand"] == int(on_hand)
        assert node.props["bin"] == f"{loc[0]}-{loc[1:]}"
        assert node.props["stock_as_of"] == "2026-06-30"
        assert "lead_time_days" not in node.props
        assert "unit_cost_sgd" not in node.props
        (evidence,) = node.evidence
        assert evidence.quote == line
    assert old_extraction.edges == []
    assert nodes_of(old_extraction, "Asset") == []


def test_both_stock_lists_name_the_same_forty_parts(
    workbook_extraction, old_extraction
):
    new = {part_key(n.ref.key) for n in nodes_of(workbook_extraction, "Part")}
    old = {part_key(n.ref.key) for n in nodes_of(old_extraction, "Part")}
    assert len(new) == 40
    assert new == old


def test_the_comment_line_of_the_old_count_is_not_a_part(old_extraction):
    keys = [n.ref.key for n in nodes_of(old_extraction, "Part")]
    assert not any("STOCK" in key.upper() and "COUNT" in key.upper() for key in keys)


# --- Inventory: small files -----------------------------------------------------


def test_quantities_bins_and_lead_times_are_normalized_in_a_small_workbook(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="Line 3 spares, stock count 01/09/2026",
        headers_row=SPARES_HEADERS,
        rows=[
            [
                "SEAL KIT 50",
                "Seal kit",
                "3 pcs",
                "pc",
                "B12",
                "2 wks",
                520,
                "old stock",
            ],
            [
                "SEALKIT50",
                "Seal kit again",
                "-",
                "pc",
                "B-12",
                "14 days",
                "12.50",
                None,
            ],
            ["BRG-6205-2RS", "Bearing", None, "pc", "a02", 10, 18.5, None],
            ["FLT-CL-25", "Filter", 7, "pc", "B-03", "1 wks", None, "reorder"],
        ],
    )
    extraction = parsers.parse_inventory(path)
    seal, again, bearing, filter_ = nodes_of(extraction, "Part")
    assert (seal.ref.key, seal.canonical) == ("SEAL-KIT-50", True)
    assert seal.props["qty_on_hand"] == 3
    assert seal.props["bin"] == "B-12"
    assert seal.props["lead_time_days"] == 14
    assert seal.props["unit_cost_sgd"] == 520.0
    assert (again.ref.key, again.canonical) == ("SEALKIT50", False)
    assert again.label == "SEALKIT50"
    assert again.props["qty_on_hand"] == 0
    assert again.props["bin"] == "B-12"
    assert again.props["unit_cost_sgd"] == 12.5
    assert bearing.props["qty_on_hand"] is None
    assert bearing.props["bin"] == "A-02"
    assert bearing.props["lead_time_days"] == 10
    assert filter_.props["lead_time_days"] == 7
    assert filter_.props["unit_cost_sgd"] is None
    assert {n.props["stock_as_of"] for n in nodes_of(extraction, "Part")} == {
        "2026-09-01"
    }
    assert extraction.notes == ["no Machines table found"]


def test_a_canonical_part_is_labelled_by_its_description(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=SPARES_HEADERS,
        rows=[
            ["BRG-6205-2RS", "Deep groove ball bearing", 6, "pc", "A02", 4, 18, None]
        ],
    )
    (node,) = nodes_of(parsers.parse_inventory(path), "Part")
    assert node.label == "Deep groove ball bearing"
    assert node.props["description"] == "Deep groove ball bearing"


def test_a_workbook_without_a_machines_sheet_yields_parts_only(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=SPARES_HEADERS,
        rows=[["BRG-6205-2RS", "Bearing", 6, "pc", "A02", 4, 18, None]],
    )
    extraction = parsers.parse_inventory(path)
    assert count_by_type(extraction) == {"Part": 1}
    assert extraction.edges == []


def test_a_missing_stock_date_is_noted_and_left_empty(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="Line 3 spares",
        headers_row=SPARES_HEADERS,
        rows=[["BRG-6205-2RS", "Bearing", 6, "pc", "A02", 4, 18, None]],
    )
    extraction = parsers.parse_inventory(path)
    (node,) = nodes_of(extraction, "Part")
    assert node.props["stock_as_of"] is None
    assert any("date" in note for note in extraction.notes)


def test_a_quantity_that_is_not_a_count_is_noted(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=SPARES_HEADERS,
        rows=[["BRG-6205-2RS", "Bearing", "lots", "pc", "A02", 4, 18, None]],
    )
    extraction = parsers.parse_inventory(path)
    (node,) = nodes_of(extraction, "Part")
    assert node.props["qty_on_hand"] is None
    assert any("1 quantity" in note for note in extraction.notes)


def test_rows_without_a_part_number_are_skipped_and_counted(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=SPARES_HEADERS,
        rows=[
            ["BRG-6205-2RS", "Bearing", 6, "pc", "A02", 4, 18, None],
            [None, None, None, None, None, None, None, None],
            [None, "orphan description", 2, "pc", "A03", 4, 18, None],
        ],
    )
    extraction = parsers.parse_inventory(path)
    assert len(nodes_of(extraction, "Part")) == 1
    assert any("1 row" in note and "part number" in note for note in extraction.notes)


def test_a_comma_separated_count_with_a_comment_line_parses(tmp_path):
    path = tmp_path / "count.csv"
    path.write_text(
        "stock count 02.07.2026\n"
        "PN,Desc,OnHand,Loc\n"
        "BRG-6205-2RS,Deep groove ball,6,A02\n"
        'SEALKIT50,"Main seal, kit",1,B12\n',
        encoding="utf-8",
    )
    extraction = parsers.parse_inventory(path)
    first, second = nodes_of(extraction, "Part")
    assert (first.ref.key, first.canonical) == ("BRG-6205-2RS", True)
    assert first.props["stock_as_of"] == "2026-07-02"
    assert (second.ref.key, second.canonical) == ("SEALKIT50", False)
    assert second.props["description"] == "Main seal, kit"
    assert second.props["bin"] == "B-12"


def test_a_file_with_no_part_table_gives_an_empty_extraction_and_a_note(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    extraction = parsers.parse_inventory(path)
    assert extraction.nodes == [] and extraction.edges == []
    assert extraction.notes and "part" in extraction.notes[0]


def test_the_inventory_parser_takes_and_ignores_an_alias_table():
    table = AliasTable([MachineEntry(tag="CNC-01")])
    with_table = parsers.parse_inventory(OLD_COUNT, table)
    assert with_table == parsers.parse_inventory(OLD_COUNT)


# --- The header fallback through the parsers ------------------------------------


def test_an_unknown_inventory_layout_calls_the_model_once_and_still_parses(
    tmp_path, fake_model
):
    fake_model.answer = spares_answer()
    path = tmp_path / "count.csv"
    path.write_text(
        "stock count 02.07.2026\n"
        "Item Ref,What,How many,Where\n"
        "BRG-6205-2RS,Deep groove ball,6,A02\n"
        "SEALKIT50,Main seal,1,B12\n",
        encoding="utf-8",
    )
    extraction = parsers.parse_inventory(path)
    assert len(fake_model.calls) == 1
    first, second = nodes_of(extraction, "Part")
    assert first.props["description"] == "Deep groove ball"
    assert first.props["qty_on_hand"] == 6
    assert second.props["bin"] == "B-12"
    assert first.props["stock_as_of"] == "2026-07-02"


def test_an_unknown_workbook_layout_calls_the_model_once(tmp_path, fake_model):
    fake_model.answer = spares_answer()
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=UNKNOWN_SPARES,
        rows=[["BRG-6205-2RS", "Bearing", 6, "A02"]],
    )
    extraction = parsers.parse_inventory(path)
    assert len(fake_model.calls) == 1
    (node,) = nodes_of(extraction, "Part")
    assert node.props["qty_on_hand"] == 6
    assert node.props["bin"] == "A-02"


def unknown_machines_workbook(tmp_path: Path) -> Path:
    return write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=SPARES_HEADERS,
        rows=[["BRG-6205-2RS", "Bearing", 6, "pc", "A02", 4, 18, None]],
        machines=[
            ["CNC-01", "Mill, 5-axis", "Maker", "KX-1", "S-1", "Line 3", "mill one"]
        ],
        machine_headers=UNKNOWN_MACHINES,
    )


def test_a_machines_sheet_with_unknown_headers_goes_through_the_model(
    tmp_path, fake_model
):
    fake_model.answer = machines_answer()
    extraction = parsers.parse_inventory(unknown_machines_workbook(tmp_path))
    assert len(fake_model.calls) == 1
    assert "Table machines" in fake_model.calls[0]["user"]
    (asset,) = nodes_of(extraction, "Asset")
    assert (asset.ref.key, asset.canonical) == ("CNC-01", True)
    assert asset.props["vendor"] == "Maker"
    assert count_by_relation(extraction) == {"HAS_MACHINE": 1}
    assert extraction.notes == []


def test_the_alias_table_reads_a_machines_sheet_with_unknown_headers(
    tmp_path, fake_model
):
    fake_model.answer = machines_answer()
    table = parsers.build_alias_table(unknown_machines_workbook(tmp_path))
    assert len(fake_model.calls) == 1
    assert table.asset_for("S-1") == "CNC-01"
    assert table.asset_for("mill one") == "CNC-01"


def both_unknown_workbook(path: Path, serial: str = "S-1") -> Path:
    return write_workbook(
        path,
        title="stock count 01/09/2026",
        headers_row=UNKNOWN_SPARES,
        rows=[["BRG-6205-2RS", "Bearing", 6, "A02"]],
        machines=[
            ["CNC-01", "Mill, 5-axis", "Maker", "KX-1", serial, "Line 3", "mill one"]
        ],
        machine_headers=UNKNOWN_MACHINES,
    )


def test_the_alias_table_and_the_inventory_share_one_model_call_per_workbook(
    tmp_path, fake_model
):
    fake_model.answer = HeaderMap(
        choices=spares_answer().choices + machines_answer().choices
    )
    path = both_unknown_workbook(tmp_path / "stock.xlsx")
    table = parsers.build_alias_table(path)
    extraction = parsers.parse_inventory(path)
    assert len(fake_model.calls) == 1
    assert table.asset_for("S-1") == "CNC-01"
    assert len(nodes_of(extraction, "Part")) == 1
    assert [n.canonical for n in nodes_of(extraction, "Asset")] == [True]
    parsers.build_alias_table(path)
    assert len(fake_model.calls) == 1


def test_a_changed_workbook_is_mapped_again(tmp_path, fake_model):
    fake_model.answer = HeaderMap(
        choices=spares_answer().choices + machines_answer().choices
    )
    path = both_unknown_workbook(tmp_path / "stock.xlsx")
    assert parsers.build_alias_table(path).asset_for("S-1") == "CNC-01"
    both_unknown_workbook(path, serial="S-2-longer")
    table = parsers.build_alias_table(path)
    assert len(fake_model.calls) == 2
    assert table.asset_for("S-2-longer") == "CNC-01"
    assert table.asset_for("S-1") is None


def test_a_workbook_with_no_machines_table_is_noted(tmp_path):
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=SPARES_HEADERS,
        rows=[["BRG-6205-2RS", "Bearing", 6, "pc", "A02", 4, 18, None]],
    )
    extraction = parsers.parse_inventory(path)
    assert "no Machines table found" in extraction.notes
    assert parsers.build_alias_table(path).tags == []


def test_a_text_stock_list_is_not_expected_to_have_a_machines_table():
    assert "no Machines table found" not in parsers.parse_inventory(OLD_COUNT).notes


def test_a_required_column_the_model_cannot_find_is_noted(tmp_path, fake_model):
    fake_model.answer = HeaderMap(
        choices=[HeaderChoice(table="spares", field="part_no", header="Item Ref")]
    )
    path = write_workbook(
        tmp_path / "stock.xlsx",
        title="stock count 01/09/2026",
        headers_row=UNKNOWN_SPARES,
        rows=[["BRG-6205-2RS", "Bearing", 6, "A02"]],
    )
    extraction = parsers.parse_inventory(path)
    assert len(nodes_of(extraction, "Part")) == 1
    assert any("quantity" in note or "qty" in note for note in extraction.notes)


def test_the_real_files_never_reach_the_model():
    # Any model call would fail the test, so passing shows that the synonym table
    # alone maps the real workbook and the old count.
    assert llm.structured is refuse
    assert parsers.build_alias_table(WORKBOOK).tags
    assert len(nodes_of(parsers.parse_inventory(WORKBOOK), "Part")) == 40
    assert len(nodes_of(parsers.parse_inventory(OLD_COUNT), "Part")) == 40
