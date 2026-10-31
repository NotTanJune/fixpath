"""Tests for the router, which decides what kind of file each raw file is, the order
files are processed in, and which extractor reads each kind.

The real raw folder is the main input. Small files in a temporary folder cover cases
where the folder and the content disagree, or where the folder gives no hint.
"""

import random
import shutil
from pathlib import Path
from typing import get_args

import pytest
from ingest_stubs import no_model  # noqa: F401  (an autouse fixture)

from app import llm
from app.config import RAW_DIR
from app.ingest import parsers, router
from app.ingest.extract_document import extract_document
from app.ingest.extract_manual import ManualExtraction, extract_manual
from app.ingest.extract_text import NoteExtraction, extract_note
from app.ingest.normalize import doc_path_for
from app.ingest.types import DocType

FOLDER_TYPES = {
    "logs": "log",
    "inventory": "inventory",
    "manuals": "manual",
    "maintenance": "maintenance_note",
    "handovers": "handover",
    "procedures": "procedure",
}
WORKBOOK = RAW_DIR / "inventory" / "spares_2026-10.xlsx"
OLD_COUNT = RAW_DIR / "inventory" / "spares_old.csv"
EXPORT = RAW_DIR / "maintenance" / "maint_log_export_2026.csv"
HANDOVER = RAW_DIR / "handovers" / "handover_2026-04-09_A.txt"
LOOSE_NOTE = RAW_DIR / "maintenance" / "note_from_arun_CNC2_spindle.txt"


def raw_files():
    return sorted(
        path
        for path in RAW_DIR.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    )


# --- classify -------------------------------------------------------------------


def test_every_real_raw_file_classifies_into_the_type_of_its_folder():
    files = raw_files()
    # the raw folder has no procedure yet; the folder exists for uploads
    assert {path.parent.name for path in files} == set(FOLDER_TYPES) - {"procedures"}
    assert len(files) >= 100
    wrong = {
        path.relative_to(RAW_DIR).as_posix(): router.classify(path)
        for path in files
        if router.classify(path) != FOLDER_TYPES[path.parent.name]
    }
    assert wrong == {}


def test_the_doc_types_are_the_ones_the_ontology_defines():
    assert set(router.PROCESSING_ORDER) == set(get_args(DocType))
    assert set(FOLDER_TYPES.values()) == set(get_args(DocType))
    assert set(router.FOLDER_TYPES) == set(FOLDER_TYPES)


def test_classify_takes_a_string_path_too():
    assert router.classify(str(WORKBOOK)) == "inventory"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("anything.pdf", "manual"),
        ("anything.PDF", "manual"),
        ("stock.xlsx", "inventory"),
        ("stock.xlsm", "inventory"),
        ("events.log", "log"),
    ],
)
def test_the_extension_alone_settles_pdf_workbooks_and_logs(tmp_path, name, expected):
    folder = tmp_path / "somewhere"
    folder.mkdir()
    path = folder / name
    path.write_bytes(b"")
    assert router.classify(path) == expected


def test_the_content_wins_over_the_folder_for_text_files(tmp_path):
    cases = [
        ("maintenance", HANDOVER, "handover"),
        ("inventory", EXPORT, "maintenance_note"),
        ("handovers", OLD_COUNT, "inventory"),
    ]
    for folder, source, expected in cases:
        (tmp_path / folder).mkdir()
        copy = tmp_path / folder / source.name
        shutil.copy(source, copy)
        assert router.classify(copy) == expected, folder


def test_a_text_file_with_no_folder_hint_is_classified_by_its_first_lines(tmp_path):
    cases = {
        "a.txt": (HANDOVER, "handover"),
        "b.csv": (EXPORT, "maintenance_note"),
        "c.csv": (OLD_COUNT, "inventory"),
        "d.txt": (None, "log"),
    }
    for name, (source, expected) in cases.items():
        path = tmp_path / name
        if source is None:
            path.write_text(
                "# Kessler KX-500 alarm history export, machine KX5-10442\n"
                "2026-04-03T02:14:55Z ALM SP-104 Spindle temperature high\n"
            )
        else:
            shutil.copy(source, path)
        assert router.classify(path) == expected, name


def test_a_comma_or_semicolon_header_row_is_sniffed_after_a_comment_line(tmp_path):
    csv = tmp_path / "count.csv"
    csv.write_text("stock count 02.07.2026\nPN,Desc,OnHand,Loc\nA-1,x,1,A01\n")
    assert router.classify(csv) == "inventory"
    export = tmp_path / "export.csv"
    export.write_text("Date;Mach.;Problem desc;Ref\n2026-05-01;CNC-01;x;WO-1\n")
    assert router.classify(export) == "maintenance_note"


def test_a_loose_note_has_no_header_so_its_folder_decides(tmp_path):
    (tmp_path / "maintenance").mkdir()
    note = tmp_path / "maintenance" / LOOSE_NOTE.name
    shutil.copy(LOOSE_NOTE, note)
    assert router.classify(note) == "maintenance_note"
    (tmp_path / "handovers").mkdir()
    moved = tmp_path / "handovers" / LOOSE_NOTE.name
    shutil.copy(LOOSE_NOTE, moved)
    assert router.classify(moved) == "handover"


def test_a_file_that_nothing_identifies_raises(tmp_path):
    unknown = tmp_path / "mystery" / "notes.txt"
    unknown.parent.mkdir()
    unknown.write_text("just some words\n")
    with pytest.raises(ValueError, match="notes.txt"):
        router.classify(unknown)
    binary = tmp_path / "mystery" / "data.bin"
    binary.write_bytes(b"\x00\x01\x02")
    with pytest.raises(ValueError):
        router.classify(binary)


def test_a_path_that_does_not_exist_falls_back_on_its_folder():
    assert router.classify(RAW_DIR / "handovers" / "not_there.txt") == "handover"
    with pytest.raises(ValueError):
        router.classify(RAW_DIR / "elsewhere" / "not_there.txt")


# --- ordered --------------------------------------------------------------------


def test_ordered_follows_the_processing_order_of_the_contract():
    files = raw_files()
    shuffled = files[:]
    random.Random(7).shuffle(shuffled)
    result = router.ordered(shuffled)
    types = [router.classify(path) for path in result]
    runs = [types[0]]
    for doc_type in types[1:]:
        if doc_type != runs[-1]:
            runs.append(doc_type)
    assert runs == ["inventory", "manual", "log", "maintenance_note", "handover"]
    assert router.PROCESSING_ORDER == (
        "inventory",
        "manual",
        "log",
        "maintenance_note",
        "procedure",
        "handover",
    )
    assert [t for t in router.PROCESSING_ORDER if t != "procedure"] == runs


def test_the_workbook_comes_before_the_old_count_and_the_export_before_the_notes():
    result = router.ordered(raw_files())
    names = [path.name for path in result]
    assert names[:2] == [WORKBOOK.name, OLD_COUNT.name]
    maintenance = [p.name for p in result if p.parent.name == "maintenance"]
    assert maintenance[0] == EXPORT.name
    assert len(maintenance) == 6
    assert maintenance[1:] == sorted(maintenance[1:])


def test_ordered_is_the_same_whatever_order_the_paths_come_in():
    files = raw_files()
    expected = router.ordered(files)
    assert router.ordered(reversed(files)) == expected
    again = files[:]
    random.Random(3).shuffle(again)
    assert router.ordered(again) == expected


def test_ordered_keeps_every_path_once_and_leaves_its_input_alone():
    files = raw_files()
    copy = files[:]
    result = router.ordered(files)
    assert files == copy
    assert sorted(result) == files
    assert len(result) == len(set(result))


def test_ordered_accepts_strings_and_returns_paths():
    result = router.ordered([str(EXPORT), str(WORKBOOK)])
    assert result == [WORKBOOK, EXPORT]


def test_ordered_of_nothing_is_empty():
    assert router.ordered([]) == []


def test_ordered_skips_hidden_files(tmp_path):
    hidden = [tmp_path / ".DS_Store", RAW_DIR / "logs" / ".gitkeep", Path(".hidden")]
    assert router.ordered([hidden[0], WORKBOOK, hidden[1], EXPORT, hidden[2]]) == [
        WORKBOOK,
        EXPORT,
    ]
    assert router.ordered(hidden) == []


def test_a_hidden_file_does_not_hide_an_unknown_visible_one(tmp_path):
    unknown = tmp_path / "mystery" / "notes.txt"
    unknown.parent.mkdir()
    unknown.write_text("just some words\n")
    hidden = tmp_path / "mystery" / ".DS_Store"
    hidden.write_bytes(b"\x00\x01")
    with pytest.raises(ValueError, match="notes.txt"):
        router.classify(unknown)
    with pytest.raises(ValueError, match="notes.txt"):
        router.ordered([hidden, unknown])


# --- The registry ---------------------------------------------------------------


def test_the_registry_holds_the_deterministic_and_the_model_based_extractors():
    assert router.REGISTRY == {
        ("log", ".log"): parsers.parse_log,
        ("inventory", ".xlsx"): parsers.parse_inventory,
        ("inventory", ".csv"): parsers.parse_inventory,
        ("maintenance_note", ".csv"): parsers.parse_maintenance_csv,
        ("handover", ".txt"): extract_note,
        ("maintenance_note", ".txt"): extract_note,
        ("manual", ".pdf"): extract_manual,
        ("procedure", ".txt"): extract_document,
    }
    assert {doc_type for doc_type, _ in router.REGISTRY} <= set(get_args(DocType))


def test_extractor_for_picks_by_doc_type_and_extension():
    log = RAW_DIR / "logs" / "HP01.log"
    assert router.extractor_for(log) is parsers.parse_log
    assert router.extractor_for(WORKBOOK) is parsers.parse_inventory
    assert router.extractor_for(OLD_COUNT) is parsers.parse_inventory
    assert router.extractor_for(EXPORT) is parsers.parse_maintenance_csv


def test_the_model_based_types_have_their_extractors():
    manual = RAW_DIR / "manuals" / "BH-250 service manual.pdf"
    assert router.extractor_for(manual) is extract_manual
    assert router.extractor_for(HANDOVER) is extract_note
    assert router.extractor_for(LOOSE_NOTE) is extract_note


def empty_answer(role, schema, *args, **kwargs):
    """What a model would answer when the file holds nothing to extract."""
    if schema is NoteExtraction:
        return NoteExtraction(
            note_date_text=None,
            shift_text=None,
            from_mention=None,
            to_mention=None,
            items=[],
        )
    if schema is ManualExtraction:
        return ManualExtraction(
            vendor="",
            model="",
            components=[],
            failure_modes=[],
            alarms=[],
            procedures=[],
            spare_parts=[],
        )
    raise AssertionError(f"no model call is expected for {schema.__name__}")


def test_every_registered_extractor_reports_the_type_the_router_gave_the_file(
    monkeypatch,
):
    monkeypatch.setattr(llm, "structured", empty_answer)
    aliases = parsers.build_alias_table(WORKBOOK)
    ran = 0
    for path in raw_files():
        extractor = router.extractor_for(path)
        if extractor is None:
            continue
        extraction = extractor(path, aliases)
        assert extraction.doc_type == router.classify(path)
        assert extraction.doc_path == doc_path_for(path)
        ran += 1
    # two inventory files, five logs, the export, 90 handovers, five loose notes, four manuals
    assert ran == 2 + 5 + 1 + 90 + 5 + 4


# --- converted text is never a stock list, and a Markdown heading is still a header ---

PART_QTY = "Oil top-up record\nPart | Qty\nOIL-HLP46-20L | 3\n"


def test_a_text_file_is_never_a_stock_list_whatever_its_table_says(tmp_path):
    inventory = tmp_path / "inventory"
    maintenance = tmp_path / "maintenance"
    inventory.mkdir()
    maintenance.mkdir()
    for table in (PART_QTY, "Item | Quantity\nA-1 | 2\n"):
        loose = maintenance / "a.docx.txt"
        loose.write_text(table)
        assert router.classify(loose) == "maintenance_note"
        elsewhere = tmp_path / "b.html.txt"
        elsewhere.write_text(table)
        with pytest.raises(router.UnknownFile):
            router.classify(elsewhere)
    # the folder still decides for a text file in the stores folder
    stock = inventory / "count.txt"
    stock.write_text(PART_QTY)
    assert router.classify(stock) == "inventory"


def test_a_header_row_of_another_layout_below_a_table_is_still_found(tmp_path):
    path = tmp_path / "mixed.docx.txt"
    path.write_text(
        PART_QTY + "Date,Mach.,Problem desc,Ref\n2026-05-01,CNC-01,x,WO-1\n"
    )
    assert router.classify(path) == "maintenance_note"


def test_a_handover_header_wins_over_a_table_in_the_same_text(tmp_path):
    path = tmp_path / "h.docx.txt"
    path.write_text("Shift handover 20 Oct Shift B\n" + PART_QTY)
    assert router.classify(path) == "handover"


def test_a_csv_with_the_same_header_is_still_a_stock_list(tmp_path):
    path = tmp_path / "count.csv"
    path.write_text("Part,Qty\nA-1,2\n")
    assert router.classify(path) == "inventory"


@pytest.mark.parametrize(
    "first",
    [
        "# Shift handover 20 Oct Shift B",
        "## Shift handover 20 Oct",
        "#   shift handover",
        "#Shift handover",
    ],
)
def test_a_markdown_heading_is_a_handover_header_for_converted_markdown(
    tmp_path, first
):
    path = tmp_path / "h.md.txt"
    path.write_text(f"\n{first}\n\n- HP-01 stopped\n")
    assert router.classify(path) == "handover"
    assert path.read_text().startswith(f"\n{first}")  # the text is not changed


def test_heading_marks_are_only_stripped_for_converted_markdown(tmp_path):
    for name in ("h.txt", "h.html.txt", "h.docx.txt"):
        path = tmp_path / name
        path.write_text("# Shift handover 20 Oct\n- x\n")
        with pytest.raises(router.UnknownFile):
            router.classify(path)


def test_a_markdown_heading_that_is_not_a_handover_stays_unrouted(tmp_path):
    path = tmp_path / "n.md.txt"
    path.write_text("# Leak tester note\nsome words\n")
    with pytest.raises(router.UnknownFile):
        router.classify(path)


# --- The type the note reader reports -------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        "# Shift handover 20 Oct Shift B",
        "## Shift handover 20 Oct",
        "#   shift handover",
        "#Shift handover",
    ],
)
def test_the_note_reader_reports_handover_for_a_markdown_handover(
    monkeypatch, tmp_path, first
):
    monkeypatch.setattr(llm, "structured", empty_answer)
    path = tmp_path / "handovers" / "h.md.txt"
    path.parent.mkdir()
    path.write_text(f"{first}\n\n- HP-01 stopped\n")
    aliases = parsers.build_alias_table(WORKBOOK)
    extraction = extract_note(path, aliases)
    assert router.classify(path) == "handover"
    assert extraction.doc_type == router.classify(path)


def test_the_note_reader_reads_heading_marks_the_way_the_router_does(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(llm, "structured", empty_answer)
    aliases = parsers.build_alias_table(WORKBOOK)
    # Heading marks count for converted Markdown only, as in router.classify.
    plain = tmp_path / "maintenance" / "h.txt"
    plain.parent.mkdir()
    plain.write_text("# Shift handover 20 Oct\n- x\n")
    assert extract_note(plain, aliases).doc_type == "maintenance_note"
    assert router.classify(plain) == "maintenance_note"  # by its folder


# --- procedures and instructions ------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        "Procedure: change the hydraulic oil and filter on PR-2",
        "Work instruction WI-27",
        "Work instruction WI-27: replace the gripper fingers on robot cell",
        "Checklist: restart RC-1 after RB563",
        "Robot cell restart checklist",
        "SOP 14 hydraulic oil change",
        "Standard operating procedure for the leak tester",
        "Instructions for the tool clamp",
        "Instruction: fit the new wiper",
        "PROCEDURE PR-4",
    ],
)
def test_a_text_file_whose_title_names_a_procedure_is_a_procedure(tmp_path, first):
    path = tmp_path / "x.docx.txt"
    path.write_text(f"{first}\nLock out the main isolator.\n")
    assert router.classify(path) == "procedure"
    # it is a procedure even when it sits in another raw folder
    elsewhere = tmp_path / "maintenance" / "x.docx.txt"
    elsewhere.parent.mkdir()
    elsewhere.write_text(f"{first}\nstep\n")
    assert router.classify(elsewhere) == "procedure"


@pytest.mark.parametrize(
    "first",
    [
        "# Procedure: change the oil",
        "## Work instruction WI-9",
        "#Checklist: restart",
        "#   sop 14",
    ],
)
def test_a_markdown_heading_names_a_procedure_for_converted_markdown(tmp_path, first):
    path = tmp_path / "p.md.txt"
    path.write_text(f"\n{first}\n\n1. Stop the cell\n")
    assert router.classify(path) == "procedure"
    assert path.read_text().startswith(f"\n{first}")  # the text is not changed
    plain = tmp_path / "p.txt"
    plain.write_text(f"{first}\n1. Stop the cell\n")
    with pytest.raises(router.UnknownFile):
        router.classify(plain)


@pytest.mark.parametrize(
    "first",
    [
        "Leak tester note, leak tester",
        "On 4 May CNC-02 stopped at about 0039",
        "Wei Ming here. MC-1 threw TC311 twice",
        "Shift notes for the procedure room",  # the word is not in the first words
        "Replaced the seal as the instruction said, then ran ten cycles",
        "proceduralist",
    ],
)
def test_other_titles_are_not_procedures(tmp_path, first):
    path = tmp_path / "n.docx.txt"
    path.write_text(f"{first}\nsome words\n")
    with pytest.raises(router.UnknownFile):
        router.classify(path)


def test_a_handover_header_wins_over_a_procedure_word(tmp_path):
    path = tmp_path / "h.txt"
    path.write_text("Shift handover 20 Oct procedure check\n- x\n")
    assert router.classify(path) == "handover"


def test_a_procedure_with_a_part_table_is_a_procedure_not_a_stock_list(tmp_path):
    path = tmp_path / "pump.docx.txt"
    path.write_text("Procedure: change the oil\nPart | Qty\nOIL-HLP46-20L | 3\n")
    assert router.classify(path) == "procedure"


def test_a_csv_is_never_a_procedure_by_its_first_line(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("Procedure,Mach.,Date\nA,B,2026-05-01\n")
    assert router.classify(path) != "procedure"
    other = tmp_path / "y.csv"
    other.write_text("Procedure,Notes\nA,B\n")
    with pytest.raises(router.UnknownFile):
        router.classify(other)


def test_a_file_in_the_procedures_folder_is_a_procedure_by_its_folder(tmp_path):
    path = tmp_path / "procedures" / "pump.docx.txt"
    path.parent.mkdir()
    path.write_text("Hydraulic oil change\nstep one\n")
    assert router.classify(path) == "procedure"


def test_procedure_files_go_to_the_document_reader():
    from app.ingest.extract_document import extract_document

    assert router.FOLDER_TYPES["procedures"] == "procedure"
    assert router.REGISTRY[("procedure", ".txt")] is extract_document
    assert router.extractor_for(Path("procedures/x.txt")) is extract_document


def test_all_committed_raw_files_still_route_as_stored_in_the_graph():
    import sqlite3
    from contextlib import closing

    from app.config import KG_PATH

    with closing(
        sqlite3.connect(f"{KG_PATH.resolve().as_uri()}?mode=ro", uri=True)
    ) as conn:
        stored = dict(conn.execute("SELECT path, doc_type FROM documents"))
    files = raw_files()
    assert len(files) == len(stored) == 107
    routed = {
        path.relative_to(RAW_DIR).as_posix(): router.classify(path) for path in files
    }
    assert routed == stored
