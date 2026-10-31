"""Tests for the generated stores files: the spares workbook and the old count.

Every render goes to a temporary folder. Each percentage is a chance per cell, so the
tests use a random source that always returns one chosen number, which makes each
case exact instead of counting samples."""

import csv
import dataclasses
import datetime as dt
import random
import time
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import pytest
from conftest import GRIPPER_PART, PART, SEED, FixedRandom, descents
from openpyxl import load_workbook

from app.generator import catalogue
from app.generator.render_inventory import render_inventory
from app.generator.world import World

# --- Inventory ------------------------------------------------------------------------

WORKBOOK_NAME = "spares_2026-10.xlsx"
OLD_COUNT_NAME = "spares_old.csv"
SPARES_HEADERS = [
    "Part No.",
    "Description",
    "Qty",
    "UoM",
    "Bin",
    "Lead time",
    "Cost (SGD)",
    "Remarks",
]
MACHINE_HEADERS = [
    "Asset tag",
    "Description",
    "Make",
    "Model",
    "Serial no.",
    "Location",
    "Also called",
]
UOM = {
    "SPR-TC-40": "set",
    "OIL-HLP46-20L": "pail",
    "O-RING-NBR-12": "pack",
    "O-RING-NBR-30": "pack",
}
PART_NUMBER_OF = {
    variant: part_no
    for part_no in PART
    for variant in (part_no, part_no.replace("-", " "), part_no.replace("-", ""))
}


def inventory_folder(out: Path) -> Path:
    return out / "raw" / "inventory"


def spares_rows(out: Path) -> list[list]:
    sheet = load_workbook(inventory_folder(out) / WORKBOOK_NAME)["Spares"]
    return [
        [cell.value for cell in row]
        for row in sheet.iter_rows(min_row=4, max_row=sheet.max_row, max_col=8)
    ]


def spares_by_part(out: Path) -> dict[str, list]:
    rows = spares_rows(out)
    by_part = {PART_NUMBER_OF[row[0]]: row for row in rows}
    assert len(by_part) == len(rows) == 40
    return by_part


def render_inventory_with(world: World, value: float, out: Path):
    return out, render_inventory(world, FixedRandom(value), out)


@pytest.fixture(scope="module")
def inventory(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("inventory")
    return out, render_inventory(world, random.Random(SEED + 3), out)


def test_the_inventory_folder_holds_the_workbook_and_the_old_count(inventory):
    out, _ = inventory
    names = sorted(path.name for path in inventory_folder(out).iterdir())
    assert names == sorted([WORKBOOK_NAME, OLD_COUNT_NAME])


def test_the_spares_sheet_has_a_merged_title_a_gap_the_headers_and_40_rows(inventory):
    out, _ = inventory
    workbook = load_workbook(inventory_folder(out) / WORKBOOK_NAME)
    assert workbook.sheetnames == ["Spares", "Machines"]
    sheet = workbook["Spares"]
    assert sheet["A1"].value == "Line 3 spares, stock count 15/10/2026"
    assert {str(cells) for cells in sheet.merged_cells.ranges} == {"A1:H1"}
    assert [cell.value for cell in sheet[2]] == [None] * sheet.max_column
    assert [cell.value for cell in sheet[3]][:8] == SPARES_HEADERS
    assert sheet.max_row == 43 and sheet.max_column == 8
    assert len(spares_rows(out)) == 40


def test_each_part_is_one_row_with_its_description_and_unit_cost(inventory):
    out, _ = inventory
    for part_no, row in spares_by_part(out).items():
        assert row[1] == PART[part_no].description
        assert row[6] == PART[part_no].unit_cost_sgd
        assert isinstance(row[6], (int, float))


@pytest.mark.parametrize(("u", "canonical"), [(0.59, True), (0.61, False)])
def test_the_part_number_is_canonical_60_percent_of_the_time(
    world, tmp_path, u, canonical
):
    out, _ = render_inventory_with(world, u, tmp_path)
    variants = set()
    for part_no, row in spares_by_part(out).items():
        assert (row[0] == part_no) == canonical
        if not canonical:
            variants.add(
                row[0] in (part_no.replace("-", " "), part_no.replace("-", ""))
            )
    assert variants <= {True}


def test_a_part_number_variant_is_one_of_the_two_generated_spellings(world, tmp_path):
    out, _ = render_inventory_with(world, 0.61, tmp_path)
    kinds = {
        "spaces" if " " in row[0] else "joined" for row in spares_by_part(out).values()
    }
    assert kinds == {"spaces", "joined"}


def test_a_part_number_variant_is_spelled_from_the_part_number_whatever_the_alias_order(
    world, tmp_path, monkeypatch
):
    """The generated spellings and the hand aliases may come in any order in the
    catalogue. With every alias list reversed, the spaced and dashless forms still
    come from the part number."""
    reversed_parts = tuple(
        dataclasses.replace(
            spec,
            props=spec.props.model_copy(update={"aliases": spec.props.aliases[::-1]}),
        )
        for spec in catalogue.PARTS
    )
    monkeypatch.setattr(catalogue, "PARTS", reversed_parts)
    out, _ = render_inventory_with(world, 0.61, tmp_path)
    part_of_description = {part.description: part_no for part_no, part in PART.items()}
    rows = spares_rows(out)
    assert len(rows) == 40
    for row in rows:
        part_no = part_of_description[row[1]]
        assert row[0] in (part_no.replace("-", " "), part_no.replace("-", "")), row


@pytest.mark.parametrize(
    ("u", "shape"),
    [(0.69, "integer"), (0.71, "pcs"), (0.89, "pcs"), (0.91, "dash or blank")],
)
def test_qty_is_an_integer_or_n_pcs_or_a_dash_for_zero_or_blank(
    world, tmp_path, u, shape
):
    out, _ = render_inventory_with(world, u, tmp_path)
    for part_no, row in spares_by_part(out).items():
        stock = PART[part_no].qty_on_hand
        if shape == "integer":
            assert row[2] == stock and isinstance(row[2], int)
        elif shape == "pcs":
            assert row[2] == f"{stock} pcs"
        else:
            assert row[2] == ("-" if stock == 0 else None)


def test_the_unit_of_measure_is_pc_except_for_sets_pails_and_packs(inventory):
    out, _ = inventory
    for part_no, row in spares_by_part(out).items():
        assert row[3] == UOM.get(part_no, "pc")


@pytest.mark.parametrize(("u", "dash"), [(0.49, True), (0.51, False)])
def test_the_bin_is_written_with_or_without_the_dash_half_the_time(
    world, tmp_path, u, dash
):
    out, _ = render_inventory_with(world, u, tmp_path)
    for part_no, row in spares_by_part(out).items():
        bin_ = PART[part_no].bin
        assert row[4] == (bin_ if dash else bin_.replace("-", ""))


def test_lead_time_is_days_or_weeks_when_they_divide_by_seven_or_the_plain_number(
    world, tmp_path_factory
):
    forms = set()
    for seed in range(10):
        out = tmp_path_factory.mktemp("lead")
        render_inventory(world, FixedRandom(0.5, seed=seed), out)
        for part_no, row in spares_by_part(out).items():
            days = PART[part_no].lead_time_days
            allowed = [f"{days} days", days] + (
                [f"{days // 7} wks"] if days % 7 == 0 else []
            )
            assert row[5] in allowed, (part_no, row[5])
            forms.add("weeks" if str(row[5]).endswith("wks") else type(row[5]).__name__)
    assert forms == {"weeks", "str", "int"}


@pytest.mark.parametrize(
    ("u", "remark"),
    [
        (0.09, "check with stores"),
        (0.11, "old stock"),
        (0.19, "old stock"),
        (0.21, None),
    ],
)
def test_remarks_say_reorder_for_zero_stock_and_otherwise_are_mostly_blank(
    world, tmp_path, u, remark
):
    out, _ = render_inventory_with(world, u, tmp_path)
    for part_no, row in spares_by_part(out).items():
        expected = "reorder" if PART[part_no].qty_on_hand == 0 else remark
        assert row[7] == expected, part_no
    assert spares_by_part(out)[GRIPPER_PART][7] == "reorder"


def test_the_rows_run_by_bin_apart_from_three_swapped_neighbours(inventory):
    out, _ = inventory
    keys = [
        (PART[PART_NUMBER_OF[row[0]]].bin, PART_NUMBER_OF[row[0]])
        for row in spares_rows(out)
    ]
    inversions = sum(
        1
        for i in range(len(keys))
        for j in range(i + 1, len(keys))
        if keys[i] > keys[j]
    )
    assert inversions == 3


@pytest.mark.parametrize("seed", range(20))
def test_the_sheet_has_three_separate_swapped_pairs_whatever_the_draws(
    world, tmp_path, seed
):
    """Three swaps that do not touch leave the bins falling back exactly three times."""
    render_inventory(world, FixedRandom(0.5, seed=seed), tmp_path)
    keys = [
        (PART[PART_NUMBER_OF[row[0]]].bin, PART_NUMBER_OF[row[0]])
        for row in spares_rows(tmp_path)
    ]
    assert descents(keys) == 3


def test_the_machines_sheet_has_one_row_per_machine(inventory):
    out, _ = inventory
    sheet = load_workbook(inventory_folder(out) / WORKBOOK_NAME)["Machines"]
    rows = [[cell.value for cell in row] for row in sheet.iter_rows()]
    assert rows[0] == MACHINE_HEADERS
    assert len(rows) == 6
    assert rows[1] == [
        "CNC-01",
        "Machining centre 1, 5-axis machining centre",
        "Kessler Werkzeugmaschinen",
        "KX-500",
        "KX5-10442",
        "Line 3 bay 1",
        "CNC1, CNC 1, MC-1, Kessler 1",
    ]
    assert rows[3] == [
        "HP-01",
        "Hydraulic press, 250 t hydraulic press",
        "Bramwell Hydraulics",
        "BH-250",
        "BH250-0091",
        "Line 3 bay 3",
        "Press 2, HP1, hyd press, the press, PR-2",
    ]
    assert [row[0] for row in rows[1:]] == [a.id for a in catalogue.ASSETS]
    assert [row[5] for row in rows[1:]] == [f"Line 3 bay {n}" for n in range(1, 6)]
    for row, asset in zip(rows[1:], catalogue.ASSETS, strict=True):
        assert row[4] == catalogue.SERIALS[asset.id]
        assert row[6] == ", ".join(asset.aliases)


def test_the_workbook_creator_and_times_are_set(inventory):
    out, _ = inventory
    properties = load_workbook(inventory_folder(out) / WORKBOOK_NAME).properties
    assert properties.creator == "stores"
    assert properties.created == dt.datetime(2026, 10, 15, 9, 0)
    assert properties.modified == dt.datetime(2026, 10, 15, 9, 0)


def test_every_entry_of_the_workbook_carries_the_stores_time(inventory):
    out, _ = inventory
    with ZipFile(inventory_folder(out) / WORKBOOK_NAME) as archive:
        stamps = {info.date_time for info in archive.infolist()}
    assert stamps == {(2026, 10, 15, 9, 0, 0)}


def test_the_workbook_bytes_do_not_depend_on_the_clock(
    world, tmp_path_factory, monkeypatch
):
    first, second = tmp_path_factory.mktemp("first"), tmp_path_factory.mktemp("second")
    render_inventory(world, random.Random(SEED + 3), first)
    now = time.time
    monkeypatch.setattr(time, "time", lambda: now() + 86_400)
    render_inventory(world, random.Random(SEED + 3), second)
    assert (inventory_folder(first) / WORKBOOK_NAME).read_bytes() == (
        inventory_folder(second) / WORKBOOK_NAME
    ).read_bytes()


def test_the_workbook_names_the_stores_as_its_application(inventory):
    """No spreadsheet product or library appears as the program that wrote the file."""
    out, _ = inventory
    with ZipFile(inventory_folder(out) / WORKBOOK_NAME) as archive:
        app = ElementTree.fromstring(archive.read("docProps/app.xml"))
    namespace = (
        "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"
    )
    assert app.findtext(f"{namespace}Application") == "Line 3 stores"


def read_old_count(out: Path) -> tuple[list[str], list[list[str]]]:
    with (inventory_folder(out) / OLD_COUNT_NAME).open(
        encoding="utf-8", newline=""
    ) as file:
        first = file.readline().rstrip("\n")
        return [first], list(csv.reader(file, delimiter=";"))


def test_the_old_count_starts_with_a_comment_line_then_a_semicolon_header(inventory):
    out, _ = inventory
    text = (inventory_folder(out) / OLD_COUNT_NAME).read_text(encoding="utf-8")
    assert text.startswith("stock count 30.06.2026\nPN;Desc;OnHand;Loc\n")
    assert text.endswith("\n")
    _, rows = read_old_count(out)
    assert rows[0] == ["PN", "Desc", "OnHand", "Loc"]
    assert len(rows) - 1 == 40


@pytest.mark.parametrize(("u", "dashless"), [(0.49, True), (0.51, False)])
def test_the_old_count_has_the_june_quantities_and_short_descriptions(
    world, tmp_path, u, dashless
):
    out, _ = render_inventory_with(world, u, tmp_path)
    _, rows = read_old_count(out)
    seen = {}
    for pn, desc, on_hand, loc in rows[1:]:
        part_no = PART_NUMBER_OF[pn]
        seen[part_no] = int(on_hand)
        assert (pn == part_no.replace("-", "")) == dashless
        assert desc == " ".join(PART[part_no].description.split()[:3])
        assert loc == PART[part_no].bin.replace("-", "")
    assert seen == world.stock_old and len(seen) == 40


def test_the_inventory_files_mention_every_part_and_the_workbook_every_machine(
    inventory,
):
    _, mentions = inventory
    workbook = mentions[f"raw/inventory/{WORKBOOK_NAME}"]
    old = mentions[f"raw/inventory/{OLD_COUNT_NAME}"]
    assert workbook.parts == sorted(PART) == old.parts
    assert workbook.assets == sorted(a.id for a in catalogue.ASSETS)
    assert old.assets == []
    for mention in (workbook, old):
        assert mention.incidents == [] and mention.alarm_codes == []
        assert mention.conflicts == []
    assert sorted(mentions) == sorted(
        [f"raw/inventory/{WORKBOOK_NAME}", f"raw/inventory/{OLD_COUNT_NAME}"]
    )
