"""Write the maintenance files: the log export and the loose notes.

The export is a CSV from the maintenance system. It has some repeated entries and a
few rows out of order. The loose notes are short paragraphs a technician left after
the longest jobs.

write_export() writes the CSV and write_loose_notes() writes the notes. Both return
what each file mentions.
"""

import csv
import datetime as dt
import random
from dataclasses import dataclass
from pathlib import Path

from app.generator import catalogue as cat
from app.generator.mentions import FileMentions, Mentions, file_key
from app.generator.notes_incidents import Incident, alarm_ref, machine_ref, part_ref
from app.generator.notes_text import MONTHS, shorthand, unique_name
from app.generator.row_swaps import swap_neighbours
from app.generator.world import Technician, World, affected_component

EXPORT_FILE = "maint_log_export_2026.csv"
EXPORT_HEADER = (
    "Date",
    "Mach.",
    "Problem desc",
    "Action taken",
    "Parts",
    "Tech",
    "Downtime (min)",
    "Ref",
)
LOOSE_NOTE_COUNT = 5  # one note for each of the longest jobs
EXPORT_REPEATS = 4  # entries keyed in twice
EXPORT_SWAPS = 3  # pairs of neighbouring rows put in the wrong order
PREFIX_PROBABILITY = 0.5  # the problem text starts with the alarm
FULL_NAME_PROBABILITY = 0.7  # the technician is given by full name
BLANK_DOWNTIME_PROBABILITY = 0.05
APPROX_DOWNTIME_PROBABILITY = 0.2

# Three date styles, used in a 60, 30 and 10 percent mix.
_DATE_STYLES = (
    lambda day: f"{day:%d/%m/%Y}",
    lambda day: f"{day:%Y-%m-%d}",
    lambda day: f"{day.day}-{MONTHS[day.month - 1]}-{day:%y}",
)
_DATE_WEIGHTS = (60, 30, 10)


@dataclass(frozen=True)
class _Row:
    incident: Incident
    style: int  # which date style
    cells: tuple[str, ...]  # the columns after the date
    alarm: str | None  # the alarm named in the problem text

    @property
    def values(self) -> list[str]:
        return [_DATE_STYLES[self.style](self.incident.start.date()), *self.cells]


def _day_of(row: _Row) -> dt.date:
    return row.incident.start.date()


def _downtime_cell(rng: random.Random, minutes: int) -> str:
    draw = rng.random()
    if draw < BLANK_DOWNTIME_PROBABILITY:
        return ""
    if draw < BLANK_DOWNTIME_PROBABILITY + APPROX_DOWNTIME_PROBABILITY:
        return f"approx {30 * round(minutes / 30)}"
    return str(minutes)


def _export_row(
    rng: random.Random, incident: Incident, ref: str, technician: Technician
) -> _Row:
    style = rng.choices(range(len(_DATE_STYLES)), weights=_DATE_WEIGHTS)[0]
    machine = machine_ref(rng, incident.asset)
    problem, alarm = incident.symptom, None
    if incident.alarm and rng.random() < PREFIX_PROBABILITY:
        problem, alarm = f"{alarm_ref(rng, incident.alarm)}: {problem}", incident.alarm
    parts = "; ".join(
        f"{part_ref(rng, part_no)} x{qty}" for part_no, qty in incident.parts
    )
    tech = (
        technician.name
        if rng.random() < FULL_NAME_PROBABILITY
        else rng.choice(technician.short_forms)
    )
    downtime = _downtime_cell(rng, incident.downtime)
    cells = (
        machine,
        problem,
        shorthand(incident.procedure),
        parts,
        tech,
        downtime,
        ref,
    )
    return _Row(incident, style, cells, alarm)


def _repeat(rng: random.Random, row: _Row) -> _Row:
    """The same entry keyed in again, with another date style and a downtime that is
    a little off."""
    style = rng.choice([s for s in range(len(_DATE_STYLES)) if s != row.style])
    minutes = row.incident.downtime + rng.choice((-1, 1)) * rng.randrange(5, 31, 5)
    return _Row(
        row.incident, style, (*row.cells[:5], str(minutes), row.cells[6]), row.alarm
    )


def write_export(
    world: World,
    rng: random.Random,
    out: Path,
    incidents: list[Incident],
    exported: list[Incident],
) -> Mentions:
    folder = out / "raw" / "maintenance"
    folder.mkdir(parents=True, exist_ok=True)
    people = {tech.name: tech for tech in world.technicians}
    # One work order number per incident, rising over time. Incidents missing from
    # the export leave gaps.
    numbers = sorted(rng.sample(range(1000, 10000), len(incidents)))
    refs = {i.id: f"WO-{number}" for i, number in zip(incidents, numbers, strict=True)}
    rows = [_export_row(rng, i, refs[i.id], people[i.technician]) for i in exported]
    rows += [
        _repeat(rng, rows[n]) for n in rng.sample(range(len(rows)), EXPORT_REPEATS)
    ]
    rows.sort(key=_day_of)
    swap_neighbours(rng, rows, key=_day_of, count=EXPORT_SWAPS)

    path = folder / EXPORT_FILE
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(EXPORT_HEADER)
        writer.writerows(row.values for row in rows)
    return {
        file_key(out, path): FileMentions(
            incidents=[row.incident.id for row in rows],
            assets=[row.incident.asset for row in rows],
            alarm_codes=[row.alarm for row in rows if row.alarm],
            parts=[part_no for row in rows for part_no, _ in row.incident.parts],
        )
    }


# The sentences a loose note may leave out, so a note has four to eight sentences.
_OPTIONAL_SENTENCES = (1, 4, 5, 7)


def _loose_note(rng: random.Random, incident: Incident) -> tuple[str, FileMentions]:
    """A note a technician leaves after a long job: what happened, what was found,
    what was replaced and what to check next time."""
    cause = cat.FAILURE_MODE_BY_ID[incident.failure_mode].props.name.lower()
    component = cat.COMPONENT_BY_ID[
        affected_component(incident.failure_mode, incident.asset)
    ].name.lower()
    alarm = alarm_ref(rng, incident.alarm) if incident.alarm else None
    parts = [(part_no, part_ref(rng, part_no), qty) for part_no, qty in incident.parts]
    replaced = " and ".join(
        alias + (f" x{qty}" if qty > 1 else "") for _, alias, qty in parts
    )
    start, hours = incident.start, max(1, round(incident.downtime / 60))

    sentences = [
        f"On {start.day} {MONTHS[start.month - 1]} "
        f"{machine_ref(rng, incident.asset)} stopped at about {start:%H%M}"
        + (f" with {alarm} on the screen." if alarm else "."),
        incident.symptom[0].upper() + incident.symptom[1:] + ".",
        rng.choice(
            (
                f"When I opened it up I found {cause} on the {component}.",
                f"The root cause was {cause}, found on the {component}.",
            )
        ),
        f"I replaced {replaced}."
        if parts
        else "No parts were needed, I only adjusted it and reset the fault.",
        f"It was down for about {hours} hour{'s' if hours != 1 else ''}.",
        rng.choice(
            (
                "After the repair I ran it for a while and it held.",
                "I ran a few parts through after the repair and all of them were fine.",
            )
        ),
        rng.choice(
            (
                f"Next time check the {component} first.",
                f"Next time I will check the {component} before the shift starts.",
            )
        ),
        "Please tell the next shift if it comes back.",
    ]
    kept = rng.sample(_OPTIONAL_SENTENCES, rng.randint(4, 8) - 4)
    text = " ".join(
        sentence
        for n, sentence in enumerate(sentences)
        if n not in _OPTIONAL_SENTENCES or n in kept
    )
    text += "\n"
    return text, FileMentions(
        incidents=[incident.id],
        assets=[incident.asset],
        alarm_codes=[incident.alarm] if alarm else [],
        parts=[part_no for part_no, _, _ in parts],
    )


def _machine_tag(asset_id: str) -> str:
    """A short machine tag for a file name. The two machining centres get a number
    because they share a prefix."""
    prefix, number = asset_id.split("-")
    shared = sum(asset.id.startswith(prefix + "-") for asset in cat.ASSETS) > 1
    return prefix + (str(int(number)) if shared else "")


def write_loose_notes(
    rng: random.Random, out: Path, incidents: list[Incident]
) -> Mentions:
    folder = out / "raw" / "maintenance"
    folder.mkdir(parents=True, exist_ok=True)
    longest = sorted(incidents, key=lambda i: (-i.downtime, i.id))[:LOOSE_NOTE_COUNT]
    taken: set[str] = set()
    mentions: Mentions = {}
    for incident in longest:
        first_name = cat.TECHNICIAN_BY_NAME[incident.technician].first_name
        first = first_name.lower().replace(" ", "")
        machine = _machine_tag(incident.asset)
        keyword = incident.failure_mode.split("-")[1].lower()
        name = unique_name(f"note_from_{first}_{machine}_{keyword}.txt", taken)
        taken.add(name)
        text, mention = _loose_note(rng, incident)
        path = folder / name
        path.write_text(text, encoding="utf-8", newline="\n")
        mentions[file_key(out, path)] = mention
    return mentions
