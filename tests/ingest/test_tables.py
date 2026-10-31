"""Tests for the table helpers: reading a file as rows, and finding the table inside
a sheet. The parsers use these helpers and their own tests cover the real files. This
file covers the reading itself, with small files."""

from ingest_stubs import no_model  # noqa: F401  (an autouse fixture)
from openpyxl import Workbook

from app.ingest import layouts, tables


def test_a_text_table_keeps_the_source_line_of_every_row(tmp_path):
    path = tmp_path / "count.csv"
    path.write_text("stock count 02.07.2026\nPN;Desc\nA-1;x\n\nB-2;y\n")
    rows = tables.read_sheets(path)["count.csv"]
    assert [row.text for row in rows] == [
        "stock count 02.07.2026",
        "PN;Desc",
        "A-1;x",
        "",
        "B-2;y",
    ]
    assert rows[1].cells == ["PN", "Desc"]
    assert rows[3].cells == []


def test_a_quoted_field_that_runs_over_two_lines_is_one_row_with_both_lines(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text('Date,Ref\n2026-05-01,"two\nlines"\n2026-05-02,WO-2\n')
    rows = tables.read_sheets(path)["export.csv"]
    assert [row.cells for row in rows] == [
        ["Date", "Ref"],
        ["2026-05-01", "two\nlines"],
        ["2026-05-02", "WO-2"],
    ]
    assert rows[1].text == '2026-05-01,"two\nlines"'


def test_the_delimiter_is_the_one_of_the_header_row_or_else_the_most_used(tmp_path):
    header_led = tmp_path / "a.txt"
    header_led.write_text("PN;Desc;OnHand\nA-1;Seal, large;3\n")
    assert tables.read_sheets(header_led)["a.txt"][1].cells == [
        "A-1",
        "Seal, large",
        "3",
    ]
    unknown = tmp_path / "b.txt"
    unknown.write_text("Item\tWhat\tHow many\nA-1\tSeal, large\t3\n")
    assert tables.read_sheets(unknown)["b.txt"][1].cells == ["A-1", "Seal, large", "3"]


def test_a_workbook_gives_one_table_per_sheet_with_its_title_rows(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Spares"
    sheet["A1"] = "Line 3 spares, stock count 15/10/2026"
    sheet.append([])
    sheet.append(["Part No.", "Description", "Qty"])
    sheet.append(["BRG-1", "Bearing", 6.0])
    workbook.create_sheet("Other").append(["x"])
    path = tmp_path / "stock.xlsx"
    workbook.save(path)
    found = tables.read_sheets(path)
    assert list(found) == ["Spares", "Other"]
    assert found["Spares"][0].text == "Line 3 spares, stock count 15/10/2026"
    assert found["Spares"][3].text == "BRG-1 | Bearing | 6"


def test_a_located_table_knows_its_data_rows_and_the_text_above_the_header(tmp_path):
    path = tmp_path / "count.csv"
    path.write_text("stock count 02.07.2026\nPN;Desc\n\nA-1;x\n;;\nB-2;y\n")
    found = tables.find_table(tables.read_sheets(path), layouts.SPARES)
    assert found is not None
    assert found.header == 1
    assert found.above == "stock count 02.07.2026"
    assert [row.cells for row in found.data] == [["A-1", "x"], ["B-2", "y"]]
    shown = found.table()
    assert shown.headers == ["PN", "Desc"]
    assert shown.samples == [["A-1", "x"], ["B-2", "y"]]


def test_find_table_can_skip_a_sheet_and_guess_table_finds_an_unknown_layout(tmp_path):
    workbook = Workbook()
    workbook.active.title = "Machines"
    workbook.active.append(["Asset tag", "Description", "Model"])
    workbook.create_sheet("Stock").append(["Item Ref", "What", "How many"])
    path = tmp_path / "w.xlsx"
    workbook.save(path)
    sheets = tables.read_sheets(path)
    assert tables.find_table(sheets, layouts.MACHINES).name == "Machines"
    assert tables.find_table(sheets, layouts.MACHINES, skip=["Machines"]) is None
    assert tables.find_table(sheets, layouts.SPARES) is None
    guessed = tables.guess_table(sheets, layouts.SPARES, skip=["Machines"])
    assert guessed is not None and guessed.name == "Stock"


def test_a_cell_is_none_without_a_column_or_past_the_end_of_a_short_row():
    row = tables.Row(["a", "b"], "a,b")
    assert tables.cell(row, {"first": 0, "third": 2}, "first") == "a"
    assert tables.cell(row, {"first": 0, "third": 2}, "third") is None
    assert tables.cell(row, {"first": 0}, "second") is None


def test_cell_text_shows_what_a_person_sees():
    assert tables.cell_text(None) == ""
    assert tables.cell_text(6.0) == "6"
    assert tables.cell_text(6.5) == "6.5"
    assert tables.cell_text("  pc ") == "pc"
    assert tables.cell_text(18) == "18"
    assert tables.row_text([None, "BRG-1", "", 6.0]) == "BRG-1 | 6"
