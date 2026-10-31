"""Tests for the helpers that turn messy values into one standard form: alarm codes,
part numbers, dates and times, ids and shifts.

Every documented example is checked, and so is badly formed input, which must give
None (or an error, where a date is promised). No test reads the raw folder.
"""

import datetime as dt
from pathlib import Path

import pytest

from app.config import RAW_DIR
from app.ingest.normalize import (
    LOG_STYLES,
    NOTE_STYLES,
    SGT,
    SHIFT_STARTS,
    AliasTable,
    MachineEntry,
    chunk_id,
    dashed_part,
    doc_path_for,
    document_id,
    find_date,
    lead_time_days,
    line_id,
    normalize_alarm_code,
    normalize_bin,
    note_date,
    parse_cost,
    parse_downtime,
    parse_quantity,
    parse_shift,
    parse_when,
    part_key,
    sgt_date,
    shift_at,
)
from app.ontology import validate_id

# --- normalize_alarm_code -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("E217", "E-217"),
        ("ERR 217", "E-217"),
        ("Err:217", "E-217"),
        ("ALM SP-104", "SP-104"),
        ("SP104", "SP-104"),
        ("VT18", "VT-018"),
        ("RB560", "RB-560"),
    ],
)
def test_alarm_codes_follow_the_contract_examples(raw, expected):
    assert normalize_alarm_code(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("E-217", "E-217"),
        ("E-200", "E-200"),
        ("ERR 200", "E-200"),
        ("Err: 217", "E-217"),
        ("ERR217", "E-217"),
        ("ERR-217", "E-217"),
        ("err 217", "E-217"),
        ("MSG SP-001", "SP-001"),
        ("msg sp001", "SP-001"),
        ("ALM SP104", "SP-104"),
        ("alm sp-104", "SP-104"),
        ("SP-001", "SP-001"),
        ("SP001", "SP-001"),
        ("VT-018", "VT-018"),
        ("VT12", "VT-012"),
        ("CL210", "CL-210"),
        ("TC 311", "TC-311"),
        ("RB-551", "RB-551"),
        ("  sp-104  ", "SP-104"),
        ("SP-104:", "SP-104"),
        ("ALM CL-214", "CL-214"),
    ],
)
def test_alarm_codes_cover_every_spelling_the_sources_use(raw, expected):
    assert normalize_alarm_code(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "HB",
        "---",
        "217",
        "ALM 217",
        "SP-",
        "SP-1045",
        "ABCD-123",
        "ALM",
        "ERR",
        "S P-104",
        "SP-10-4",
        "SP_104",
        "Spindle temperature high",
        "SP-104 Spindle temperature high",
    ],
)
def test_malformed_alarm_codes_give_none(raw):
    assert normalize_alarm_code(raw) is None


def test_a_value_that_is_not_text_gives_none():
    assert normalize_alarm_code(None) is None
    assert normalize_alarm_code(217) is None


def test_a_normalized_code_is_valid_and_normalizing_it_again_changes_nothing():
    for raw in ("E217", "ERR 217", "Err:217", "ALM SP-104", "VT18", "RB560"):
        code = normalize_alarm_code(raw)
        assert code is not None
        assert validate_id("AlarmCode", code)
        assert normalize_alarm_code(code) == code


# --- part_key and dashed_part ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SEAL KIT 50", "SEALKIT50"),
        ("seal-kit-50", "SEALKIT50"),
        ("SEAL-KIT-50", "SEALKIT50"),
        ("SK-50", "SK50"),
        ("  o ring nbr 12 ", "ORINGNBR12"),
        ("FLT/CL 25.", "FLTCL25"),
        ("---", ""),
        ("", ""),
    ],
)
def test_part_key_keeps_only_upper_case_letters_and_digits(raw, expected):
    assert part_key(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SEAL KIT 50", "SEAL-KIT-50"),
        ("SEAL-KIT-50", "SEAL-KIT-50"),
        ("seal  kit - 50", "SEAL-KIT-50"),
        ("seal-kit-50", "SEAL-KIT-50"),
        ("  BRG 6205 2RS ", "BRG-6205-2RS"),
        ("SK-50", "SK-50"),
        ("KLB2 grease", "KLB2-GREASE"),
        ("O-RING-NBR-12", "O-RING-NBR-12"),
    ],
)
def test_dashed_part_collapses_spaces_and_dashes_into_single_dashes(raw, expected):
    assert dashed_part(raw) == expected
    assert validate_id("Part", expected)


@pytest.mark.parametrize(
    "raw",
    [
        "SEALKIT50",
        "HLP46",
        "",
        "   ",
        "-",
        "-SEAL-KIT-50",
        "SEAL-KIT-50-",
        "SEAL/KIT 50",
        "SEAL KIT 50!",
        "SEAL_KIT_50",
    ],
)
def test_dashed_part_gives_none_without_a_separator_or_with_bad_characters(raw):
    assert dashed_part(raw) is None


def test_part_key_matches_across_every_spelling_of_one_part():
    spellings = ("SEAL-KIT-50", "SEAL KIT 50", "SEALKIT50", "seal kit 50")
    assert {part_key(spelling) for spelling in spellings} == {"SEALKIT50"}


# --- parse_when -----------------------------------------------------------------

UTC = dt.UTC
# One row per style: the text, and the moment it names in UTC. Singapore time is
# 8 hours ahead of UTC. A date alone means the start of that day in Singapore.
WHEN = {
    "iso_utc": ("2026-04-03T02:14:55Z", dt.datetime(2026, 4, 3, 2, 14, 55, tzinfo=UTC)),
    "ddmmyyyy_sgt": (
        "03/04/2026 10:14:55",
        dt.datetime(2026, 4, 3, 2, 14, 55, tzinfo=UTC),
    ),
    "epoch": ("1774972800", dt.datetime(2026, 3, 31, 16, 0, 0, tzinfo=UTC)),
    "dotted_sgt": (
        "2026.04.03 10:14:55",
        dt.datetime(2026, 4, 3, 2, 14, 55, tzinfo=UTC),
    ),
    "DD/MM/YYYY": ("17/04/2026", dt.datetime(2026, 4, 16, 16, 0, tzinfo=UTC)),
    "YYYY-MM-DD": ("2026-04-14", dt.datetime(2026, 4, 13, 16, 0, tzinfo=UTC)),
    "D-Mon-YY": ("3-Apr-26", dt.datetime(2026, 4, 2, 16, 0, tzinfo=UTC)),
    "D Mon": ("3 Apr", dt.datetime(2026, 4, 2, 16, 0, tzinfo=UTC)),
    "DD-MM": ("31-08", dt.datetime(2026, 8, 30, 16, 0, tzinfo=UTC)),
    "YYYYMMDD": ("20260406", dt.datetime(2026, 4, 5, 16, 0, tzinfo=UTC)),
}


def test_every_style_has_a_row_in_the_table():
    assert set(WHEN) == set(LOG_STYLES) | set(NOTE_STYLES)
    assert LOG_STYLES == ("iso_utc", "ddmmyyyy_sgt", "epoch", "dotted_sgt")
    assert NOTE_STYLES == (
        "DD/MM/YYYY",
        "YYYY-MM-DD",
        "D-Mon-YY",
        "D Mon",
        "DD-MM",
        "YYYYMMDD",
    )


@pytest.mark.parametrize("style", sorted(WHEN))
def test_parse_when_returns_the_utc_instant_for_every_style(style):
    raw, expected = WHEN[style]
    result = parse_when(raw, style)
    assert result == expected
    assert result.utcoffset() == dt.timedelta(0)


def test_sgt_is_eight_hours_ahead_of_utc():
    assert SGT.utcoffset(None) == dt.timedelta(hours=8)


def test_an_sgt_time_before_eight_in_the_morning_falls_on_the_day_before_in_utc():
    result = parse_when("03/04/2026 05:00:00", "ddmmyyyy_sgt")
    assert result == dt.datetime(2026, 4, 2, 21, 0, 0, tzinfo=UTC)
    assert parse_when("2026.04.03 05:00:00", "dotted_sgt") == result


def test_the_same_instant_reads_the_same_in_every_log_style():
    instant = dt.datetime(2026, 4, 3, 2, 14, 55, tzinfo=UTC)
    epoch = str(int(instant.timestamp()))
    assert parse_when("2026-04-03T02:14:55Z", "iso_utc") == instant
    assert parse_when("03/04/2026 10:14:55", "ddmmyyyy_sgt") == instant
    assert parse_when(epoch, "epoch") == instant
    assert parse_when("2026.04.03 10:14:55", "dotted_sgt") == instant


@pytest.mark.parametrize(
    ("raw", "style", "expected"),
    [
        ("4 May", "D Mon", dt.datetime(2026, 5, 3, 16, 0, tzinfo=UTC)),
        ("4 may", "D Mon", dt.datetime(2026, 5, 3, 16, 0, tzinfo=UTC)),
        ("12 Sep", "D Mon", dt.datetime(2026, 9, 11, 16, 0, tzinfo=UTC)),
        ("4-May-26", "D-Mon-YY", dt.datetime(2026, 5, 3, 16, 0, tzinfo=UTC)),
        ("28-Jun-26", "D-Mon-YY", dt.datetime(2026, 6, 27, 16, 0, tzinfo=UTC)),
        ("05-04", "DD-MM", dt.datetime(2026, 4, 4, 16, 0, tzinfo=UTC)),
        ("1/2/2026", "DD/MM/YYYY", dt.datetime(2026, 1, 31, 16, 0, tzinfo=UTC)),
    ],
)
def test_note_dates_without_a_year_mean_2026_and_short_forms_are_accepted(
    raw, style, expected
):
    assert parse_when(raw, style) == expected


@pytest.mark.parametrize(
    ("raw", "style"),
    [
        ("31/02/2026", "DD/MM/YYYY"),
        ("2026-13-01", "YYYY-MM-DD"),
        ("not a date", "DD/MM/YYYY"),
        ("", "YYYY-MM-DD"),
        ("3-Foo-26", "D-Mon-YY"),
        ("32 Apr", "D Mon"),
        ("2026-04-03", "DD/MM/YYYY"),
        ("03/04/2026 10:14:55", "iso_utc"),
        ("2026-04-03T02:14:55Z", "ddmmyyyy_sgt"),
        ("abc", "epoch"),
        ("2026.13.03 10:14:55", "dotted_sgt"),
    ],
)
def test_malformed_dates_raise_value_error(raw, style):
    with pytest.raises(ValueError):
        parse_when(raw, style)


def test_an_unknown_style_raises_value_error():
    with pytest.raises(ValueError, match="style"):
        parse_when("2026-04-03", "ISO")


def test_sgt_date_is_the_calendar_date_in_singapore():
    start_of_day = parse_when("17/04/2026", "DD/MM/YYYY")
    assert start_of_day.date() == dt.date(2026, 4, 16)  # the UTC date is a day earlier
    assert sgt_date(start_of_day) == dt.date(2026, 4, 17)
    late = parse_when("2026-04-03T17:00:00Z", "iso_utc")
    assert sgt_date(late) == dt.date(2026, 4, 4)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("17/04/2026", dt.date(2026, 4, 17)),
        ("2026-04-14", dt.date(2026, 4, 14)),
        ("3-Apr-26", dt.date(2026, 4, 3)),
        ("3 Apr", dt.date(2026, 4, 3)),
        ("31-08", dt.date(2026, 8, 31)),
        ("20260406", dt.date(2026, 4, 6)),
        ("  2026-04-14 ", dt.date(2026, 4, 14)),
        ("2026-10-23T03:12:00+08:00", dt.date(2026, 10, 23)),
        ("2026-10-23T03:12:00", dt.date(2026, 10, 23)),
        ("2026-10-23 03:12", dt.date(2026, 10, 23)),
        ("2026-10-23 03:12:40.5", dt.date(2026, 10, 23)),
        ("2026-10-22T19:30:00Z", dt.date(2026, 10, 23)),  # Singapore date is a day later
        ("2026-10-23T23:30:00-05:00", dt.date(2026, 10, 24)),
        ("2026-02-31T10:00:00+08:00", None),
        ("2026-10-23T25:00:00+08:00", None),
        ("2026-10-23 and more", None),
        ("31/02/2026", None),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_note_date_detects_the_style_and_gives_the_singapore_date(raw, expected):
    assert note_date(raw) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Line 3 spares, stock count 15/10/2026", dt.date(2026, 10, 15)),
        ("stock count 30.06.2026", dt.date(2026, 6, 30)),
        ("counted on 2026-06-30 by stores", dt.date(2026, 6, 30)),
        ("counted 3-Apr-26 by stores", dt.date(2026, 4, 3)),
        ("Shift handover 23 Apr B", dt.date(2026, 4, 23)),
        ("Shift handover 9 Sep C shift", dt.date(2026, 9, 9)),
        ("Shift handover 12 Sept night", dt.date(2026, 9, 12)),
        ("Shift handover 03/05/2026 night", dt.date(2026, 5, 3)),
        ("Shift handover 2026-04-12 Shift A", dt.date(2026, 4, 12)),
        ("the 5 marked parts, then 7 Jun", dt.date(2026, 6, 7)),
        ('"time": "2026-10-23T03:12:00+08:00",', dt.date(2026, 10, 23)),
        ("raised 2026-10-23T03:12 on CNC 2", dt.date(2026, 10, 23)),
        ("raised 2026-10-23 03:12 on CNC 2", dt.date(2026, 10, 23)),
        ("raised 2026-10-22T19:30:00Z on CNC 2", dt.date(2026, 10, 23)),
        ("raised 2026-10-23T03:12:00+08:00 and 5 Nov", dt.date(2026, 10, 23)),
        ("2026-10-23x", None),
        ("Shift handover 31 Feb A", None),
        ("no date in this line", None),
        ("stock count 31.02.2026", None),
        ("", None),
    ],
)
def test_find_date_reads_the_date_out_of_a_title_or_comment_line(text, expected):
    assert find_date(text) == expected


# --- Inventory values -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (6, 6),
        (0, 0),
        (4.0, 4),
        ("6", 6),
        ("3 pcs", 3),
        ("1 pcs", 1),
        (" 12 pcs ", 12),
        ("-", 0),
        ("", None),
        ("   ", None),
        (None, None),
        ("many", None),
        ("2.5", None),
        (2.5, None),
    ],
)
def test_quantities_read_numbers_pcs_dash_and_blank(value, expected):
    assert parse_quantity(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("B12", "B-12"),
        ("B-12", "B-12"),
        ("a02", "A-02"),
        ("A 09", "A-09"),
        ("  C-07 ", "C-07"),
        ("", None),
        (None, None),
        ("Cage", "Cage"),
    ],
)
def test_bins_get_the_dashed_form(value, expected):
    assert normalize_bin(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("14 days", 14),
        ("2 wks", 14),
        ("3 wks", 21),
        ("1 wks", 7),
        ("2 weeks", 14),
        ("10 d", 10),
        (10, 10),
        (7.0, 7),
        ("21", 21),
        ("", None),
        (None, None),
        ("soon", None),
    ],
)
def test_lead_times_become_days(value, expected):
    assert lead_time_days(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (18, 18.0),
        (38.5, 38.5),
        ("12.50", 12.5),
        ("SGD 1,200.00", 1200.0),
        ("$9", 9.0),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_costs_become_floats(value, expected):
    result = parse_cost(value)
    assert result == expected
    assert result is None or isinstance(result, float)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("175", 175),
        (175, 175),
        (" 95 ", 95),
        ("approx 360", 360),
        ("Approx 360", 360),
        ("approx. 150", 150),
        ("approx 3h", 180),
        ("apporx 6h", 360),
        ("185 min", 185),
        ("Down 185 mins", 185),
        ("130 mni", 130),
        ("145 mni", 145),
        ("45 mns", 45),
        ("45 m", 45),
        ("Down 30 minutes", 30),
        ("2 hrs", 120),
        ("about 11 hours", 660),
        ("2.5 hrs", 150),
        ("1 hour", 60),
        ("~4 H", 240),
        ("90 minutes", 90),
        ("", None),
        ("   ", None),
        (None, None),
        ("abc", None),
        ("approx", None),
    ],
)
def test_downtime_reads_an_integer_or_approx_n(value, expected):
    assert parse_downtime(value) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Line 3 bay 1", "LINE-03"),
        ("line 12", "LINE-12"),
        ("Line 3", "LINE-03"),
        ("Bay 1", None),
        ("Line three", None),
        ("Line 123", None),
        ("", None),
    ],
)
def test_the_line_id_is_line_plus_the_two_digit_number_in_the_text(text, expected):
    assert line_id(text) == expected
    assert expected is None or validate_id("Line", expected)


# --- doc_path_for ---------------------------------------------------------------


def test_doc_path_is_relative_to_the_raw_folder_with_forward_slashes():
    assert doc_path_for(RAW_DIR / "logs" / "HP01.log") == "logs/HP01.log"
    assert doc_path_for(RAW_DIR / "manuals" / "BH-250 service manual.pdf") == (
        "manuals/BH-250 service manual.pdf"
    )


def test_doc_path_takes_another_raw_folder(tmp_path):
    folder = tmp_path / "raw"
    assert doc_path_for(folder / "logs" / "a.log", folder) == "logs/a.log"


def test_a_file_outside_the_raw_folder_keeps_its_folder_and_name():
    assert doc_path_for(Path("/somewhere/else/logs/a.log")) == "logs/a.log"


# --- AliasTable -----------------------------------------------------------------

MACHINES = (
    MachineEntry(
        tag="CNC-01",
        serial="KX5-10442",
        model="KX-500",
        aliases=("CNC1", "CNC 1", "MC-1", "Kessler 1"),
    ),
    MachineEntry(
        tag="CNC-02",
        serial="KX5-10457",
        model="KX-500",
        aliases=("CNC2", "CNC 2", "MC-2", "Kessler 2"),
    ),
    MachineEntry(
        tag="HP-01",
        serial="BH250-0091",
        model="BH-250",
        aliases=("Press 2", "HP1", "hyd press", "the press", "PR-2"),
    ),
)


@pytest.fixture
def table() -> AliasTable:
    return AliasTable(MACHINES)


def test_asset_for_matches_the_tag_an_alias_or_the_serial(table):
    for machine in MACHINES:
        assert table.asset_for(machine.tag) == machine.tag
        assert table.asset_for(machine.serial) == machine.tag
        for alias in machine.aliases:
            assert table.asset_for(alias) == machine.tag


def test_asset_for_ignores_case_and_surrounding_spaces_only(table):
    assert table.asset_for("cnc 1") == "CNC-01"
    assert table.asset_for("PRESS 2") == "HP-01"
    assert table.asset_for("kx5-10457") == "CNC-02"
    assert table.asset_for("  The Press ") == "HP-01"
    # exact means exact: a near miss is not a match
    assert table.asset_for("CNC-1") is None
    assert table.asset_for("CNC 3") is None
    assert table.asset_for("press") is None
    assert table.asset_for("") is None
    assert table.asset_for(None) is None


def test_asset_for_gives_none_for_a_name_that_two_machines_share():
    shared = AliasTable(
        (
            MachineEntry(tag="CNC-01", aliases=("robot",)),
            MachineEntry(tag="RW-01", aliases=("robot", "RC-1")),
        )
    )
    assert shared.asset_for("robot") is None
    assert shared.asset_for("RC-1") == "RW-01"


def test_assets_for_model_lists_every_machine_of_that_model(table):
    assert table.assets_for_model("KX-500") == ["CNC-01", "CNC-02"]
    assert table.assets_for_model("BH-250") == ["HP-01"]
    assert table.assets_for_model("kx 500") == ["CNC-01", "CNC-02"]
    assert table.assets_for_model("KX-600") == []
    assert table.assets_for_model("") == []


def test_aliases_lists_the_names_of_one_machine(table):
    assert table.aliases("CNC-01") == ["CNC1", "CNC 1", "MC-1", "Kessler 1"]
    assert table.aliases("NOPE-01") == []
    table.aliases("CNC-01").append("changed")
    assert table.aliases("CNC-01") == ["CNC1", "CNC 1", "MC-1", "Kessler 1"]


def test_tags_come_in_sheet_order(table):
    assert table.tags == ["CNC-01", "CNC-02", "HP-01"]


# --- Document and chunk ids -----------------------------------------------------


def test_document_id_is_doc_plus_eight_hex_digits_of_the_sha256_of_the_path():
    import hashlib

    path = "handovers/handover_2026-04-09_A.txt"
    expected = "DOC-" + hashlib.sha256(path.encode()).hexdigest()[:8]
    assert document_id(path) == expected
    assert validate_id("Document", document_id(path))
    assert document_id(path) != document_id("handovers/other.txt")


def test_chunk_id_joins_the_document_id_the_page_or_zero_and_the_position():
    doc = document_id("manuals/x.pdf")
    assert chunk_id(doc, 3, 7) == f"{doc}-3-7"
    assert chunk_id(doc, None, 0) == f"{doc}-0-0"


# --- Shifts ---------------------------------------------------------------------


def test_shift_starts_are_seven_fifteen_and_twenty_three_hundred():
    assert SHIFT_STARTS == {
        "A": dt.time(7, 0),
        "B": dt.time(15, 0),
        "C": dt.time(23, 0),
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Shift A", "A"),
        ("B", "B"),
        ("C shift", "C"),
        ("night", "C"),
        ("Night shift", "C"),
        ("shift c", "C"),
        ("", None),
        (None, None),
        ("day", None),
    ],
)
def test_parse_shift_reads_the_letter_and_treats_night_as_c(text, expected):
    assert parse_shift(text) == expected


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        (dt.time(7, 0), "A"),
        (dt.time(14, 59), "A"),
        (dt.time(15, 0), "B"),
        (dt.time(22, 59), "B"),
        (dt.time(23, 0), "C"),
        (dt.time(0, 39), "C"),
        (dt.time(6, 59), "C"),
    ],
)
def test_shift_at_gives_the_shift_that_covers_a_clock_time(clock, expected):
    assert shift_at(clock) == expected
