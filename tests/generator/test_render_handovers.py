"""Tests for the generated shift handover notes and the wording in them.

Every render goes to a temporary folder. Each percentage is a chance per item, so the
tests use a random source that always returns one chosen number, which makes each
case exact instead of counting samples."""

import datetime as dt
import random
import re
from pathlib import Path

import pytest
from conftest import (
    ALARM_VARIANTS,
    ASSET_OF_REF,
    ASSET_REFS,
    EXPORT_NAME,
    GRIPPER_PART,
    PART,
    PART_OF_ALIAS,
    FixedRandom,
    render_notes_with,
    repl,
)

from app.generator import catalogue
from app.generator.mentions import FileMentions, file_key
from app.generator.notes_incidents import Incident
from app.generator.notes_text import add_typos, shorthand, unique_name
from app.generator.render_handovers import handover_slots
from app.generator.render_notes import render_notes

TECHNICIAN_OF_SHORT_FORM = {
    short: tech for tech in catalogue.TECHNICIANS for short in tech.short_forms
}
NEXT_SHIFT = {"A": "B", "B": "C", "C": "A"}

# --- Shift handovers ----------------------------------------------------------------

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
MONTH_NUMBER = {month: number for number, month in enumerate(MONTHS, start=1)}
SHIFT_OF_TOKEN = {"A": "A", "B": "B", "C": "C", "night": "C"}
NAME_STYLES = (
    re.compile(r"handover_(\d{4})-(\d{2})-(\d{2})_(A|B|C|night)( \(2\))?\.txt"),
    re.compile(r"HO (\d{2})-(\d{2}) (A|B|C|night) shift( \(2\))?\.txt"),
    re.compile(
        r"shift notes (\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r" (A|B|C|night)( \(2\))?\.txt"
    ),
    re.compile(r"Handover_(\d{4})(\d{2})(\d{2})_(A|B|C|night)( \(2\))?\.txt"),
)
HEADER_PATTERN = re.compile(
    r"Shift handover (\d{2}/\d{2}/\d{4}|\d{1,2} [A-Z][a-z]{2}|\d{4}-\d{2}-\d{2})"
    r" (C shift|night|Shift A|B)"
)
FROM_TO_PATTERN = re.compile(r"From: (.+)   To: (.+)")
SHIFT_OF_LABEL = {"Shift A": "A", "B": "B", "C shift": "C", "night": "C"}
FILLERS = {
    "CNC 1 running fine, tool wear ok": (["CNC-01"], []),
    "5S done on robot cell": (["RW-01"], []),
    "coolant topped up on MC-2": (["CNC-02"], []),
    "leak tester master part passed at start of shift": (["LT-01"], []),
    "press warm-up normal": (["HP-01"], []),
    "nothing major, all machines running": ([], []),
    "chk air pressure at LT1, ok": (["LT-01"], []),
    "pls f/u on stores count for seals": ([], []),
    "robot cell guard door sticking a bit, f/u tmr": (["RW-01"], []),
}
GRIPPER_FILLER = "waiting on gripper fingers, stores says none left"
ITEM_PATTERN = re.compile(
    r"(?P<machine>.+?) stopped (?P<time>\d{4})(?: w/ (?P<alarm>.+?))?, "
    r"(?P<symptom>.+?)\. (?P<action>.+?)(?: \((?P<parts>.+?)\))?, "
    r"(?P<closer>tested ok|running ok now|test run ok)\. "
    r"Down (?P<down>approx \d+h|\d+ min)\.(?: (?P<follow>.+)\.)?"
)


def slot_from_name(name: str) -> tuple[dt.date, str]:
    for index, style in enumerate(NAME_STYLES):
        match = style.fullmatch(name)
        if match is None:
            continue
        *fields, token = match.groups()[:-1]  # the last group is the "(2)" suffix
        if index in (0, 3):
            year, month, day = (int(field) for field in fields)
        elif index == 1:
            year, (month, day) = 2026, (int(field) for field in fields)
        else:
            year, month, day = 2026, MONTH_NUMBER[fields[1]], int(fields[0])
        return dt.date(year, month, day), SHIFT_OF_TOKEN[token]
    raise AssertionError(f"not a handover file name: {name!r}")


def name_style(name: str) -> int:
    return next(i for i, style in enumerate(NAME_STYLES) if style.fullmatch(name))


def slot_from_header(line: str) -> tuple[dt.date, str]:
    match = HEADER_PATTERN.fullmatch(line)
    assert match, line
    day_text, label = match.groups()
    if "/" in day_text:
        day, month, year = (int(part) for part in day_text.split("/"))
    elif "-" in day_text:
        year, month, day = (int(part) for part in day_text.split("-"))
    else:
        number, month_name = day_text.split()
        year, month, day = 2026, MONTH_NUMBER[month_name], int(number)
    return dt.date(year, month, day), SHIFT_OF_LABEL[label]


def date_style(line: str) -> int:
    day_text = HEADER_PATTERN.fullmatch(line).group(1)
    return 0 if "/" in day_text else 2 if "-" in day_text else 1


def handover_files(out: Path) -> list[Path]:
    return sorted((out / "raw" / "handovers").iterdir())


def items_of(text: str) -> list[str]:
    return [line[2:] for line in text.splitlines() if line.startswith("- ")]


def handover_mentions(out: Path, mentions) -> dict[Path, FileMentions]:
    return {path: mentions[file_key(out, path)] for path in handover_files(out)}


def test_there_are_exactly_90_handover_notes(notes):
    out, _ = notes
    files = handover_files(out)
    assert len(files) == 90
    assert all(path.suffix == ".txt" for path in files)


def test_the_file_names_use_the_four_styles_and_all_four_appear(notes):
    out, _ = notes
    styles = {name_style(path.name) for path in handover_files(out)}
    assert styles == {0, 1, 2, 3}


def test_a_name_and_its_header_give_the_same_date_and_shift(notes):
    out, _ = notes
    for path in handover_files(out):
        header = path.read_text(encoding="utf-8").split("\n")[0]
        assert slot_from_name(path.name) == slot_from_header(header), path.name


def test_every_note_is_for_a_different_slot(notes):
    out, _ = notes
    slots = [slot_from_name(path.name) for path in handover_files(out)]
    assert len(set(slots)) == 90


def test_the_notes_name_the_shift_c_two_ways_and_dates_three_ways(notes):
    out, _ = notes
    headers = [
        p.read_text(encoding="utf-8").split("\n")[0] for p in handover_files(out)
    ]
    labels = {HEADER_PATTERN.fullmatch(header).group(2) for header in headers}
    assert labels == {"Shift A", "B", "C shift", "night"}
    assert {date_style(header) for header in headers} == {0, 1, 2}
    names = [path.name for path in handover_files(out)]
    assert any("night" in name for name in names)
    assert any("night" not in name for name in names)


def test_a_note_has_the_brief_layout(notes):
    out, _ = notes
    for path in handover_files(out):
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        assert HEADER_PATTERN.fullmatch(lines[0]), path.name
        assert FROM_TO_PATTERN.fullmatch(lines[1]), path.name
        assert lines[2] == "" and lines[-1] == "" and lines[-3] == ""
        assert lines[-2].startswith("Signed ")
        body = lines[3:-3]
        assert body and all(line.startswith("- ") for line in body), path.name


def test_from_is_on_the_shift_to_is_on_the_next_shift_and_the_sender_signs(notes):
    out, _ = notes
    for path in handover_files(out):
        lines = path.read_text(encoding="utf-8").split("\n")
        _, shift = slot_from_header(lines[0])
        sender, receiver = FROM_TO_PATTERN.fullmatch(lines[1]).groups()
        signer = lines[-2].removeprefix("Signed ")
        assert TECHNICIAN_OF_SHORT_FORM[sender].shift == shift
        assert TECHNICIAN_OF_SHORT_FORM[receiver].shift == NEXT_SHIFT[shift]
        assert TECHNICIAN_OF_SHORT_FORM[signer] == TECHNICIAN_OF_SHORT_FORM[sender]


def test_an_incident_chosen_for_handovers_is_in_the_note_for_its_slot(notes, truth):
    out, mentions = notes
    seen = set()
    for path, mention in handover_mentions(out, mentions).items():
        for incident in mention.incidents:
            assert truth[incident].slot == slot_from_name(path.name), incident
            seen.add(incident)
    assert seen


def test_each_note_has_one_to_three_filler_items(notes):
    out, mentions = notes
    for path, mention in handover_mentions(out, mentions).items():
        fillers = len(items_of(path.read_text(encoding="utf-8"))) - len(
            mention.incidents
        )
        assert 1 <= fillers <= 3, path.name


@pytest.mark.parametrize("u", [0.3, 0.5, 0.65])
def test_incident_items_follow_the_example_and_describe_their_incident(
    world, truth, tmp_path, u
):
    """With no typos (u is above 1/12), an incident item is exactly the brief's
    shape. The machine, alarm, parts and downtime come from the incident."""
    out, mentions = render_notes_with(world, u, tmp_path)
    checked = 0
    for path, mention in handover_mentions(out, mentions).items():
        found = []
        for item in items_of(path.read_text(encoding="utf-8")):
            match = ITEM_PATTERN.fullmatch(item)
            if match is None:
                assert item in FILLERS or item == GRIPPER_FILLER, item
                continue
            (incident,) = [
                truth[i]
                for i in mention.incidents
                if truth[i].start.strftime("%H%M") == match["time"]
            ]
            found.append(incident.id)
            says = ASSET_OF_REF[match["machine"]]
            wrong = {c.incident: c.says_asset for c in mention.conflicts}
            assert says == wrong.get(incident.id, incident.asset)
            if incident.alarm is None:
                assert match["alarm"] is None
            else:
                assert match["alarm"] in ALARM_VARIANTS[incident.alarm]
            assert match["symptom"] == incident.symptom
            assert match["action"] == repl(incident.procedure)
            shown = (match["parts"] or "").split(", ") if match["parts"] else []
            assert bool(shown) == bool(incident.parts)
            for shown_part in shown:
                alias, _, qty = shown_part.partition(" x")
                part_no = PART_OF_ALIAS[alias]
                assert incident.parts[part_no] == (int(qty) if qty else 1)
                assert (qty != "") == (incident.parts[part_no] > 1)
            down = match["down"]
            if down.startswith("approx"):
                assert int(down[7:-1]) == max(1, round(incident.downtime / 60))
            else:
                assert int(down.removesuffix(" min")) == incident.downtime
            if match["follow"]:
                assert re.search(r"pls|chk|tmr|f/u", match["follow"])
            checked += 1
        assert sorted(found) == mention.incidents, path.name
    assert checked > 0


def test_the_downtime_is_written_as_hours_or_minutes(world, tmp_path_factory):
    for u, pattern in ((0.49, r"approx \d+h\."), (0.51, r"\d+ min\.")):
        out, _ = render_notes_with(world, u, tmp_path_factory.mktemp("down"))
        texts = [p.read_text(encoding="utf-8") for p in handover_files(out)]
        items = [i for t in texts for i in items_of(t) if " stopped " in i]
        assert items and all(re.search(rf"Down {pattern}", item) for item in items)


def test_the_brief_shorthand_appears_in_the_notes(world, tmp_path):
    out, _ = render_notes_with(world, 0.3, tmp_path)
    text = " ".join(p.read_text(encoding="utf-8") for p in handover_files(out))
    for word in ("repl", "chk", "brg", "w/", "approx", "pls", "tmr", "f/u"):
        assert re.search(
            rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", text, re.IGNORECASE
        ), word


# The export draw comes first and the handover draw second, for each incident in turn.
PLACEMENTS = (
    (0.79, 0.69, True, True),  # under 0.8 and under 0.7: both
    (0.81, 0.69, False, True),  # handover only
    (0.79, 0.71, True, False),  # export only
    (0.81, 0.71, True, False),  # neither is chosen, so the export takes it
)


def placement_draws(count: int) -> list[float]:
    return [
        draw
        for index in range(count)
        for draw in PLACEMENTS[index % len(PLACEMENTS)][:2]
    ]


def test_each_incident_goes_to_the_export_or_a_handover_or_both_and_never_neither(
    world, truth, tmp_path
):
    """The export takes an incident with probability 0.8 and a handover with 0.7.
    An incident chosen for neither goes to the export. The draws bracket both
    thresholds, and the four patterns cover every combination."""
    out, mentions = (
        tmp_path,
        render_notes(world, FixedRandom(0.5, placement_draws(len(truth))), tmp_path),
    )
    handed_over = {
        incident
        for mention in handover_mentions(out, mentions).values()
        for incident in mention.incidents
    }
    exported = set(mentions[f"raw/maintenance/{EXPORT_NAME}"].incidents)
    ordered = sorted(truth)
    for flag, found in ((2, exported), (3, handed_over)):
        expected = {
            incident
            for index, incident in enumerate(ordered)
            if PLACEMENTS[index % len(PLACEMENTS)][flag]
        }
        assert found == expected
        assert 0 < len(expected) < len(truth)
    assert exported | handed_over == set(truth)


@pytest.mark.parametrize("u", [0.05, 0.079])
def test_the_wrong_machine_is_written_in_eight_percent_of_incident_items(
    world, truth, tmp_path, u
):
    """Under 0.08 every item that can name a wrong machine does: the other
    machining centre, or CNC 1 for the press. Robot and tester items cannot. The
    record names the incident and the machine the note says."""
    out, mentions = render_notes_with(world, u, tmp_path)
    expected = {"CNC-01": "CNC-02", "CNC-02": "CNC-01", "HP-01": "CNC-01"}
    recorded = {}
    for path, mention in handover_mentions(out, mentions).items():
        items = items_of(path.read_text(encoding="utf-8"))
        for conflict in mention.conflicts:
            incident = truth[conflict.incident]
            recorded[incident.id] = conflict.says_asset
            assert conflict.says_asset in mention.assets
            assert conflict.says_asset != incident.asset
            # The item for the incident is the one with its start time.
            (item,) = [i for i in items if re.search(rf"\b{incident.start:%H%M}\b", i)]
            assert any(ref in item for ref in ASSET_REFS[conflict.says_asset])
            if incident.asset == "HP-01":
                assert "CNC 1" in item
    for incident in truth.values():
        if incident.asset in expected:
            assert recorded[incident.id] == expected[incident.asset]
        else:
            assert incident.id not in recorded


@pytest.mark.parametrize("u", [0.081, 0.5])
def test_no_wrong_machine_at_or_above_eight_percent(world, tmp_path, u):
    out, mentions = render_notes_with(world, u, tmp_path)
    assert all(m.conflicts == [] for m in handover_mentions(out, mentions).values())
    assert any(m.incidents for m in handover_mentions(out, mentions).values())


@pytest.mark.parametrize(("u", "with_follow_up"), [(0.39, True), (0.41, False)])
def test_an_item_ends_with_a_follow_up_with_probability_0_4(
    world, tmp_path, u, with_follow_up
):
    out, _ = render_notes_with(world, u, tmp_path)
    items = [
        item
        for path in handover_files(out)
        for item in items_of(path.read_text(encoding="utf-8"))
        if ITEM_PATTERN.fullmatch(item)
    ]
    assert items
    for item in items:
        assert bool(ITEM_PATTERN.fullmatch(item)["follow"]) == with_follow_up


def test_an_item_names_each_part_with_probability_0_6_and_at_least_one(
    world, truth, tmp_path_factory
):
    for u, every_part in ((0.59, True), (0.61, False)):
        out, mentions = render_notes_with(world, u, tmp_path_factory.mktemp("parts"))
        seen = 0
        for mention in handover_mentions(out, mentions).values():
            for incident in (truth[i] for i in mention.incidents):
                shown = set(mention.parts) & set(incident.parts)
                if len(incident.parts) > 1 and len(mention.incidents) == 1:
                    seen += 1
                    if every_part:
                        assert shown == set(incident.parts)
                    else:
                        assert len(shown) == 1
        assert seen


def test_fillers_come_from_the_brief_list_and_their_entities_are_mentioned(
    world, tmp_path
):
    """At 0.9 no incident is chosen for handovers and nothing is mistyped, so a
    note holds only filler lines and each one is word for word the brief's."""
    out, mentions = render_notes_with(world, 0.9, tmp_path)
    appearing = set()
    for path, mention in handover_mentions(out, mentions).items():
        items = items_of(path.read_text(encoding="utf-8"))
        assert 1 <= len(items) <= 3
        assert mention.incidents == [] and mention.conflicts == []
        assets, parts = set(), set()
        for item in items:
            appearing.add(item)
            if item == GRIPPER_FILLER:
                parts.add(GRIPPER_PART)
            else:
                assets |= set(FILLERS[item][0])
        assert mention.assets == sorted(assets) and mention.parts == sorted(parts)
    assert set(FILLERS) <= appearing and GRIPPER_FILLER in appearing


def test_the_gripper_line_is_used_only_after_the_first_incident_that_used_the_part(
    world, truth, tmp_path
):
    first = min(i.start.date() for i in truth.values() if GRIPPER_PART in i.parts)
    out, _ = render_notes_with(world, 0.9, tmp_path)
    dates = [
        slot_from_name(path.name)[0]
        for path in handover_files(out)
        if GRIPPER_FILLER in items_of(path.read_text(encoding="utf-8"))
    ]
    assert dates and all(date > first for date in dates)


def test_a_handover_mentions_the_alarms_of_its_incidents_and_a_part_per_item(
    notes, truth
):
    out, mentions = notes
    for mention in handover_mentions(out, mentions).values():
        incidents = [truth[i] for i in mention.incidents]
        assert mention.alarm_codes == sorted({i.alarm for i in incidents if i.alarm})
        allowed_parts = {p for i in incidents for p in i.parts} | {GRIPPER_PART}
        assert set(mention.parts) <= allowed_parts


# --- The slots of the notes ---------------------------------------------------------


def incident_in_slot(number: int) -> Incident:
    """A made-up incident. Slot number n is shift A, B or C of day n // 3 from 1 April,
    and the slot is all that handover_slots reads."""
    day = dt.date(2026, 4, 1) + dt.timedelta(days=number // 3)
    return Incident(
        id=f"INC-TEST-{number:03d}",
        start=dt.datetime.combine(day, dt.time(12, 0), tzinfo=catalogue.SGT),
        shift="ABC"[number % 3],
        asset="HP-01",
        alarm=None,
        failure_mode="FM-SEAL-WEAR",
        procedure="Replace main cylinder seal kit",
        parts=(),
        downtime=60,
        symptom="oil leaking under the ram",
        technician="Rajesh Pillai",
    )


@pytest.mark.parametrize("count", [0, 5, 89])
def test_the_slots_are_filled_up_to_90_with_empty_random_slots(count):
    incidents = [incident_in_slot(n) for n in range(count)]
    slots = handover_slots(random.Random(1), incidents)
    assert len(slots) == 90
    assert [len(here) for here in slots.values()].count(0) == 90 - count
    assert list(slots) == sorted(slots)
    for incident in incidents:
        assert slots[incident.start.date(), incident.shift] == [incident]


@pytest.mark.parametrize("count", [90, 91, 95, 140])
def test_no_slot_is_added_when_the_incidents_already_fill_90_or_more(count):
    """With more than 90 incident slots the notes are one for each of them, and the
    random generator is not used at all."""
    incidents = [incident_in_slot(n) for n in range(count)]
    rng = random.Random(1)
    state = rng.getstate()
    slots = handover_slots(rng, incidents)
    assert len(slots) == count
    assert all(len(here) == 1 for here in slots.values())
    assert [here[0] for here in slots.values()] == incidents
    assert rng.getstate() == state


def test_two_incidents_in_one_slot_share_one_note():
    first, second = incident_in_slot(7), incident_in_slot(7)
    slots = handover_slots(random.Random(1), [first, second])
    assert slots[first.start.date(), first.shift] == [first, second]
    assert len(slots) == 90


# --- Typos, shorthand and names ----------------------------------------------------


def test_a_typo_swaps_two_adjacent_letters_when_random_is_under_one_twelfth():
    text = "hydraulic pressure dropping mid stroke"
    typed = add_typos(FixedRandom(0.08), text)
    plain = add_typos(FixedRandom(0.09), text)
    assert plain == text
    assert typed != text
    for original, changed in zip(text.split(), typed.split(), strict=True):
        if original != changed:
            differing = [i for i in range(len(original)) if original[i] != changed[i]]
            assert len(differing) == 2 and differing[1] == differing[0] + 1
            assert original[differing[0]] == changed[differing[1]]
            assert original[differing[1]] == changed[differing[0]]
            assert differing[0] >= 1


def test_a_typo_never_touches_an_id_alias_alarm_variant_or_part_reference():
    references = [
        *ASSET_OF_REF,
        *(v for variants in ALARM_VARIANTS.values() for v in variants),
        *(PART),
        *PART_OF_ALIAS,
    ]
    for reference in references:
        text = f"before {reference} after"
        typed = add_typos(FixedRandom(0.01), text)
        assert reference in typed, reference


def test_a_typo_changes_the_plain_words_around_a_reference():
    typed = add_typos(
        FixedRandom(0.01), "Press 2 stopped 0215 w/ ERR 217, oil under ram"
    )
    assert typed.startswith("Press 2 ") and " ERR 217, " in typed
    assert "stopped" not in typed and "under" not in typed


def test_shorthand_shortens_replace_check_bearing_and_and():
    assert shorthand("Replace spindle bearing set") == "Repl spindle brg set"
    assert (
        shorthand("Check spindle vibration from the HMI")
        == "Chk spindle vibration from the HMI"
    )
    assert shorthand("Reset and test relief valve") == "Reset & test relief valve"


def test_a_second_note_with_the_same_name_gets_a_two_in_brackets():
    assert unique_name("HO 04-04 B shift.txt", set()) == "HO 04-04 B shift.txt"
    taken = {"HO 04-04 B shift.txt"}
    assert unique_name("HO 04-04 B shift.txt", taken) == "HO 04-04 B shift (2).txt"
