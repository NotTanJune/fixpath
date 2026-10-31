"""Write the stores files: a spares workbook and an older stock count.

The workbook looks like one a storekeeper keeps by hand. Part numbers and quantities
are typed in several ways, and a second sheet lists the machines. The older count is
a plain CSV file exported from the stock system.

render_inventory() writes both files and returns what each mentions. _write_workbook()
builds the workbook and _write_old_count() writes the CSV. _save() makes the workbook
file identical on every run.
"""

import csv
import datetime as dt
import io
import random
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from openpyxl import Workbook
from openpyxl.packaging.extended import ExtendedProperties
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.writer.excel import ExcelWriter
from openpyxl.xml.constants import ARC_APP
from openpyxl.xml.functions import tostring

from app.generator import catalogue as cat
from app.generator.mentions import FileMentions, Mentions, file_key
from app.generator.row_swaps import swap_neighbours
from app.generator.world import World
from app.ontology import PartProps

WORKBOOK_FILE = "spares_2026-10.xlsx"
OLD_COUNT_FILE = "spares_old.csv"
WORKBOOK_CREATOR = "stores"
WORKBOOK_APPLICATION = "Line 3 stores"
WORKBOOK_TIME = dt.datetime(2026, 10, 15, 9, 0)

SPARES_HEADERS = (
    "Part No.",
    "Description",
    "Qty",
    "UoM",
    "Bin",
    "Lead time",
    "Cost (SGD)",
    "Remarks",
)
MACHINE_HEADERS = (
    "Asset tag",
    "Description",
    "Make",
    "Model",
    "Serial no.",
    "Location",
    "Also called",
)
OLD_COUNT_HEADER = ("PN", "Desc", "OnHand", "Loc")
# The look of the workbook. It changes only how cells are drawn, not their values.
TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="1F2937")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Calibri", size=11, color="1F2937")
HEADER_FILL = PatternFill("solid", start_color="1F2937", end_color="1F2937")
BAND_FILL = PatternFill("solid", start_color="F3F5F8", end_color="F3F5F8")
THIN = Side(style="thin", color="C9D0D9")
HEADER_BOTTOM = Side(style="medium", color="0B6B5D")
GRID = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=HEADER_BOTTOM)
QTY_FORMAT = "#,##0"
COST_FORMAT = "#,##0.00"
SPARES_HEADER_ROW = 3
MIN_COLUMN_WIDTH, MAX_COLUMN_WIDTH = 9, 60
SPARES_ROW_SWAPS = 3  # pairs of neighbouring rows put in the wrong order

_UNIT = {
    "SPR-TC-40": "set",
    "OIL-HLP46-20L": "pail",
    "O-RING-NBR-12": "pack",
    "O-RING-NBR-30": "pack",
}


def _bin_order(part: PartProps) -> tuple[str | None, str]:
    return part.bin, part.part_no


def _parts_by_bin() -> list[PartProps]:
    return sorted((spec.props for spec in cat.PARTS), key=_bin_order)


def _spares_row(rng: random.Random, part: PartProps) -> list[str | int | float | None]:
    stock, days = part.qty_on_hand, part.lead_time_days
    assert stock is not None and days is not None
    spellings = (part.part_no.replace("-", " "), part.part_no.replace("-", ""))
    number = part.part_no if rng.random() < 0.6 else rng.choice(spellings)
    draw = rng.random()
    if draw < 0.7:
        qty: str | int | None = stock
    elif draw < 0.9:
        qty = f"{stock} pcs"
    else:
        qty = "-" if stock == 0 else None
    bin_ = part.bin if rng.random() < 0.5 else part.bin.replace("-", "")
    weeks = [f"{days // 7} wks"] if days % 7 == 0 else []
    lead_time = rng.choice([f"{days} days", *weeks, days])
    if stock == 0:
        remark: str | None = "reorder"
    else:
        draw = rng.random()
        remark = (
            "check with stores" if draw < 0.1 else "old stock" if draw < 0.2 else None
        )
    unit = _UNIT.get(part.part_no, "pc")
    return [
        number,
        part.description,
        qty,
        unit,
        bin_,
        lead_time,
        part.unit_cost_sgd,
        remark,
    ]


def _machine_rows() -> list[list[str]]:
    return [
        [
            asset.id,
            f"{asset.name}, {asset.machine_type}",
            asset.vendor,
            asset.model,
            cat.SERIALS[asset.id],
            f"Line 3 bay {bay}",
            ", ".join(asset.aliases),
        ]
        for bay, asset in enumerate(cat.ASSETS, start=1)
    ]


def _application_properties() -> bytes:
    """The workbook's application property, naming the stores instead of openpyxl."""
    properties = ExtendedProperties()
    properties.Application = WORKBOOK_APPLICATION
    return tostring(properties.to_tree())


def _save(workbook: Workbook, path: Path) -> None:
    """Save the workbook so the same data always gives the same file bytes.

    The normal save stamps the current time into the file. This version uses a fixed
    time, skips compression and records a fixed operating system, so the file does
    not change from one machine or run to the next."""
    scratch = io.BytesIO()
    with ZipFile(scratch, "w", ZIP_DEFLATED) as archive:
        ExcelWriter(workbook, archive).write_data()
    with ZipFile(scratch) as source, ZipFile(path, "w", ZIP_STORED) as target:
        for name in source.namelist():
            entry = ZipInfo(name, date_time=WORKBOOK_TIME.timetuple()[:6])
            entry.create_system = 3
            entry.external_attr = 0o600 << 16
            data = _application_properties() if name == ARC_APP else source.read(name)
            target.writestr(entry, data, ZIP_STORED)


def _style_sheet(
    sheet: Worksheet, header_row: int, number_formats: dict[str, str]
) -> None:
    """Give a sheet the look of a stores sheet: dark header, borders, banded rows,
    right-aligned numbers and sized columns. number_formats says how to show the
    numbers under each named header."""
    headers = {
        column: sheet.cell(row=header_row, column=column).value
        for column in range(1, sheet.max_column + 1)
    }
    for row in sheet.iter_rows(min_row=header_row, max_row=sheet.max_row):
        for cell in row:
            if cell.row == header_row:
                cell.font, cell.fill, cell.border = (
                    HEADER_FONT,
                    HEADER_FILL,
                    HEADER_BORDER,
                )
                cell.alignment = Alignment(
                    vertical="center",
                    horizontal="right"
                    if headers[cell.column] in number_formats
                    else "left",
                )
                continue
            cell.font, cell.border = BODY_FONT, GRID
            if (cell.row - header_row) % 2 == 0:
                cell.fill = BAND_FILL
            is_number = isinstance(cell.value, int | float)
            cell.alignment = Alignment(
                vertical="center", horizontal="right" if is_number else "left"
            )
            if headers[cell.column] in number_formats and is_number:
                cell.number_format = number_formats[headers[cell.column]]
    for column in headers:
        longest = max(
            len(str(cell.value))
            for cell in sheet[get_column_letter(column)][header_row - 1 :]
            if cell.value is not None
        )
        sheet.column_dimensions[get_column_letter(column)].width = min(
            max(longest + 3, MIN_COLUMN_WIDTH), MAX_COLUMN_WIDTH
        )
    sheet.row_dimensions[header_row].height = 22
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)


def _write_workbook(rng: random.Random, path: Path) -> None:
    entries = [(part, _spares_row(rng, part)) for part in _parts_by_bin()]
    swap_neighbours(
        rng,
        entries,
        key=lambda entry: _bin_order(entry[0]),
        count=SPARES_ROW_SWAPS,
    )
    rows = [row for _, row in entries]

    workbook = Workbook()
    spares = workbook.active
    spares.title = "Spares"
    spares["A1"] = f"Line 3 spares, stock count {cat.STOCK_AS_OF:%d/%m/%Y}"
    spares.merge_cells("A1:H1")
    for column, header in enumerate(SPARES_HEADERS, start=1):
        spares.cell(row=3, column=column, value=header)
    for number, row in enumerate(rows, start=4):
        for column, value in enumerate(row, start=1):
            spares.cell(row=number, column=column, value=value)

    machines = workbook.create_sheet("Machines")
    machines.append(MACHINE_HEADERS)
    for row in _machine_rows():
        machines.append(row)

    spares["A1"].font = TITLE_FONT
    spares["A1"].alignment = Alignment(vertical="center")
    spares.row_dimensions[1].height = 28
    _style_sheet(
        spares,
        SPARES_HEADER_ROW,
        {"Qty": QTY_FORMAT, "Cost (SGD)": COST_FORMAT, "Lead time": QTY_FORMAT},
    )
    _style_sheet(machines, 1, {})

    workbook.properties.creator = WORKBOOK_CREATOR
    workbook.properties.created = WORKBOOK_TIME
    workbook.properties.modified = WORKBOOK_TIME
    _save(workbook, path)


def _write_old_count(world: World, rng: random.Random, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        file.write(f"stock count {cat.STOCK_OLD_AS_OF:%d.%m.%Y}\n")
        writer = csv.writer(file, delimiter=";", lineterminator="\n")
        writer.writerow(OLD_COUNT_HEADER)
        for part in _parts_by_bin():
            number = (
                part.part_no.replace("-", "") if rng.random() < 0.5 else part.part_no
            )
            description = " ".join(part.description.split()[:3])
            writer.writerow(
                (
                    number,
                    description,
                    world.stock_old[part.part_no],
                    part.bin.replace("-", ""),
                )
            )


def render_inventory(world: World, rng: random.Random, out: Path) -> Mentions:
    """Write the workbook and the old count. Both mention every part, and the workbook
    also lists every machine."""
    folder = out / "raw" / "inventory"
    folder.mkdir(parents=True, exist_ok=True)
    workbook, old_count = folder / WORKBOOK_FILE, folder / OLD_COUNT_FILE
    _write_workbook(rng, workbook)
    _write_old_count(world, rng, old_count)
    parts = [spec.props.part_no for spec in cat.PARTS]
    return {
        file_key(out, workbook): FileMentions(
            assets=[asset.id for asset in cat.ASSETS], parts=parts
        ),
        file_key(out, old_count): FileMentions(parts=parts),
    }
