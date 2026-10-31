"""Tests for reading a workbook's cell styling, which the source view shows."""

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from app.api.sheet_styles import read_styles
from app.config import RAW_DIR

WORKBOOK = RAW_DIR / "inventory" / "spares_2026-10.xlsx"


def styled_workbook(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "Stock"
    sheet["A1"] = "Title"
    sheet.merge_cells("A1:C1")
    sheet["A1"].font = Font(bold=True, italic=True, size=14, color="FF112233")
    sheet["A1"].alignment = Alignment(horizontal="center")
    sheet["A3"] = "Qty"
    sheet["A3"].fill = PatternFill(
        "solid", start_color="FF1F2937", end_color="FF1F2937"
    )
    side = Side(style="thin", color="FFC9D0D9")
    sheet["A3"].border = Border(bottom=side, top=Side(style="medium", color="FF000000"))
    sheet["B4"] = 1234.5
    sheet["B4"].number_format = "#,##0.00"
    sheet["B4"].alignment = Alignment(horizontal="right")
    sheet["C4"] = "plain"
    sheet.column_dimensions["A"].width = 10
    sheet.column_dimensions["B"].width = 20
    sheet.row_dimensions[1].height = 30
    sheet.freeze_panes = "A4"
    book.create_sheet("Bare")["A1"] = "nothing styled"
    book.save(path)
    return path


def test_a_sheet_reports_widths_heights_merges_and_the_frozen_pane(tmp_path):
    styles = read_styles(styled_workbook(tmp_path / "book.xlsx"))
    stock = styles["Stock"]
    assert stock["columns"] == {"A": 75, "B": 145}
    assert stock["rows"] == {"1": 40}
    assert stock["merged"] == ["A1:C1"]
    assert stock["freeze"] == "A4"


def test_a_cell_reports_only_what_differs_from_the_default(tmp_path):
    cells = read_styles(styled_workbook(tmp_path / "book.xlsx"))["Stock"]["cells"]
    assert cells["A1"] == {
        "bold": True,
        "italic": True,
        "color": "#112233",
        "size": 14,
        "align": "center",
    }
    assert cells["A3"] == {
        "fill": "#1f2937",
        "borders": {
            "top": {"style": "medium", "color": "#000000"},
            "bottom": {"style": "thin", "color": "#c9d0d9"},
        },
    }
    assert cells["B4"] == {"align": "right", "format": "#,##0.00"}
    assert "C4" not in cells


def test_a_sheet_without_styling_has_empty_styles(tmp_path):
    bare = read_styles(styled_workbook(tmp_path / "book.xlsx"))["Bare"]
    assert bare == {
        "columns": {},
        "rows": {},
        "merged": [],
        "freeze": None,
        "cells": {},
    }


def test_the_header_row_of_the_real_workbook_is_dark_with_white_bold_text():
    sheet = read_styles(WORKBOOK)["Spares"]
    assert sheet["freeze"] == "A4"
    assert sheet["merged"] == ["A1:H1"]
    for column in "ABCDEFGH":
        header = sheet["cells"][f"{column}3"]
        assert header["fill"] == "#1f2937"
        assert header["bold"] is True and header["color"] == "#ffffff"
        assert header["borders"]["bottom"]["style"] == "medium"
    assert sheet["cells"]["A1"]["bold"] is True and sheet["cells"]["A1"]["size"] == 14
    assert sheet["cells"]["G4"]["format"] == "#,##0.00"
    assert sheet["cells"]["G4"]["align"] == "right"
