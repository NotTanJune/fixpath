"""Write the shift handover notes.

A handover is a short, untidy note from one shift to the next. Each note lists the
incidents that began in its shift, plus a few filler lines about routine work. The
file names and headers vary on purpose.

write_handovers() writes the files and returns what each mentions. handover_slots()
decides which shifts get a note. _handover_item() writes one incident as a line.
"""

import datetime as dt
import random
from dataclasses import dataclass
from pathlib import Path

from app.generator import catalogue as cat
from app.generator.mentions import Conflict, FileMentions, Mentions, file_key
from app.generator.notes_incidents import Incident, alarm_ref, machine_ref, part_ref
from app.generator.notes_text import MONTHS, add_typos, shorthand, unique_name
from app.generator.world import World

HANDOVER_COUNT = 90
CONFLICT_PROBABILITY = 0.08  # the line names the wrong machine
PART_SHOWN_PROBABILITY = 0.6  # the line names a part used
FOLLOW_UP_PROBABILITY = 0.4  # the line ends with a follow-up request

GRIPPER_PART = "GRP-FNG-12"

_SHIFTS = ("A", "B", "C")
_NEXT_SHIFT = {"A": "B", "B": "C", "C": "A"}
_NAME_STYLES = (
    "handover_{d:%Y-%m-%d}_{s}.txt",
    "HO {d:%m-%d} {s} shift.txt",
    "shift notes {d.day} {mon} {s}.txt",
    "Handover_{d:%Y%m%d}_{s}.txt",
)
_CLOSERS = ("tested ok", "running ok now", "test run ok")
_FOLLOW_UPS = (
    "pls chk again tmr",
    "f/u tmr",
    "pls f/u w/ stores",
    "chk again next shift",
)

# Each filler is the line, the machines it names and the parts it names.
_FILLERS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("CNC 1 running fine, tool wear ok", ("CNC-01",), ()),
    ("5S done on robot cell", ("RW-01",), ()),
    ("coolant topped up on MC-2", ("CNC-02",), ()),
    ("leak tester master part passed at start of shift", ("LT-01",), ()),
    ("press warm-up normal", ("HP-01",), ()),
    ("nothing major, all machines running", (), ()),
    ("chk air pressure at LT1, ok", ("LT-01",), ()),
    ("pls f/u on stores count for seals", (), ()),
    ("robot cell guard door sticking a bit, f/u tmr", ("RW-01",), ()),
)
# Used only after the first incident that used this part, when stores has run out.
_GRIPPER_FILLER = (
    "waiting on gripper fingers, stores says none left",
    (),
    (GRIPPER_PART,),
)


@dataclass(frozen=True)
class _Item:
    """One line of a handover and what it mentions."""

    text: str
    incident: str | None = None
    assets: tuple[str, ...] = ()
    alarms: tuple[str, ...] = ()
    parts: tuple[str, ...] = ()
    conflict: Conflict | None = None


def _wrong_machine(rng: random.Random, asset_id: str) -> tuple[str, str] | None:
    """A machine a note might name by mistake, with the way the note writes it.
    It is another machine of the same type, or CNC 1 for the press."""
    same_type = [
        other.id
        for other in cat.ASSETS
        if other.machine_type == cat.ASSET_BY_ID[asset_id].machine_type
        and other.id != asset_id
    ]
    if same_type:
        wrong = rng.choice(same_type)
        return wrong, machine_ref(rng, wrong)
    return ("CNC-01", "CNC 1") if asset_id == "HP-01" else None


def _downtime_phrase(rng: random.Random, minutes: int) -> str:
    if rng.random() < 0.5:
        return f"approx {max(1, round(minutes / 60))}h"
    return f"{minutes} min"


def _handover_item(rng: random.Random, incident: Incident) -> _Item:
    """Write one incident as a short handover line. Sometimes it names the wrong
    machine on purpose."""
    asset, machine, conflict = incident.asset, None, None
    if rng.random() < CONFLICT_PROBABILITY and (wrong := _wrong_machine(rng, asset)):
        asset, machine = wrong
        conflict = Conflict(incident=incident.id, says_asset=asset)
    machine = machine or machine_ref(rng, asset)

    text = f"{machine} stopped {incident.start:%H%M}"
    if incident.alarm:
        text += f" w/ {alarm_ref(rng, incident.alarm)}"
    text += f", {incident.symptom}. {shorthand(incident.procedure)}"
    shown = [part for part in incident.parts if rng.random() < PART_SHOWN_PROBABILITY]
    shown = shown or list(incident.parts[:1])
    if shown:
        refs = (
            part_ref(rng, part_no) + (f" x{qty}" if qty > 1 else "")
            for part_no, qty in shown
        )
        text += f" ({', '.join(refs)})"
    text += (
        f", {rng.choice(_CLOSERS)}. Down {_downtime_phrase(rng, incident.downtime)}."
    )
    if rng.random() < FOLLOW_UP_PROBABILITY:
        text += f" {rng.choice(_FOLLOW_UPS)}."
    return _Item(
        add_typos(rng, text),
        incident=incident.id,
        assets=(asset,),
        alarms=(incident.alarm,) if incident.alarm else (),
        parts=tuple(part_no for part_no, _ in shown),
        conflict=conflict,
    )


def _filler_items(rng: random.Random, gripper_allowed: bool) -> list[_Item]:
    pool = [*_FILLERS, _GRIPPER_FILLER] if gripper_allowed else list(_FILLERS)
    return [
        _Item(add_typos(rng, text), assets=assets, parts=parts)
        for text, assets, parts in rng.sample(pool, rng.randint(1, 3))
    ]


def _date_text(rng: random.Random, day: dt.date) -> str:
    return rng.choice(
        (f"{day:%d/%m/%Y}", f"{day.day} {MONTHS[day.month - 1]}", f"{day:%Y-%m-%d}")
    )


def _period_days() -> list[dt.date]:
    days = (cat.PERIOD_END.date() - cat.PERIOD_START.date()).days + 1
    return [cat.PERIOD_START.date() + dt.timedelta(days=n) for n in range(days)]


def handover_slots(
    rng: random.Random, handed_over: list[Incident]
) -> dict[tuple[dt.date, str], list[Incident]]:
    """The shifts that get a note, with the incidents that began in each. Every shift
    with an incident gets one. Random empty shifts fill the count up to 90."""
    slots: dict[tuple[dt.date, str], list[Incident]] = {}
    for incident in handed_over:
        slots.setdefault((incident.start.date(), incident.shift), []).append(incident)
    missing = HANDOVER_COUNT - len(slots)
    if missing > 0:
        spare = [
            (day, shift)
            for day in _period_days()
            for shift in _SHIFTS
            if (day, shift) not in slots
        ]
        for slot in rng.sample(spare, missing):
            slots[slot] = []
    return dict(sorted(slots.items()))


def write_handovers(
    world: World,
    rng: random.Random,
    out: Path,
    incidents: list[Incident],
    handed_over: list[Incident],
) -> Mentions:
    folder = out / "raw" / "handovers"
    folder.mkdir(parents=True, exist_ok=True)
    first_gripper = min(
        (
            i.start.date()
            for i in incidents
            if any(p == GRIPPER_PART for p, _ in i.parts)
        ),
        default=None,
    )
    crews = {
        shift: [t for t in world.technicians if t.shift == shift] for shift in _SHIFTS
    }
    taken: set[str] = set()
    mentions: Mentions = {}
    for (day, shift), here in handover_slots(rng, handed_over).items():
        items = [_handover_item(rng, incident) for incident in here]
        items += _filler_items(rng, first_gripper is not None and day > first_gripper)
        rng.shuffle(items)
        sender = rng.choice(crews[shift])
        receiver = rng.choice(crews[_NEXT_SHIFT[shift]])
        label = (
            rng.choice(("C shift", "night"))
            if shift == "C"
            else {"A": "Shift A", "B": "B"}[shift]
        )
        date_text = _date_text(rng, day)
        sender_form = rng.choice(sender.short_forms)
        receiver_form = rng.choice(receiver.short_forms)
        lines = [
            f"Shift handover {date_text} {label}",
            f"From: {sender_form}   To: {receiver_form}",
            "",
            *(f"- {item.text}" for item in items),
            "",
            f"Signed {rng.choice(sender.short_forms)}",
        ]
        token = rng.choice(("C", "night")) if shift == "C" else shift
        name = unique_name(
            rng.choice(_NAME_STYLES).format(d=day, s=token, mon=MONTHS[day.month - 1]),
            taken,
        )
        taken.add(name)
        path = folder / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        mentions[file_key(out, path)] = FileMentions(
            incidents=[i.incident for i in items if i.incident],
            assets=[asset for i in items for asset in i.assets],
            alarm_codes=[code for i in items for code in i.alarms],
            parts=[part for i in items for part in i.parts],
            conflicts=[i.conflict for i in items if i.conflict],
        )
    return mentions
