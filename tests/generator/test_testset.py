"""Tests for the test set and the validation set: they come out the same every time,
describe the existing plant, and are read by the upload as expected.json says. These
tests read the truth to check that every expected id is real."""

import json
import zipfile

import pytest
from docx import Document

from app.api import uploads
from app.config import ROOT, TRUTH_DIR
from app.generator import catalogue as cat
from app.generator import testset
from app.ingest import convert, manual_chunks, router

COMMITTED = ROOT / "data" / "testset"
INGEST_NAMES = [
    "01_handover_2026-10-19_A.txt",
    "02_handover_2026-10-20_B.md",
    "03_note_from_arun_CNC2_spindle.eml",
    "04_maintenance_export_oct.csv",
    "05_hp01_event_log_oct.log",
    "06_service_bulletin_BH-250.pdf",
    "07_pump_procedure.docx",
    "08_work_instruction_gripper.html",
    "09_alarm_export_week43.json",
    "10_unknown_firmware.bin",
]


def tree(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    out = tmp_path_factory.mktemp("testset")
    testset.write_testset(out)
    return out


@pytest.fixture(scope="module")
def truth():
    world = json.loads((TRUTH_DIR / "world.json").read_text(encoding="utf-8"))
    return {node["id"]: node for node in world["nodes"]}


def test_it_writes_ten_ingest_files_five_validation_files_and_expected_json(made):
    assert sorted(p.name for p in (made / "ingest").iterdir()) == INGEST_NAMES
    assert len(list((made / "validation").iterdir())) == 5
    assert sorted(p.name for p in made.iterdir()) == [
        "expected.json",
        "ingest",
        "validation",
    ]


def test_the_validation_set_has_a_docx_a_md_a_pdf_a_csv_and_a_note(made):
    suffixes = sorted(p.suffix for p in (made / "validation").iterdir())
    assert suffixes == [".csv", ".docx", ".md", ".note", ".pdf"]
    pdf = next((made / "validation").glob("*.pdf")).name
    assert "KX-500" in pdf


def test_the_ingest_files_have_the_formats_of_the_plan():
    suffixes = [name.rsplit(".", 1)[1] for name in INGEST_NAMES]
    assert suffixes == [
        "txt",
        "md",
        "eml",
        "csv",
        "log",
        "pdf",
        "docx",
        "html",
        "json",
        "bin",
    ]


def test_the_same_code_gives_the_same_bytes_twice(made, tmp_path):
    testset.write_testset(tmp_path)
    assert tree(tmp_path) == tree(made)


def test_a_source_date_epoch_in_the_environment_changes_nothing(
    made, tmp_path, monkeypatch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    testset.write_testset(tmp_path)
    assert tree(tmp_path) == tree(made)
    import os

    assert os.environ["SOURCE_DATE_EPOCH"] == "1700000000"  # put back, not removed


def test_a_second_run_replaces_the_files_it_owns_and_nothing_else(tmp_path):
    (tmp_path / "ingest").mkdir()
    (tmp_path / "ingest" / "stale.txt").write_text("old")
    (tmp_path / "other.txt").write_text("keep")
    testset.write_testset(tmp_path)
    assert not (tmp_path / "ingest" / "stale.txt").exists()
    assert (tmp_path / "other.txt").read_text() == "keep"


def test_the_default_place_is_data_testset_and_never_data_raw():
    assert testset.TESTSET_DIR == ROOT / "data" / "testset"


def test_the_committed_files_are_what_the_generator_writes(made):
    committed = tree(COMMITTED)
    assert committed == tree(made), "run make testset"


def test_the_word_file_has_fixed_properties_and_zip_times(made):
    path = made / "ingest" / "07_pump_procedure.docx"
    properties = Document(str(path)).core_properties
    fixed = testset.FIXED_TIME
    assert properties.created.replace(tzinfo=None) == fixed
    assert properties.modified.replace(tzinfo=None) == fixed
    assert properties.author == "Plant maintenance"
    with zipfile.ZipFile(path) as archive:
        assert {info.date_time for info in archive.infolist()} == {testset.ZIP_TIME}


# --- expected.json ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def expected(made):
    return json.loads((made / "expected.json").read_text(encoding="utf-8"))


def test_expected_has_one_entry_per_ingest_file_with_a_doc_type_and_ids(expected):
    assert sorted(expected) == INGEST_NAMES
    for name, entry in expected.items():
        assert set(entry) == {"doc_type", "expect"}
        assert entry["expect"] == sorted(set(entry["expect"]))
    assert expected["10_unknown_firmware.bin"] == {"doc_type": None, "expect": []}
    assert all(e["expect"] for n, e in expected.items() if not n.endswith(".bin"))


def test_every_expected_id_is_a_machine_alarm_or_part_of_the_truth(expected, truth):
    ids = {i for entry in expected.values() for i in entry["expect"]}
    assert ids <= set(truth)
    assert {truth[i]["type"] for i in ids} == {"Asset", "AlarmCode", "Part"}


def text_of(path):
    if path.suffix == ".pdf":
        return "\n".join(manual_chunks.read_pages(path).values())
    found = convert.readable(path.name, path.read_bytes())
    return found.data.decode("utf-8")


def test_every_expected_id_is_in_its_file_by_the_id_or_an_alias(made, expected, truth):
    for name, entry in expected.items():
        if not entry["expect"]:
            continue
        text = text_of(made / "ingest" / name).casefold()
        for node_id in entry["expect"]:
            forms = [node_id, *truth[node_id]["props"].get("aliases", [])]
            if name.endswith(".log"):  # a controller log names its machine by serial
                forms.append(cat.SERIALS.get(node_id, node_id))
            assert any(f.casefold() in text for f in forms), (name, node_id)


def test_an_expected_id_is_always_written_in_a_form_other_than_the_id_somewhere(
    made, expected
):
    """The files are messy on purpose: not every id appears in canonical form."""
    plain = 0
    for name, entry in expected.items():
        if name.endswith(".bin"):
            continue
        text = text_of(made / "ingest" / name)
        plain += sum(i in text for i in entry["expect"])
    total = sum(len(e["expect"]) for e in expected.values())
    assert plain < total


# --- what the upload makes of each file ---------------------------------------------------


def test_the_binary_is_refused_by_readable(made):
    data = (made / "ingest" / "10_unknown_firmware.bin").read_bytes()
    assert b"\x00" in data
    with pytest.raises(uploads.Refused) as refused:
        convert.readable("10_unknown_firmware.bin", data)
    assert (refused.value.status, refused.value.message) == (415, convert.REFUSAL)


def test_every_other_ingest_file_is_accepted_under_the_doc_type_expected(
    made, expected, tmp_path
):
    folder_types = router.FOLDER_TYPES
    for name, entry in expected.items():
        if entry["doc_type"] is None:
            continue
        saved = uploads.save_upload(
            name, (made / "ingest" / name).read_bytes(), tmp_path / "raw"
        )
        assert folder_types[saved.path.parent.name] == entry["doc_type"], name


def test_the_converted_files_say_what_they_came_from(made, tmp_path):
    came = {}
    for path in sorted((made / "ingest").iterdir()):
        if path.suffix == ".bin":
            continue
        saved = uploads.save_upload(path.name, path.read_bytes(), tmp_path / "raw")
        came[path.suffix] = saved.converted_from
    assert came == {
        ".txt": None,
        ".md": ".md",
        ".eml": ".eml",
        ".csv": None,
        ".log": None,
        ".pdf": None,
        ".docx": ".docx",
        ".html": ".html",
        ".json": ".json",
    }


def test_the_converted_text_is_text_the_extractors_read(made, tmp_path):
    for name in ("07_pump_procedure.docx", "08_work_instruction_gripper.html"):
        saved = uploads.save_upload(
            name, (made / "ingest" / name).read_bytes(), tmp_path / "raw"
        )
        text = saved.path.read_text(encoding="utf-8")
        assert "<" not in text and "tracking" not in text
        assert ("GRP" in text) or ("HLP" in text)


def test_the_validation_files_are_all_accepted(made, tmp_path):
    names = []
    for path in sorted((made / "validation").iterdir()):
        saved = uploads.save_upload(path.name, path.read_bytes(), tmp_path / "raw")
        names.append(saved.path.name)
    assert (
        "V5_shift_note.note.txt" in names
        and "V1_robot_restart_checklist.docx.txt" in names
    )


def test_the_pdfs_read_back_with_headings_and_a_footer_the_chunker_drops(made):
    pages = manual_chunks.read_pages(made / "ingest" / "06_service_bulletin_BH-250.pdf")
    assert len(pages) == 2
    lines = manual_chunks.body_lines(pages[1])
    assert any(line.startswith("2 Alarm") for line in lines)
    assert not any(line.startswith("Page ") for line in lines)


def test_the_text_files_have_no_dashes_or_curly_quotes(made):
    banned = "\u2013\u2014\u2018\u2019\u201c\u201d"
    for path in (made / "ingest").iterdir():
        if path.suffix in (".pdf", ".bin", ".docx"):
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(char in text for char in banned), path.name


def test_the_set_covers_a_markdown_heading_header_and_stock_style_tables(made):
    ingest = made / "ingest"
    first = (ingest / "02_handover_2026-10-20_B.md").read_text().splitlines()[0]
    assert first == "# Shift handover 20 Oct Shift B"
    docx = text_of(ingest / "07_pump_procedure.docx")
    html = text_of(ingest / "08_work_instruction_gripper.html")
    assert "Part | Qty" in docx and "Part | Qty" in html
