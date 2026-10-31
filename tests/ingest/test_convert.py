"""Tests for the upload converter. It turns an uploaded file into text or its native
bytes, or refuses it."""

import io
import json

import pytest
from docx import Document

from app.api.uploads import Refused
from app.ingest import convert

REFUSAL = "This file is not text, a PDF or a workbook, so it cannot be read."


def docx_bytes(build) -> bytes:
    document = Document()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("suffix", [".pdf", ".xlsx", ".csv", ".log", ".txt"])
def test_a_native_suffix_keeps_its_bytes_and_name(suffix):
    data = b"\x00\x01 not looked at \xff"
    found = convert.readable(f"file{suffix}", data)
    assert (found.name, found.data, found.converted_from) == (
        f"file{suffix}",
        data,
        None,
    )


def test_the_suffixes_are_the_ones_of_the_plan():
    assert convert.NATIVE == (".pdf", ".xlsx", ".csv", ".log", ".txt")
    assert convert.CONVERTED == (".md", ".html", ".htm", ".json", ".eml", ".docx")
    assert convert.REFUSAL == REFUSAL


def test_markdown_keeps_its_text_and_the_name_records_where_it_came_from():
    text = "# Shift handover\n\nHP-01: seals replaced.\n"
    found = convert.readable("x.md", text.encode())
    assert found.name == "x.md.txt" and found.converted_from == ".md"
    assert found.data.decode("utf-8") == text


def test_a_suffix_is_matched_without_regard_to_case():
    found = convert.readable("NOTES.MD", b"a note\n")
    assert found.name == "NOTES.MD.txt" and found.converted_from == ".md"


def test_html_drops_script_style_and_tags_and_keeps_the_visible_text_in_order():
    page = (
        b"<html><head><title>Work instruction</title><style>p {color: red}</style>"
        b"<script>var secret = 1;</script></head><body>"
        b"<h1>Replace the seal</h1><p>Lock out the <b>main</b> isolator.</p>"
        b"<ul><li>Drain the oil</li><li>Fit the kit</li></ul>"
        b"<table><tr><td>HP-01</td><td>E-217</td></tr></table>"
        b"<p>Done &amp; signed</p></body></html>"
    )
    found = convert.readable("wi.html", page)
    text = found.data.decode("utf-8")
    assert found.name == "wi.html.txt" and found.converted_from == ".html"
    assert "secret" not in text and "color" not in text and "<" not in text
    order = [
        "Work instruction",
        "Replace the seal",
        "Lock out the main isolator.",
        "Drain the oil",
        "Fit the kit",
        "HP-01",
        "E-217",
        "Done & signed",
    ]
    positions = [text.index(piece) for piece in order]
    assert positions == sorted(positions)
    assert "Lock out the main isolator." in text.splitlines()


def test_htm_is_html_too():
    found = convert.readable("a.htm", b"<p>one</p><p>two</p>")
    assert found.name == "a.htm.txt" and found.data.decode().split() == ["one", "two"]


def test_json_comes_out_pretty_printed():
    raw = json.dumps({"alarms": [{"code": "E-217", "machine": "HP-01"}], "n": 1})
    found = convert.readable("alarms.json", raw.encode())
    assert found.name == "alarms.json.txt" and found.converted_from == ".json"
    text = found.data.decode("utf-8")
    assert text == json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    assert len(text.splitlines()) > 5


def test_json_that_does_not_parse_is_kept_as_the_text_it_is():
    found = convert.readable("broken.json", b'{"alarms": [')
    assert found.data == b'{"alarms": ['


def test_an_email_gives_subject_from_date_and_the_plain_body():
    message = (
        b"From: Arun Raj <arun@plant.example>\r\n"
        b"To: shift-leads@plant.example\r\n"
        b"Subject: HP-01 seal leak again\r\n"
        b"Date: Tue, 20 Oct 2026 08:15:00 +0800\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Replaced the main cylinder seal kit.\r\nStill weeping a little.\r\n"
    )
    found = convert.readable("note.eml", message)
    text = found.data.decode("utf-8")
    assert found.name == "note.eml.txt" and found.converted_from == ".eml"
    lines = text.splitlines()
    assert lines[:3] == [
        "Subject: HP-01 seal leak again",
        "From: Arun Raj <arun@plant.example>",
        "Date: Tue, 20 Oct 2026 08:15:00 +0800",
    ]
    assert "Replaced the main cylinder seal kit." in text and "\r" not in text


def test_an_email_with_only_an_html_part_gives_its_visible_text():
    message = (
        b"Subject: Seal\r\nFrom: a@plant.example\r\nDate: Tue, 20 Oct 2026 08:15:00 +0800\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<p>Seal <b>replaced</b></p><script>x()</script>\r\n"
    )
    text = convert.readable("n.eml", message).data.decode()
    assert "Seal replaced" in text and "x()" not in text


def test_docx_gives_paragraphs_and_table_cells_in_order():
    def build(document):
        document.add_paragraph("Pump procedure")
        document.add_paragraph("Isolate the pump.")
        table = document.add_table(rows=2, cols=2)
        for row, values in enumerate((("Step", "Part"), ("1", "SEAL-KIT-50"))):
            for col, value in enumerate(values):
                table.cell(row, col).text = value
        document.add_paragraph("Close the valve.")

    found = convert.readable("pump procedure.docx", docx_bytes(build))
    text = found.data.decode("utf-8")
    assert found.name == "pump procedure.docx.txt" and found.converted_from == ".docx"
    assert text.splitlines() == [
        "Pump procedure",
        "Isolate the pump.",
        "Step | Part",
        "1 | SEAL-KIT-50",
        "Close the valve.",
    ]


def test_a_docx_that_is_not_a_zip_is_refused():
    with pytest.raises(Refused) as refused:
        convert.readable("fake.docx", b"just words")
    assert (refused.value.status, refused.value.message) == (415, REFUSAL)


def test_an_unknown_suffix_holding_utf8_text_is_accepted_as_name_ext_txt():
    found = convert.readable("shift.note", "Arun: seals again, café\n".encode())
    assert found.name == "shift.note.txt" and found.converted_from == ".note"
    assert found.data.decode("utf-8") == "Arun: seals again, café\n"


def test_a_name_without_a_suffix_holding_text_is_accepted_too():
    found = convert.readable("README", b"a plain note\n")
    assert found.name == "README.txt" and found.converted_from == ""


def test_utf16_with_a_bom_is_accepted_and_saved_as_utf8():
    data = "Shift handover - 2026-10-20\n".encode("utf-16")
    assert data.startswith((b"\xff\xfe", b"\xfe\xff"))
    found = convert.readable("old.dat", data)
    assert found.data.decode("utf-8") == "Shift handover - 2026-10-20\n"
    assert found.name == "old.dat.txt"


def test_bytes_with_a_nul_are_refused_with_the_exact_message():
    with pytest.raises(Refused) as refused:
        convert.readable("tool.bin", b"MZ\x00\x00\x01 binary")
    assert (refused.value.status, refused.value.message) == (415, REFUSAL)


def test_bytes_that_are_not_utf8_are_refused():
    with pytest.raises(Refused) as refused:
        convert.readable("blob.dat", b"\xff\xfe\xfa\xc3 broken")  # not a UTF-16 text
    assert refused.value.status == 415


def test_looks_like_text_allows_five_percent_control_characters():
    ok = "a" * 95 + "\x01" * 5
    assert convert.looks_like_text(ok.encode()) == ok
    assert convert.looks_like_text(("a" * 94 + "\x01" * 6).encode()) is None
    assert convert.looks_like_text(b"tab\tnewline\ncr\r") == "tab\tnewline\ncr\r"
    assert convert.looks_like_text(b"") == ""


def test_looks_like_text_drops_a_utf8_bom():
    assert convert.looks_like_text(b"\xef\xbb\xbfhello") == "hello"


def test_openpyxl_stays_on_the_stdlib_xml_writer_although_lxml_is_installed():
    """Importing python-docx would change how openpyxl writes a workbook, which breaks
    the generator's committed workbook. app/__init__.py prevents that."""
    import lxml  # noqa: F401  (python-docx needs it, so it is there)
    import openpyxl.xml

    assert openpyxl.xml.LXML is False
