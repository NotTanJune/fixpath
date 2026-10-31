"""Read how a workbook looks, so the source view can draw it as a spreadsheet.

`read_styles` returns, for each sheet, the column widths, row heights, merged cells,
frozen rows and the style of each cell that differs from the default. It does not read
the cell values.
"""

from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles.borders import Side
from openpyxl.styles.colors import Color
from openpyxl.worksheet.worksheet import Worksheet

DEFAULT_SIZE = 11
CHARACTER_PX = 7  # the width of a digit in the default font, as Excel counts it
PADDING_PX = 5
POINTS_TO_PX = 96 / 72
SIDES = ("left", "right", "top", "bottom")


def _hex(color: Color | None) -> str | None:
    """Return a colour as #rrggbb, or None if it has no plain value."""
    rgb = getattr(color, "rgb", None)
    if color is None or color.type != "rgb" or not isinstance(rgb, str):
        return None
    return "#" + rgb[-6:].lower()


def _border(side: Side | None) -> dict[str, str | None] | None:
    if side is None or side.style is None:
        return None
    return {"style": side.style, "color": _hex(side.color) or "#000000"}


def cell_style(cell: Any) -> dict[str, Any]:
    """Return the style of one cell. Only settings that differ from the default appear."""
    style: dict[str, Any] = {}
    if cell.fill.fill_type == "solid" and (fill := _hex(cell.fill.fgColor)):
        style["fill"] = fill
    font = cell.font
    if font.bold:
        style["bold"] = True
    if font.italic:
        style["italic"] = True
    if color := _hex(font.color):
        style["color"] = color
    if font.sz and font.sz != DEFAULT_SIZE:
        size = font.sz
        style["size"] = int(size) if float(size).is_integer() else size
    horizontal = cell.alignment.horizontal
    if horizontal and horizontal != "general":
        style["align"] = horizontal
    borders = {
        name: edge for name in SIDES if (edge := _border(getattr(cell.border, name)))
    }
    if borders:
        style["borders"] = borders
    if cell.number_format != "General":
        style["format"] = cell.number_format
    return style


def _sheet_styles(sheet: Worksheet) -> dict[str, Any]:
    cells = {}
    for row in sheet.iter_rows():
        for cell in row:
            if isinstance(cell, MergedCell):
                continue
            if style := cell_style(cell):
                cells[cell.coordinate] = style
    return {
        "columns": {
            letter: round(dimension.width * CHARACTER_PX + PADDING_PX)
            for letter, dimension in sorted(sheet.column_dimensions.items())
            if dimension.width and dimension.customWidth
        },
        "rows": {
            str(number): round(dimension.height * POINTS_TO_PX)
            for number, dimension in sorted(sheet.row_dimensions.items())
            if dimension.height
        },
        "merged": sorted(str(merged) for merged in sheet.merged_cells.ranges),
        "freeze": sheet.freeze_panes,
        "cells": cells,
    }


def read_styles(path: Path) -> dict[str, dict[str, Any]]:
    """Return the styles of each sheet, by sheet name."""
    workbook = load_workbook(path)
    try:
        return {sheet.title: _sheet_styles(sheet) for sheet in workbook.worksheets}
    finally:
        workbook.close()
