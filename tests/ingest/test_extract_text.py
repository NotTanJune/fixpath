"""Tests for the reader of shift handovers and loose maintenance notes.

No test calls a model. Each test supplies a canned model answer for one real note. What
is tested is the work done after the answer: start times, dates, downtime, quotes,
edges and the text chunk."""

import re
from pathlib import Path

import pytest
from ingest_stubs import (
    ALIASES,
    FakeModel,
    count_by_relation,
    count_by_type,
    fake_model,  # noqa: F401  (a fixture)
    no_model,  # noqa: F401  (an autouse fixture)
    nodes_of,
)

from app import llm
from app.config import RAW_DIR, Settings
from app.ingest import router
from app.ingest.extract_text import (
    NOTE_SYSTEM,
    IncidentItem,
    NoteExtraction,
    PartMention,
    extract_note,
)
from app.ingest.normalize import chunk_id, document_id, find_date
from app.ingest.types import Extraction

HANDOVERS = RAW_DIR / "handovers"
NIGHT = HANDOVERS / "shift notes 12 Sep night.txt"
AFTERNOON = HANDOVERS / "HO 08-31 A shift.txt"
LOOSE = RAW_DIR / "maintenance" / "note_from_arun_CNC2_spindle.txt"
ROBOT_LINE = (
    "- robot cell stopped 2359 w/ RB-551, part-present signal flickering, "
    "grippre slipipng. Repl gripper fingers (gripper finger x2), test run ok. "
    "Down approx 3h."
)


def item(**changes) -> IncidentItem:
    """The robot cell item of the night handover, with any field changed."""
    fields = {
        "machine_mention": "robot cell",
        "time_text": "2359",
        "alarm_mentions": ["RB-551"],
        "symptom": "part-present signal flickering, grippre slipipng",
        "action": "Repl gripper fingers",
        "parts": [PartMention(mention="gripper finger", qty=2)],
        "technician_mention": None,
        "downtime_text": "approx 3h",
        "quote": ROBOT_LINE,
    }
    return IncidentItem(**(fields | changes))


def answer(*items: IncidentItem, **changes) -> NoteExtraction:
    fields = {
        "note_date_text": "12 Sep",
        "shift_text": "C shift",
        "from_mention": "Arun",
        "to_mention": "Siew Ling",
        "items": list(items),
    }
    return NoteExtraction(**(fields | changes))


def extract(monkeypatch, path: Path, canned: NoteExtraction) -> Extraction:
    fake = FakeModel(canned)
    monkeypatch.setattr(llm, "structured", fake)
    return extract_note(path, ALIASES)


# --- The model call -------------------------------------------------------------


def test_the_settings_of_the_caller_go_to_the_model_call(fake_model):
    fake_model.answer = answer(item())
    mine = Settings(budget_usd=3.0)
    extract_note(NIGHT, ALIASES, settings=mine)
    assert fake_model.calls[0]["settings"] is mine
    extract_note(NIGHT, ALIASES)
    assert fake_model.calls[1]["settings"] is None


def test_one_sol_call_per_file_with_the_note_text_and_the_handover_purpose(fake_model):
    fake_model.answer = answer(item())
    extract_note(NIGHT, ALIASES)
    assert len(fake_model.calls) == 1
    call = fake_model.calls[0]
    assert call["role"] == "sol"
    assert call["schema"] is NoteExtraction
    assert call["system"] == NOTE_SYSTEM
    assert call["user"] == NIGHT.read_text(encoding="utf-8")
    assert call["purpose"] == "extract handover"


def test_a_loose_note_uses_the_note_purpose(fake_model):
    fake_model.answer = answer(note_date_text="4 May", shift_text=None, items=[])
    result = extract_note(LOOSE, ALIASES)
    assert fake_model.calls[0]["purpose"] == "extract note"
    assert result.doc_type == "maintenance_note"
    assert result.extractor == "sol-text"


def test_a_markdown_handover_uses_the_handover_type_and_purpose(fake_model, tmp_path):
    fake_model.answer = answer(items=[])
    path = tmp_path / "handovers" / "02_handover.md.txt"
    path.parent.mkdir()
    path.write_text("# Shift handover 20 Oct B\n\n- all quiet\n", encoding="utf-8")
    result = extract_note(path, ALIASES)
    assert result.doc_type == "handover"
    assert fake_model.calls[0]["purpose"] == "extract handover"


def test_the_prompt_asks_for_exact_mentions_and_breakdowns_only():
    text = NOTE_SYSTEM.lower()
    assert "exactly as it is" in text
    assert "breakdowns and repairs only" in text
    assert "routine" in text


# --- The incident node ----------------------------------------------------------


def test_one_non_canonical_incident_per_item_keyed_by_path_and_position(monkeypatch):
    other = item(machine_mention="CNC 1", alarm_mentions=[], quote="- CNC 1 stopped")
    result = extract(monkeypatch, NIGHT, answer(item(), other))
    incidents = nodes_of(result, "Incident")
    assert [n.ref.key for n in incidents] == [
        "handovers/shift notes 12 Sep night.txt#1",
        "handovers/shift notes 12 Sep night.txt#2",
    ]
    assert all(not n.canonical for n in incidents)
    assert count_by_type(result) == {"Incident": 2}
    assert result.doc_path == "handovers/shift notes 12 Sep night.txt"
    assert result.doc_type == "handover"


def test_the_incident_props_hold_the_parsed_values(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item(technician_mention="Arun")))
    node = nodes_of(result, "Incident")[0]
    assert node.props == {
        "date": "2026-09-12",
        "start": "2026-09-12T15:59:00+00:00",
        "shift": "C",
        "downtime_min": 180,
        "symptom_text": "part-present signal flickering, grippre slipipng",
        "technician": "Arun",
        "resolution_text": "Repl gripper fingers",
    }
    assert node.label == "part-present signal flickering, grippre slipipng"


def test_downtime_reads_hours_and_minutes(monkeypatch):
    texts = {"approx 3h": 180, "185 min": 185, "approx 360": 360, "11 hours": 660}
    for text, minutes in texts.items():
        result = extract(monkeypatch, NIGHT, answer(item(downtime_text=text)))
        assert nodes_of(result, "Incident")[0].props["downtime_min"] == minutes
    result = extract(monkeypatch, NIGHT, answer(item(downtime_text=None)))
    assert nodes_of(result, "Incident")[0].props["downtime_min"] is None


@pytest.mark.parametrize(
    ("text", "minutes"), [("130 mni", 130), ("145 mni", 145), ("45 mns", 45)]
)
def test_a_misspelt_minutes_unit_still_reads_as_minutes(monkeypatch, text, minutes):
    result = extract(monkeypatch, NIGHT, answer(item(downtime_text=text)))
    assert nodes_of(result, "Incident")[0].props["downtime_min"] == minutes
    assert not [n for n in result.notes if "downtime" in n]


def test_a_downtime_that_cannot_be_read_gives_none_and_a_note(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item(downtime_text="a good while")))
    assert nodes_of(result, "Incident")[0].props["downtime_min"] is None
    assert 'item 1: the downtime "a good while" cannot be read' in result.notes


def test_a_blank_downtime_adds_no_note(monkeypatch):
    for blank in (None, "", "  "):
        result = extract(monkeypatch, NIGHT, answer(item(downtime_text=blank)))
        assert not [n for n in result.notes if "downtime" in n]


def test_a_time_that_cannot_be_read_adds_a_note(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item(time_text="2561")))
    assert 'item 1: the time "2561" cannot be read' in result.notes
    result = extract(monkeypatch, NIGHT, answer(item(time_text="at lunch")))
    assert 'item 1: the time "at lunch" cannot be read' in result.notes


def test_a_blank_or_readable_time_adds_no_unreadable_note(monkeypatch):
    for fine in (None, "", "  ", "2359", "about 0039"):
        result = extract(monkeypatch, NIGHT, answer(item(time_text=fine)))
        assert not [n for n in result.notes if "cannot be read" in n]


# --- The start time -------------------------------------------------------------


def test_the_start_is_the_note_date_plus_the_time_in_sgt_in_utc(monkeypatch):
    result = extract(
        monkeypatch,
        AFTERNOON,
        answer(
            item(time_text="1429"), note_date_text="2026-08-31", shift_text="Shift A"
        ),
    )
    props = nodes_of(result, "Incident")[0].props
    assert props["start"] == "2026-08-31T06:29:00+00:00"
    assert props["date"] == "2026-08-31"
    assert props["shift"] == "A"


def test_a_c_shift_item_after_midnight_keeps_the_handover_date(monkeypatch):
    """The handover date is the day the shift starts, so the date never moves to the next day."""
    result = extract(monkeypatch, NIGHT, answer(item(time_text="0039")))
    props = nodes_of(result, "Incident")[0].props
    assert props["date"] == "2026-09-12"
    assert props["start"] == "2026-09-11T16:39:00+00:00"  # 00:39 SGT on 12 Sep
    assert props["shift"] == "C"


def test_the_date_prop_is_the_singapore_date_even_when_utc_is_the_day_before(
    monkeypatch,
):
    result = extract(
        monkeypatch, NIGHT, answer(item(time_text="0039"), note_date_text="2026-09-12")
    )
    node = nodes_of(result, "Incident")[0]
    assert node.props["start"].startswith("2026-09-11")  # UTC is the previous day
    assert node.props["date"] == "2026-09-12"


@pytest.mark.parametrize(
    ("shift_text", "start"),
    [
        ("A", "2026-09-11T23:00:00+00:00"),
        ("Shift B", "2026-09-12T07:00:00+00:00"),
        ("night", "2026-09-12T15:00:00+00:00"),
    ],
)
def test_an_item_with_no_time_starts_when_its_shift_starts(
    monkeypatch, shift_text, start
):
    result = extract(
        monkeypatch, NIGHT, answer(item(time_text=None), shift_text=shift_text)
    )
    assert nodes_of(result, "Incident")[0].props["start"] == start
    assert any("shift start" in note for note in result.notes)


def test_no_time_and_no_shift_gives_a_date_and_no_start(monkeypatch):
    result = extract(
        monkeypatch,
        LOOSE,
        answer(item(time_text=None), shift_text=None, note_date_text="4 May"),
    )
    props = nodes_of(result, "Incident")[0].props
    assert props["start"] is None
    assert props["date"] == "2026-05-04"
    assert props["shift"] is None


def test_a_loose_note_takes_its_shift_from_the_time_of_day(monkeypatch):
    result = extract(
        monkeypatch,
        LOOSE,
        answer(
            item(
                time_text="0039",
                machine_mention="CNC-02",
                alarm_mentions=["SP104"],
                quote="On 4 May CNC-02 stopped at about 0039 with SP104 on the screen.",
            ),
            note_date_text="4 May",
            shift_text=None,
        ),
    )
    props = nodes_of(result, "Incident")[0].props
    assert props["shift"] == "C"
    assert props["start"] == "2026-05-03T16:39:00+00:00"
    assert props["date"] == "2026-05-04"


def test_a_bad_time_is_ignored(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item(time_text="2561")))
    assert nodes_of(result, "Incident")[0].props["start"] == "2026-09-12T15:00:00+00:00"


# --- The date comes from the text -----------------------------------------------


def test_the_date_comes_from_the_note_text_and_not_from_the_file_name(
    monkeypatch, tmp_path
):
    """The name says month-first 08-31 and the text says 2026-04-09, and so does the
    date the model read."""
    path = tmp_path / "handovers" / "HO 08-31 A shift.txt"
    path.parent.mkdir()
    path.write_text(
        "Shift handover 2026-04-09 Shift A\nFrom: WM   To: NA\n\n- x\n",
        encoding="utf-8",
    )
    result = extract(
        monkeypatch,
        path,
        answer(
            item(time_text="1000"), note_date_text="2026-04-09", shift_text="Shift A"
        ),
    )
    assert nodes_of(result, "Incident")[0].props["date"] == "2026-04-09"


def test_the_date_falls_back_to_the_first_date_in_the_text(monkeypatch, tmp_path):
    path = tmp_path / "handovers" / "notes.txt"
    path.parent.mkdir()
    path.write_text(
        "Shift handover 23 Apr B\nFrom: WM   To: NA\n\n- x\n", encoding="utf-8"
    )
    result = extract(
        monkeypatch,
        path,
        answer(item(time_text="1000"), note_date_text=None, shift_text="B"),
    )
    assert nodes_of(result, "Incident")[0].props["date"] == "2026-04-23"


def handover_file(tmp_path, first_line: str) -> Path:
    path = tmp_path / "handovers" / "notes.txt"
    path.parent.mkdir()
    path.write_text(f"{first_line}\nFrom: WM   To: NA\n\n- x\n", encoding="utf-8")
    return path


def test_a_handover_date_comes_from_its_first_line_before_the_model_text(
    monkeypatch, tmp_path
):
    """The model swapped day and month: 05/03 is the 5th of March, but the line says
    3 May."""
    path = handover_file(tmp_path, "Shift handover 03/05/2026 night")
    result = extract(
        monkeypatch,
        path,
        answer(item(time_text="2000"), note_date_text="05/03/2026", shift_text="night"),
    )
    props = nodes_of(result, "Incident")[0].props
    assert props["date"] == "2026-05-03"
    assert props["start"] == "2026-05-03T12:00:00+00:00"
    disagreement = [n for n in result.notes if "05/03/2026" in n]
    assert len(disagreement) == 1
    assert "2026-05-03" in disagreement[0]
    assert "first line" in disagreement[0]


def test_a_handover_with_matching_dates_adds_no_note(monkeypatch, tmp_path):
    path = handover_file(tmp_path, "Shift handover 23 Apr B")
    result = extract(
        monkeypatch,
        path,
        answer(item(time_text="1000"), note_date_text="23 Apr", shift_text="B"),
    )
    assert nodes_of(result, "Incident")[0].props["date"] == "2026-04-23"
    assert not [n for n in result.notes if "date" in n]


def test_a_handover_whose_first_line_has_no_date_uses_the_model_date(
    monkeypatch, tmp_path
):
    path = handover_file(tmp_path, "Shift handover B shift")
    result = extract(
        monkeypatch,
        path,
        answer(item(time_text="1000"), note_date_text="2026-04-09", shift_text="B"),
    )
    assert nodes_of(result, "Incident")[0].props["date"] == "2026-04-09"
    assert not [n for n in result.notes if "date" in n]


def test_a_loose_note_still_takes_the_model_date_before_the_text(monkeypatch, tmp_path):
    path = tmp_path / "maintenance" / "note.txt"
    path.parent.mkdir()
    path.write_text("On 4 May CNC-02 stopped. Replaced the seal.\n", encoding="utf-8")
    result = extract(
        monkeypatch,
        path,
        answer(item(time_text="1000"), note_date_text="2026-05-06", shift_text=None),
    )
    assert nodes_of(result, "Incident")[0].props["date"] == "2026-05-06"


def test_no_date_anywhere_gives_no_date_and_a_note(monkeypatch, tmp_path):
    path = tmp_path / "handovers" / "notes.txt"
    path.parent.mkdir()
    path.write_text("Shift handover\n\n- x\n", encoding="utf-8")
    result = extract(monkeypatch, path, answer(item(), note_date_text="sometime"))
    props = nodes_of(result, "Incident")[0].props
    assert (props["date"], props["start"]) == (None, None)
    assert "no date was found in the note" in result.notes


EXPORT_TEXT = (
    "{\n"
    '  "export": {\n'
    '    "alarms": [\n'
    '      {"code": "SP-117", "machine": "CNC 2", "text": "Spindle vibration above limit",\n'
    '       "time": "2026-10-23T03:12:00+08:00"}\n'
    "    ]\n"
    "  }\n"
    "}\n"
)


@pytest.mark.parametrize(
    "model_date", [None, "2026-10-23T03:12:00+08:00", "2026-10-23 03:12"]
)
def test_an_iso_timestamp_in_an_export_is_the_date_of_the_note(
    monkeypatch, tmp_path, model_date
):
    path = tmp_path / "maintenance" / "09_alarm_export.json.txt"
    path.parent.mkdir()
    path.write_text(EXPORT_TEXT, encoding="utf-8")
    canned = answer(
        item(
            machine_mention="CNC 2",
            time_text="03:12",
            alarm_mentions=["SP-117"],
            parts=[],
            downtime_text=None,
            quote='"time": "2026-10-23T03:12:00+08:00"',
        ),
        note_date_text=model_date,
        shift_text=None,
    )
    result = extract(monkeypatch, path, canned)
    props = nodes_of(result, "Incident")[0].props
    assert props["date"] == "2026-10-23"
    assert props["start"] == "2026-10-22T19:12:00+00:00"
    assert "no date was found in the note" not in result.notes


@pytest.mark.parametrize("path", sorted(HANDOVERS.glob("*.txt")), ids=lambda p: p.name)
def test_every_real_handover_first_line_carries_a_date(path):
    first = path.read_text(encoding="utf-8").splitlines()[0]
    assert first.lower().startswith("shift handover")
    assert find_date(first) is not None


# --- Quotes ---------------------------------------------------------------------


def test_a_verbatim_quote_is_kept_as_evidence(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item()))
    evidence = nodes_of(result, "Incident")[0].evidence
    assert [(e.quote, e.extractor, e.confidence, e.doc_path) for e in evidence] == [
        (ROBOT_LINE, "sol-text", 0.8, "handovers/shift notes 12 Sep night.txt")
    ]
    assert not [note for note in result.notes if "quote" in note]


def test_a_quote_that_is_not_in_the_note_falls_back_to_the_machine_line(monkeypatch):
    claimed = "robot cell stopped at 2359 because the grippers slipped"
    result = extract(monkeypatch, NIGHT, answer(item(quote=claimed)))
    evidence = nodes_of(result, "Incident")[0].evidence
    assert evidence[0].quote == ROBOT_LINE
    assert evidence[0].quote in NIGHT.read_text(encoding="utf-8")
    assert any("quote is not in the note" in note for note in result.notes)


def test_a_one_word_quote_is_not_evidence_and_falls_back(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item(quote="stopped")))
    assert nodes_of(result, "Incident")[0].evidence[0].quote == ROBOT_LINE
    assert any("quote is not in the note" in note for note in result.notes)


def test_a_short_quote_that_holds_the_machine_mention_is_kept(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item(quote="robot cell stopped")))
    assert nodes_of(result, "Incident")[0].evidence[0].quote == "robot cell stopped"
    assert not [note for note in result.notes if "quote" in note]


def test_a_quote_of_20_characters_is_kept_without_the_machine_mention(monkeypatch):
    short = "present signal flick"
    assert len(short) == 20 and short in ROBOT_LINE
    result = extract(monkeypatch, NIGHT, answer(item(quote=short)))
    assert nodes_of(result, "Incident")[0].evidence[0].quote == short


def test_a_19_character_quote_without_the_machine_mention_falls_back(monkeypatch):
    short = "present signal flic"
    result = extract(monkeypatch, NIGHT, answer(item(quote=short)))
    assert nodes_of(result, "Incident")[0].evidence[0].quote == ROBOT_LINE


def test_the_fallback_is_case_insensitive_and_then_tries_the_alarm_code(monkeypatch):
    result = extract(
        monkeypatch, NIGHT, answer(item(quote="zzz", machine_mention="ROBOT CELL"))
    )
    assert nodes_of(result, "Incident")[0].evidence[0].quote == ROBOT_LINE
    result = extract(
        monkeypatch, NIGHT, answer(item(quote="zzz", machine_mention="the gantry"))
    )
    assert nodes_of(result, "Incident")[0].evidence[0].quote == ROBOT_LINE


def test_no_matching_line_falls_back_to_the_first_line(monkeypatch):
    result = extract(
        monkeypatch,
        NIGHT,
        answer(item(quote="zzz", machine_mention="the gantry", alarm_mentions=[])),
    )
    quote = nodes_of(result, "Incident")[0].evidence[0].quote
    assert quote == "Shift handover 12 Sep C shift"
    assert any("first line" in note for note in result.notes)


def test_a_long_quote_is_cut_to_300_characters(monkeypatch, tmp_path):
    path = tmp_path / "maintenance" / "long.txt"
    path.parent.mkdir()
    sentence = "On 4 May CNC-02 stopped " + "and the spindle kept grinding " * 20
    path.write_text(sentence + "\n", encoding="utf-8")
    result = extract(
        monkeypatch,
        path,
        answer(
            item(quote=sentence.strip(), machine_mention="CNC-02"),
            note_date_text="4 May",
        ),
    )
    quote = nodes_of(result, "Incident")[0].evidence[0].quote
    assert len(quote) == 300
    assert quote in sentence


# --- Edges ----------------------------------------------------------------------


def test_the_edges_follow_the_maintenance_csv_parser(monkeypatch):
    parts = [
        PartMention(mention="gripper finger", qty=2),
        PartMention(mention="SEAL SPDL 70", qty=None),
    ]
    result = extract(
        monkeypatch,
        NIGHT,
        answer(item(alarm_mentions=["ALM RB-551", "Err:217"], parts=parts)),
    )
    key = "handovers/shift notes 12 Sep night.txt#1"
    edges = [(e.src.key, e.rel, e.dst.type, e.dst.key, e.props) for e in result.edges]
    assert edges == [
        (key, "OCCURRED_ON", "Asset", "RW-01", {}),
        (key, "TRIGGERED_BY", "AlarmCode", "RB-551", {}),
        (key, "TRIGGERED_BY", "AlarmCode", "E-217", {}),
        (key, "USED", "Part", "GRIPPER-FINGER", {"qty": 2}),
        (key, "USED", "Part", "SEAL-SPDL-70", {"qty": 1}),
    ]
    assert count_by_relation(result) == {"OCCURRED_ON": 1, "TRIGGERED_BY": 2, "USED": 2}
    assert all(e.src.type == "Incident" for e in result.edges)
    assert any("no quantity" in note for note in result.notes)


def test_every_edge_carries_the_evidence_of_its_item(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item()))
    assert result.edges
    for edge in result.edges:
        assert [e.quote for e in edge.evidence] == [ROBOT_LINE]


def test_unmatched_mentions_stay_raw(monkeypatch):
    mention = item(
        machine_mention="the gantry",
        alarm_mentions=["??"],
        parts=[PartMention(mention="GREASEKLB2", qty=1)],
    )
    result = extract(monkeypatch, NIGHT, answer(mention))
    keys = {(e.rel, e.dst.key) for e in result.edges}
    assert keys == {
        ("OCCURRED_ON", "the gantry"),
        ("TRIGGERED_BY", "??"),
        ("USED", "GREASEKLB2"),
    }
    assert any("not in the alias table" in note for note in result.notes)


def test_an_item_with_no_machine_alarm_or_parts_has_no_edges(monkeypatch):
    bare = item(machine_mention=None, alarm_mentions=[], parts=[])
    result = extract(monkeypatch, NIGHT, answer(bare))
    assert result.edges == []
    assert any("no machine" in note for note in result.notes)


# --- The chunk and the empty case -----------------------------------------------


def test_one_chunk_holds_the_whole_note(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item()))
    doc = document_id("handovers/shift notes 12 Sep night.txt")
    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.id == chunk_id(doc, None, 0) == f"{doc}-0-0"
    assert (chunk.page, chunk.ord) == (None, 0)
    assert chunk.text == NIGHT.read_text(encoding="utf-8")
    assert chunk.doc_path == result.doc_path


def test_a_note_with_no_breakdown_has_a_chunk_and_no_nodes(monkeypatch):
    path = HANDOVERS / "handover_2026-04-09_A.txt"
    result = extract(
        monkeypatch, path, answer(note_date_text="2026-04-09", shift_text="A", items=[])
    )
    assert (result.nodes, result.edges, result.notes) == ([], [], [])
    assert len(result.chunks) == 1


def test_an_extraction_reads_back_from_json_equal(monkeypatch):
    result = extract(monkeypatch, NIGHT, answer(item()))
    assert Extraction.model_validate_json(result.model_dump_json()) == result


def test_the_real_loose_note_gives_one_incident_with_the_alarm_and_the_parts(
    monkeypatch,
):
    quote = "On 4 May CNC-02 stopped at about 0039 with SP104 on the screen."
    canned = answer(
        item(
            machine_mention="CNC-02",
            time_text="0039",
            alarm_mentions=["SP104"],
            symptom="Grinding noise from spindle, vibration alarm",
            action="replaced bearings, grease and seal",
            parts=[
                PartMention(mention="BRG 7014 ACD", qty=2),
                PartMention(mention="KLB2", qty=None),
                PartMention(mention="SEAL SPDL 70", qty=None),
            ],
            downtime_text=None,
            quote=quote,
        ),
        note_date_text="4 May",
        shift_text=None,
        from_mention=None,
        to_mention=None,
    )
    result = extract(monkeypatch, LOOSE, canned)
    node = nodes_of(result, "Incident")[0]
    assert node.ref.key == "maintenance/note_from_arun_CNC2_spindle.txt#1"
    assert node.props["date"] == "2026-05-04"
    assert node.evidence[0].quote == quote
    assert {(e.rel, e.dst.key) for e in result.edges} == {
        ("OCCURRED_ON", "CNC-02"),
        ("TRIGGERED_BY", "SP-104"),
        ("USED", "BRG-7014-ACD"),
        ("USED", "KLB2"),
        ("USED", "SEAL-SPDL-70"),
    }


# --- The router -----------------------------------------------------------------


def test_the_router_registers_the_extractor_for_handovers_and_loose_notes():
    assert router.REGISTRY[("handover", ".txt")] is extract_note
    assert router.REGISTRY[("maintenance_note", ".txt")] is extract_note
    assert router.extractor_for(NIGHT) is extract_note
    assert router.extractor_for(LOOSE) is extract_note
    assert re.fullmatch(r"DOC-[0-9a-f]{8}", document_id("x"))
