"""Tests for the generated maintenance files: the log export and the loose notes.

Every render goes to a temporary folder. Each percentage is a chance per item, so the
tests use a random source that always returns one chosen number, which makes each
case exact instead of counting samples."""

import csv
import dataclasses
import datetime as dt
import re
from pathlib import Path

import pytest
from conftest import (
    ALARM_VARIANTS,
    ASSET_OF_REF,
    ASSET_REFS,
    EXPORT_NAME,
    PART,
    PART_OF_ALIAS,
    FixedRandom,
    Truth,
    descents,
    render_notes_with,
    repl,
)

from app.generator import catalogue
from app.generator.mentions import file_key
from app.generator.render_notes import render_notes

FAILURE_MODE = {spec.props.id: spec.props for spec in catalogue.FAILURE_MODES}

# --- The maintenance log export -------------------------------------------------------

EXPORT_HEADER = [
    "Date",
    "Mach.",
    "Problem desc",
    "Action taken",
    "Parts",
    "Tech",
    "Downtime (min)",
    "Ref",
]
DATE_STYLES = (
    ("%d/%m/%Y", re.compile(r"\d{2}/\d{2}/\d{4}")),
    ("%Y-%m-%d", re.compile(r"\d{4}-\d{2}-\d{2}")),
    ("%d-%b-%y", re.compile(r"\d{1,2}-[A-Z][a-z]{2}-\d{2}")),
)
TECHNICIAN = {tech.name: tech for tech in catalogue.TECHNICIANS}


def read_export(out: Path) -> list[list[str]]:
    path = out / "raw" / "maintenance" / EXPORT_NAME
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.reader(file))


def date_style_of(cell: str) -> int:
    (style,) = [
        i for i, (_, pattern) in enumerate(DATE_STYLES) if pattern.fullmatch(cell)
    ]
    return style


def parse_date(cell: str) -> dt.date:
    return dt.datetime.strptime(cell, DATE_STYLES[date_style_of(cell)][0]).date()


def parse_parts(cell: str) -> dict[str, int]:
    if not cell:
        return {}
    pairs = (item.rsplit(" x", 1) for item in cell.split("; "))
    return {PART_OF_ALIAS[alias]: int(qty) for alias, qty in pairs}


def rows_by_ref(rows: list[list[str]]) -> dict[str, list[list[str]]]:
    grouped: dict[str, list[list[str]]] = {}
    for row in rows[1:]:
        grouped.setdefault(row[7], []).append(row)
    return grouped


def incidents_of_row(row: list[str], truth, mention) -> list[Truth]:
    """The exported incidents that happened on the row's date on its machine."""
    return [
        truth[i]
        for i in mention.incidents
        if truth[i].start.date() == parse_date(row[0])
        and truth[i].asset == ASSET_OF_REF[row[1]]
    ]


def row_matches(row: list[str], incident: Truth) -> bool:
    """True when every cell but the date, downtime and ref fits the incident."""
    _, machine, problem, action, parts, tech, _, _ = row
    problems = {incident.symptom}
    if incident.alarm:
        problems |= {f"{v}: {incident.symptom}" for v in ALARM_VARIANTS[incident.alarm]}
    person = TECHNICIAN[incident.technician]
    return (
        ASSET_OF_REF[machine] == incident.asset
        and problem in problems
        and action == repl(incident.procedure)
        and parse_parts(parts) == incident.parts
        and tech in (person.name, *person.short_forms)
    )


@pytest.fixture(scope="module")
def export(notes):
    out, mentions = notes
    return read_export(out), mentions[f"raw/maintenance/{EXPORT_NAME}"]


def test_the_export_header_is_exact_and_it_has_one_row_per_incident_plus_four(export):
    rows, mention = export
    assert rows[0] == EXPORT_HEADER
    assert len(rows) - 1 == len(mention.incidents) + 4
    assert all(len(row) == 8 for row in rows)


def test_every_row_describes_an_incident_that_the_export_holds(export, truth):
    rows, mention = export
    for row in rows[1:]:
        candidates = incidents_of_row(row, truth, mention)
        assert any(row_matches(row, incident) for incident in candidates), row


def test_each_incident_has_its_own_work_order_number_and_four_are_repeated(export):
    rows, mention = export
    grouped = rows_by_ref(rows)
    assert all(re.fullmatch(r"WO-\d{4}", ref) for ref in grouped)
    assert len(grouped) == len(mention.incidents)
    assert sorted(len(group) for group in grouped.values()).count(2) == 4
    assert {len(group) for group in grouped.values()} == {1, 2}


@pytest.mark.parametrize(("u", "style"), [(0.59, 0), (0.61, 1), (0.89, 1), (0.91, 2)])
def test_dates_are_60_percent_day_first_30_iso_and_10_day_month_name(
    world, tmp_path, u, style
):
    out, _ = render_notes_with(world, u, tmp_path)
    rows = read_export(out)
    for group in rows_by_ref(rows).values():
        original_styles = {date_style_of(row[0]) for row in group}
        if len(group) == 1:
            assert original_styles == {style}
        else:
            assert len(original_styles) == 2 and style in original_styles
            assert len({parse_date(row[0]) for row in group}) == 1


def test_a_date_with_a_month_name_has_no_leading_zero_and_a_two_digit_year(
    world, tmp_path
):
    out, _ = render_notes_with(world, 0.91, tmp_path)
    cells = [row[0] for row in read_export(out)[1:] if date_style_of(row[0]) == 2]
    assert cells and all(re.fullmatch(r"[1-9]\d?-[A-Z][a-z]{2}-26", c) for c in cells)
    assert any(
        c.startswith(("1-", "2-", "3-")) and len(c.split("-")[0]) == 1 for c in cells
    )


def test_a_repeated_row_keeps_everything_but_the_date_format_and_the_downtime(
    world, tmp_path_factory
):
    """At 0.5 every downtime is a plain integer, so a repeat differs from its
    original by 5 to 30 minutes. Twenty renders, each with its own choices, show
    every step of that range."""
    steps = set()
    for seed in range(20):
        out = tmp_path_factory.mktemp("repeat")
        render_notes(world, FixedRandom(0.5, seed=seed), out)
        repeats = [g for g in rows_by_ref(read_export(out)).values() if len(g) == 2]
        assert len(repeats) == 4
        for first, second in repeats:
            for column in (1, 2, 3, 4, 5, 7):
                assert first[column] == second[column]
            assert date_style_of(first[0]) != date_style_of(second[0])
            steps.add(abs(int(first[6]) - int(second[6])))
    assert steps == {5, 10, 15, 20, 25, 30}


def test_the_rows_run_by_date_apart_from_three_swapped_neighbours(world, tmp_path):
    out, _ = render_notes_with(world, 0.5, tmp_path)
    days = [parse_date(row[0]) for row in read_export(out)[1:]]
    inversions = sum(
        1
        for i in range(len(days))
        for j in range(i + 1, len(days))
        if days[i] > days[j]
    )
    assert inversions == 3


@pytest.mark.parametrize("seed", range(12))
def test_the_export_has_three_separate_swapped_pairs_whatever_the_draws(
    world, tmp_path, seed
):
    """Swapping two rows with the same date would not show, and two swaps that touch
    would run into each other. Three separate swaps of rows with different dates leave
    the dates falling back exactly three times."""
    render_notes(world, FixedRandom(0.5, seed=seed), tmp_path)
    days = [parse_date(row[0]) for row in read_export(tmp_path)[1:]]
    assert descents(days) == 3


@pytest.mark.parametrize(("u", "prefixed"), [(0.49, True), (0.51, False)])
def test_half_the_problem_descriptions_start_with_the_alarm_variant(
    world, truth, tmp_path, u, prefixed
):
    out, mentions = render_notes_with(world, u, tmp_path)
    rows = read_export(out)
    mention = mentions[f"raw/maintenance/{EXPORT_NAME}"]
    for row in rows[1:]:
        (incident,) = {
            i.id: i
            for i in incidents_of_row(row, truth, mention)
            if row_matches(row, i)
        }.values()
        assert (row[2] != incident.symptom) == (prefixed and incident.alarm is not None)


@pytest.mark.parametrize(("u", "full_name"), [(0.69, True), (0.71, False)])
def test_the_tech_is_the_full_name_70_percent_of_the_time(
    world, tmp_path, u, full_name
):
    out, _ = render_notes_with(world, u, tmp_path)
    for row in read_export(out)[1:]:
        assert (row[5] in TECHNICIAN) == full_name


@pytest.mark.parametrize(
    ("u", "shape"),
    [
        (0.049, r""),
        (0.051, r"approx \d+"),
        (0.15, r"approx \d+"),
        (0.5, r"\d+"),
    ],
)
def test_downtime_is_blank_5_percent_of_the_time_otherwise_a_number_or_approx(
    world, truth, tmp_path, u, shape
):
    out, mentions = render_notes_with(world, u, tmp_path)
    mention = mentions[f"raw/maintenance/{EXPORT_NAME}"]
    for group in rows_by_ref(read_export(out)).values():
        original = group[0]
        (incident,) = {
            i.id: i
            for i in incidents_of_row(original, truth, mention)
            if row_matches(original, i)
        }.values()
        if len(group) == 1:
            assert re.fullmatch(shape, original[6]), original
            if original[6].startswith("approx"):
                assert abs(int(original[6][7:]) - incident.downtime) <= 15
            elif original[6]:
                assert int(original[6]) == incident.downtime
        else:
            # A repeat is a re-keyed entry with a plain number in every case.
            for row in group:
                assert re.fullmatch(r"\d+", row[6]) or re.fullmatch(shape, row[6])


def test_the_export_mentions_the_incidents_assets_alarms_and_parts_of_its_rows(
    export, truth
):
    rows, mention = export
    prefix_codes = {
        variant: code
        for code, variants in ALARM_VARIANTS.items()
        for variant in variants
    }
    alarms, parts = set(), set()
    for row in rows[1:]:
        head, separator, _ = row[2].partition(": ")
        if separator and head in prefix_codes:
            alarms.add(prefix_codes[head])
        parts |= set(parse_parts(row[4]))
    assert mention.alarm_codes == sorted(alarms)
    assert mention.parts == sorted(parts)
    assert mention.assets == sorted({ASSET_OF_REF[row[1]] for row in rows[1:]})
    assert mention.conflicts == []
    assert set(mention.incidents) <= set(truth)


@pytest.mark.parametrize("u", [0.5, 0.9])
def test_every_incident_is_mentioned_by_a_handover_or_an_export_row(
    world, truth, tmp_path, u
):
    _, mentions = render_notes_with(world, u, tmp_path)
    mentioned = {i for mention in mentions.values() for i in mention.incidents}
    assert mentioned == set(truth)


# --- Loose notes ----------------------------------------------------------------------

LOOSE_NAME = re.compile(r"note_from_([a-z]+)_([A-Z]+\d?)_([a-z]+)( \(2\))?\.txt")
MACHINE_TAG = {
    "CNC-01": "CNC1",
    "CNC-02": "CNC2",
    "HP-01": "HP",
    "RW-01": "RW",
    "LT-01": "LT",
}


def loose_notes(out: Path) -> list[Path]:
    files = sorted(
        path
        for path in (out / "raw" / "maintenance").iterdir()
        if path.suffix == ".txt"
    )
    assert len(files) == 5
    return files


def longest_downtime(truth: dict[str, Truth]) -> list[str]:
    ranked = sorted(truth.values(), key=lambda i: (-i.downtime, i.id))
    return [incident.id for incident in ranked[:5]]


def sentences(text: str) -> list[str]:
    return re.findall(r"[^.!?]+[.!?](?=\s|$)", text.strip())


def test_the_maintenance_folder_holds_the_export_and_five_loose_notes(notes):
    out, _ = notes
    names = sorted(path.name for path in (out / "raw" / "maintenance").iterdir())
    assert len(names) == 6 and EXPORT_NAME in names
    assert all(LOOSE_NAME.fullmatch(name) for name in names if name != EXPORT_NAME)


def test_the_loose_notes_are_for_the_five_incidents_with_the_longest_downtime(
    notes, truth
):
    out, mentions = notes
    found = []
    for path in loose_notes(out):
        (incident,) = mentions[file_key(out, path)].incidents
        found.append(incident)
    assert sorted(found) == sorted(longest_downtime(truth))


def test_a_loose_note_is_named_after_the_first_name_the_machine_and_a_keyword(
    notes, truth
):
    """note_from_rajesh_HP_seal.txt: the technician's first name from the catalogue,
    the machine type and the word that names the cause."""
    out, mentions = notes
    for path in loose_notes(out):
        (incident,) = (truth[i] for i in mentions[file_key(out, path)].incidents)
        first, machine, keyword, _ = LOOSE_NAME.fullmatch(path.name).groups()
        first_name = TECHNICIAN[incident.technician].first_name
        assert first == first_name.lower().replace(" ", "")
        assert machine == MACHINE_TAG[incident.asset]
        assert keyword == incident.failure_mode.split("-")[1].lower()


def test_the_first_name_in_a_loose_note_name_is_the_catalogue_field(
    world, truth, tmp_path, monkeypatch
):
    """Every technician gets a first name that is none of their short forms, and
    the file names follow the field."""
    call_signs = (
        "Alpha",
        "Bravo",
        "Charlie",
        "Delta",
        "Echo",
        "Foxtrot",
        "Golf",
        "Hotel",
    )
    renamed = {
        name: dataclasses.replace(spec, first_name=call_sign)
        for (name, spec), call_sign in zip(
            catalogue.TECHNICIAN_BY_NAME.items(), call_signs, strict=True
        )
    }
    monkeypatch.setattr(catalogue, "TECHNICIAN_BY_NAME", renamed)
    out, mentions = render_notes_with(world, 0.5, tmp_path)
    for path in loose_notes(out):
        (incident,) = (truth[i] for i in mentions[file_key(out, path)].incidents)
        first = LOOSE_NAME.fullmatch(path.name).group(1)
        assert first == renamed[incident.technician].first_name.lower()


def test_a_loose_note_is_four_to_eight_plain_sentences(notes):
    out, _ = notes
    for path in loose_notes(out):
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n") and "\n" not in text.strip()
        assert 4 <= len(sentences(text)) <= 8, text
        assert all(s.strip()[0].isupper() for s in sentences(text)), text


def test_loose_notes_use_every_sentence_count_from_four_to_eight(
    world, tmp_path_factory
):
    counts = set()
    for seed in range(20):
        out = tmp_path_factory.mktemp("sentences")
        render_notes(world, FixedRandom(0.5, seed=seed), out)
        counts |= {
            len(sentences(p.read_text(encoding="utf-8"))) for p in loose_notes(out)
        }
    assert counts == {4, 5, 6, 7, 8}


def test_a_loose_note_names_the_real_asset_alarm_parts_and_root_cause(notes, truth):
    out, mentions = notes
    for path in loose_notes(out):
        text = path.read_text(encoding="utf-8")
        mention = mentions[file_key(out, path)]
        (incident,) = (truth[i] for i in mention.incidents)
        assert any(ref in text for ref in ASSET_REFS[incident.asset])
        assert FAILURE_MODE[incident.failure_mode].name.lower() in text
        if incident.alarm:
            assert any(variant in text for variant in ALARM_VARIANTS[incident.alarm])
        for part_no in incident.parts:
            assert any(alias in text for alias in PART[part_no].aliases), part_no
        assert mention.assets == [incident.asset]
        assert mention.alarm_codes == ([incident.alarm] if incident.alarm else [])
        assert mention.parts == sorted(incident.parts)
        assert mention.conflicts == []


def test_every_incident_is_mentioned_by_some_handover_export_row_or_loose_note(
    notes, truth
):
    _, mentions = notes
    mentioned = {i for mention in mentions.values() for i in mention.incidents}
    assert mentioned == set(truth)
