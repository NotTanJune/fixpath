"""Reads shift handovers and loose maintenance notes into incidents.

The model reads the note once and lists each breakdown or repair, copying every
mention as written. The code then works out the start time, downtime, alarm codes and
parts, and swaps any quote the model got wrong for the note line that names the
machine. Mentions it cannot match are left as written for a later step.

extract_note is the entry point. starts_handover tells the file router whether a
first line begins a shift handover.
"""

import datetime as dt
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import Settings
from app.ingest.evidence import quote
from app.ingest.normalize import (
    SGT,
    SHIFT_STARTS,
    AliasTable,
    chunk_id,
    dashed_part,
    doc_path_for,
    document_id,
    find_date,
    normalize_alarm_code,
    note_date,
    parse_downtime,
    parse_shift,
    sgt_date,
    shift_at,
)
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    Chunk,
    Extraction,
    NodeRef,
)

CONFIDENCE = 0.8  # a model read free text, and the quotes were checked
PURPOSES = {"handover": "extract handover", "maintenance_note": "extract note"}
EFFORT = "low"
HANDOVER_START = "shift handover"
MARKDOWN_NAME = ".md.txt"  # what every converted Markdown upload is saved as
MIN_QUOTE = 20  # a shorter quote is evidence only when it names the machine

# --- The schema -------------------------------------------------------------------
# Every field is required, so a value that may be missing is typed "or None" with no
# default.


class PartMention(BaseModel):
    """A spare part named in an item, and how many were used when the note says."""

    mention: str
    qty: int | None


class IncidentItem(BaseModel):
    """One breakdown or repair in a note, with every mention as written."""

    machine_mention: str | None
    time_text: str | None
    alarm_mentions: list[str]
    symptom: str | None
    action: str | None
    parts: list[PartMention]
    technician_mention: str | None
    downtime_text: str | None
    quote: str


class NoteExtraction(BaseModel):
    """What the model reads from one note."""

    note_date_text: str | None
    shift_text: str | None
    from_mention: str | None
    to_mention: str | None
    items: list[IncidentItem]


NOTE_SYSTEM = """\
You read short notes from the floor of a precision machining plant. A note is either
a shift handover or a loose maintenance note.

A shift handover starts with a line such as "Shift handover 23 Apr B", then a
"From: ... To: ..." line, then dashed lines, then "Signed ...". A loose maintenance
note is a few sentences by one technician about one repair.

The notes are full of typos and shorthand. Copy every piece of text exactly as it is
written. Never correct, expand, translate or normalize a mention.

Find the breakdowns and repairs only: a machine stopped, an alarm showed, something
was repaired or replaced. Leave out routine lines such as "nothing major", "running
fine", warm-up checks, coolant top-ups, air pressure checks, 5S, stores follow-ups,
waiting for parts and a door that is only sticking. A note with no breakdown has an
empty items list. Each breakdown is one item.

Note fields. note_date_text is the date of the note as written, such as "2026-04-09",
"23 Apr", "03/05/2026" or "4 May"; for a handover it is the date in the first line.
shift_text is the shift as written in the first line, such as "A", "Shift B" or
"night"; null for a loose note. from_mention and to_mention are the names after From
and To; null when the note has none.

Item fields. machine_mention is the machine as written, such as "CNC 1", "Press 2",
"the press" or "robot cell". time_text is the time of day the machine stopped, as
written, such as "2002" or "0039", without words like "about"; null when none is
given. alarm_mentions holds every alarm code as written, such as "ALM SP-104",
"Err:217" or "SP104"; an empty list when there is none. symptom is the fault as
written. action is what was done to fix it, as written, without the part names: in a
handover the repair words before the brackets, in a loose note the sentences about
what was found and replaced. parts holds each spare part named, with the mention
exactly as written (inside the brackets of a handover, after "replaced" in a loose
note) and qty as the number when the note gives one, such as 2 for "x2"; null when it
gives none. technician_mention is the person who did the repair when the text
says who; never use the From, To or Signed names for it. downtime_text is only the
duration with its unit, as written, such as "655 min", "approx 3h" or "11 hours";
null when none is given. quote is the part of the note that this item comes from:
one line of a handover, or the sentences of a loose note, copied character for
character, at most 300 characters. Use null for anything the note does not state.\
"""


# --- Reading the model's answer ---------------------------------------------------

_CLOCK = re.compile(r"\b(\d{1,2})[:.]?(\d{2})\b")


def _clock(time_text: str | None) -> dt.time | None:
    """The time of day in "2002", "20:02" or "about 0039", or None."""
    match = _CLOCK.search(time_text or "")
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return dt.time(hours, minutes)


def _text(value: str | None) -> str | None:
    """A stripped string, or None when the model left it blank."""
    value = (value or "").strip()
    return value or None


def starts_handover(first_line: str, markdown: bool = False) -> bool:
    """Whether the first line starts a shift handover. The router uses this too, so
    both agree on the type."""
    head = first_line.lstrip("# \t") if markdown else first_line
    return head.lower().startswith(HANDOVER_START)


def _doc_type(text: str, path: Path) -> str:
    """A handover if the first line says so, otherwise a loose maintenance note."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    markdown = path.name.lower().endswith(MARKDOWN_NAME)
    return "handover" if starts_handover(first, markdown) else "maintenance_note"


def _ask(doc_type: str, text: str, settings: Settings | None) -> NoteExtraction:
    # Imported here so the extractors that need no model do not load the model client.
    from app import llm

    return llm.structured(
        "sol",
        NoteExtraction,
        NOTE_SYSTEM,
        text,
        PURPOSES[doc_type],
        EFFORT,
        settings=settings,
    )


def _evidence_text(item: IncidentItem, text: str, number: int, notes: list[str]) -> str:
    """The quote to back an item. The model's quote is used if it appears in the note
    and is long enough or names the machine. Otherwise the best line naming the machine
    is used, then a line with an alarm code, then the first line."""
    claimed = item.quote.strip()
    machine = _text(item.machine_mention)
    if claimed and claimed in text:
        # A short quote such as "stopped" fits almost any note, so it only counts
        # if it names the machine.
        if len(claimed) >= MIN_QUOTE or (
            machine and machine.casefold() in claimed.casefold()
        ):
            return claimed
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    facts = [
        *item.alarm_mentions,
        item.time_text,
        item.downtime_text,
        *(part.mention for part in item.parts),
    ]

    def holds(line: str, needle: str | None) -> bool:
        return (
            bool(needle and needle.strip())
            and needle.strip().casefold() in line.casefold()
        )

    def score(line: str) -> int:
        return sum(holds(line, fact) for fact in facts)

    named = [line for line in lines if holds(line, item.machine_mention)]
    alarmed = [
        line for line in lines if any(holds(line, a) for a in item.alarm_mentions)
    ]
    for candidates, reason in (
        (named, "names the machine"),
        (alarmed, "holds the alarm code"),
    ):
        if candidates:
            notes.append(
                f"item {number}: the quote is not in the note, so the line that {reason} is used"
            )
            return max(candidates, key=score)  # max keeps the first of equal scores
    notes.append(
        f"item {number}: the quote is not in the note and no line names the machine, so the first line is used"
    )
    return lines[0] if lines else ""


def _note_day(
    answer: NoteExtraction, text: str, doc_type: str, notes: list[str]
) -> dt.date | None:
    """The date of the note, read from its text and never from the file name.

    A handover has its date in the first line, so that line wins over the model.
    A loose note trusts the model first. Both fall back to any date in the text.
    """
    model_text = _text(answer.note_date_text)
    model_day = note_date(model_text)
    if doc_type == "handover":
        first = next((line for line in text.splitlines() if line.strip()), "")
        line_day = find_date(first)
        if line_day is not None and model_day not in (None, line_day):
            notes.append(
                f"the date {model_text!r} the model read differs from {line_day.isoformat()} "
                "in the first line, so the first line is used"
            )
        day = line_day or model_day or find_date(text)
    else:
        day = model_day or find_date(text)
    if day is None:
        notes.append("no date was found in the note")
    return day


def _start(
    day: dt.date | None,
    clock: dt.time | None,
    shift: str | None,
    number: int,
    notes: list[str],
) -> dt.datetime | None:
    """The start of an item as a UTC time. The clock time is read as Singapore time
    on the note date. With no time it uses the start of the shift, and with neither
    there is no start."""
    if day is None:
        return None
    if clock is None and shift is not None:
        clock = SHIFT_STARTS[shift]
        notes.append(f"item {number}: no time given, so the shift start is used")
    if clock is None:
        notes.append(f"item {number}: no time and no shift, so there is no start time")
        return None
    return dt.datetime.combine(day, clock, tzinfo=SGT).astimezone(dt.UTC)


def _incident(
    item: IncidentItem,
    number: int,
    doc_path: str,
    text: str,
    day: dt.date | None,
    shift_header: str | None,
    notes: list[str],
) -> CandidateNode:
    """The incident for one item in the note."""
    key = f"{doc_path}#{number}"
    evidence = quote(
        doc_path, _evidence_text(item, text, number, notes), "sol-text", CONFIDENCE
    )
    clock = _clock(item.time_text)
    if _text(item.time_text) and clock is None:
        notes.append(
            f'item {number}: the time "{_text(item.time_text)}" cannot be read'
        )
    downtime = parse_downtime(item.downtime_text)
    if _text(item.downtime_text) and downtime is None:
        notes.append(
            f'item {number}: the downtime "{_text(item.downtime_text)}" cannot be read'
        )
    start = _start(day, clock, shift_header, number, notes)
    # The date is the Singapore day. In UTC the start can fall on the day before.
    when = sgt_date(start) if start else day
    props: dict[str, Any] = {
        "date": when.isoformat() if when else None,
        "start": start.isoformat() if start else None,
        "shift": shift_header or (shift_at(clock) if clock else None),
        "downtime_min": downtime,
        "symptom_text": _text(item.symptom),
        "technician": _text(item.technician_mention),
        "resolution_text": _text(item.action),
    }
    return CandidateNode(
        ref=NodeRef(type="Incident", key=key),
        label=props["symptom_text"] or key,
        props=props,
        evidence=evidence,
        canonical=False,
    )


def _edges(
    item: IncidentItem,
    node: CandidateNode,
    aliases: AliasTable,
    number: int,
    notes: list[str],
) -> list[CandidateEdge]:
    evidence = node.evidence
    edges: list[CandidateEdge] = []
    machine = _text(item.machine_mention)
    if machine:
        tag = aliases.asset_for(machine)
        if tag is None:
            notes.append(
                f"item {number}: machine {machine!r} is not in the alias table"
            )
        edges.append(
            CandidateEdge(
                src=node.ref,
                rel="OCCURRED_ON",
                dst=NodeRef(type="Asset", key=tag or machine),
                evidence=evidence,
            )
        )
    else:
        notes.append(f"item {number}: no machine is named")
    for mention in item.alarm_mentions:
        mention = mention.strip()
        if mention:
            edges.append(
                CandidateEdge(
                    src=node.ref,
                    rel="TRIGGERED_BY",
                    dst=NodeRef(
                        type="AlarmCode", key=normalize_alarm_code(mention) or mention
                    ),
                    evidence=evidence,
                )
            )
    for part in item.parts:
        name = part.mention.strip()
        if not name:
            continue
        if part.qty is None:
            notes.append(
                f"item {number}: part {name!r} has no quantity, so 1 is assumed"
            )
        edges.append(
            CandidateEdge(
                src=node.ref,
                rel="USED",
                dst=NodeRef(type="Part", key=dashed_part(name) or name),
                props={"qty": part.qty if part.qty is not None else 1},
                evidence=evidence,
            )
        )
    return edges


def extract_note(
    path: Path, aliases: AliasTable, settings: Settings | None = None
) -> Extraction:
    """Read a shift handover or maintenance note into one incident per breakdown.

    Each incident links to its machine, alarm codes and parts. The date comes from
    the note text. Settings are loaded when not given.
    """
    path = Path(path)
    doc_path = doc_path_for(path)
    text = path.read_text(encoding="utf-8-sig")
    doc_type = _doc_type(text, path)
    extraction = Extraction(doc_path=doc_path, doc_type=doc_type, extractor="sol-text")
    extraction.chunks.append(
        Chunk(
            id=chunk_id(document_id(doc_path), None, 0),
            doc_path=doc_path,
            page=None,
            ord=0,
            text=text,
        )
    )

    answer = _ask(doc_type, text, settings)
    notes = extraction.notes
    day = _note_day(answer, text, doc_type, notes) if answer.items else None
    shift = parse_shift(answer.shift_text)
    if answer.items and doc_type == "handover" and shift is None:
        notes.append("no shift was found in the first line")
    for number, item in enumerate(answer.items, start=1):
        node = _incident(item, number, doc_path, text, day, shift, notes)
        extraction.nodes.append(node)
        extraction.edges += _edges(item, node, aliases, number, notes)
    return extraction
