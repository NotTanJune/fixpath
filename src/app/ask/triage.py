"""Triage reads a technician's question and decides what the crew does next.

It works out which machine and alarm the question is about, the likely root cause, what
the technician wants, and whether the job looks safety-critical. A model makes these
picks, each with a confidence. Code then double-checks them against the text. If a pick
is unsure, the card offers tap-to-confirm options. If the question is unclear, it asks
the technician to clarify. If it is outside faults, parts and breakdown history, it says
who to ask instead.

triage is the entry point. found_ids and shared_name find machine and alarm names that
are written in the text itself.
"""

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field

from app import store
from app.ask import plant_check, tools
from app.ask.types import Chip, Triage
from app.decide import Answer, Choice, Decider, DecisionError, Noul, Question
from app.ingest.normalize import normalize_alarm_code
from app.ingest.questions import probability

PURPOSE = "triage"
GATES = {"act": 0.75, "confirm": 0.4, "chip": 0.15}
HISTORY_TURNS = 4
CLARIFY_CHIPS = 3
MAX_WORDS = 60  # how many words of the question code scans for ids
MAX_PHRASE = 4  # the longest alias, in words

UNKNOWN, NONE = "unknown", "none"
INTENTS = {
    "troubleshoot": "Find the cause and the fix of a fault",
    "find_part": "Find a spare part: stock, bin or lead time",
    "history": "Look up past breakdowns or how often an alarm came",
    "out_of_scope": (
        "Not about the faults, parts or history of these machines, for example "
        "scheduling, staffing, safety policy or ordering"
    ),
}
INTENT_LABELS = {
    "troubleshoot": "Troubleshoot a fault",
    "find_part": "Find a part",
    "history": "Past breakdowns",
    "out_of_scope": "Something else",
}
DEFAULT_INTENT = "troubleshoot"
SAFETY_INSTRUCTIONS = (
    "Does the question involve working on a machine that needs isolating, or a "
    "hazard to people?"
)
SAFETY_TRUE = "Someone would work inside or on the machine, or people could be hurt"
SAFETY_FALSE = "The question is about reading, checking or ordering, with no hazard"

# Who to ask about an out-of-scope question. The first match wins. Scheduling comes
# before stores, so "holiday leave roster for stores" counts as scheduling.
REFERRALS = (
    (
        re.compile(r"\b(policy|policies|ppe|regulations?|compliance|audits?)\b", re.I),
        "This is about safety policy. Ask EHS.",
    ),
    (
        re.compile(
            r"\b(shifts?|rosters?|rotas?|schedule[ds]?|scheduling|staff|staffing|staffed|leave"
            r"|overtime|holidays?|swap|swaps)\b",
            re.I,
        ),
        "This is about scheduling or staffing. Ask the shift lead.",
    ),
    (
        re.compile(
            r"\b(orders|ordering|order(?!\s+to\b)|reorder|purchases?|purchasing|procurement"
            r"|suppliers?|stores)\b",
            re.I,
        ),
        "This is about ordering. Ask stores.",
    ),
)
DEFAULT_REFERRAL = (
    "This is outside faults, parts and breakdown history. Ask the shift lead."
)

# Matches "no alarm", "no alarm code", "without an alarm" and "no code". A hyphenated
# "no-alarm light" does not match.
NO_ALARM = re.compile(
    r"\b(?:no|without(?:\s+(?:an?|any))?)\s+(?:alarms?(?:\s+codes?)?|codes?)(?![\w-])",
    re.I,
)

_WORD = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")


# --- the options of the graph --------------------------------------------------------


@dataclass
class _Option:
    description: str  # what the model reads
    label: str  # what the technician taps


@dataclass
class _Options:
    machine: dict[str, _Option] = field(default_factory=dict)
    alarm: dict[str, _Option] = field(default_factory=dict)
    root_cause: dict[str, _Option] = field(default_factory=dict)


def _rows(conn: sqlite3.Connection, node_type: str) -> list[tuple[str, str, dict]]:
    rows = conn.execute(
        "SELECT id, label, props FROM nodes WHERE type = ? ORDER BY id", (node_type,)
    )
    return [(row["id"], row["label"], json.loads(row["props"])) for row in rows]


def _options(conn: sqlite3.Connection) -> _Options:
    options = _Options()
    names = {}
    for asset_id, label, props in _rows(conn, "Asset"):
        names[asset_id] = props.get("name", label)
        aliases = props.get("aliases", [])
        text = f"{names[asset_id]}, {props.get('machine_type', 'machine')}, model {props.get('model')}"
        if aliases:
            text += f"; also called {', '.join(aliases)}"
        options.machine[asset_id] = _Option(text, f"{names[asset_id]} ({asset_id})")
    for code, label, props in _rows(conn, "AlarmCode"):
        text = props.get("text", label)
        options.alarm[code] = _Option(text, f"{code} {text}")
    for mode_id, label, props in _rows(conn, "FailureMode"):
        name = props.get("name", label)
        affected = sorted(
            {
                names[comp["props"]["asset_id"]]
                for comp in store.neighbors(conn, mode_id, "AFFECTS")
                if comp["props"].get("asset_id") in names
            }
        )
        text = f"{name} (affects {', '.join(affected)})" if affected else name
        options.root_cause[mode_id] = _Option(text, name)
    return options


def _questions(options: _Options) -> dict[str, Question]:
    def choice(
        instructions: str, listed: dict[str, _Option], other: str, other_text: str
    ):
        return Choice(
            instructions,
            {key: item.description for key, item in listed.items()}
            | {other: other_text},
        )

    return {
        "machine": choice(
            "Which machine does the question concern?",
            options.machine,
            UNKNOWN,
            "No machine is named or implied",
        ),
        "alarm": choice(
            "Which alarm code does the question concern?",
            options.alarm,
            NONE,
            "No alarm code is named or implied",
        ),
        "root_cause": choice(
            "Which root cause does the technician suspect, or the symptoms point to?",
            options.root_cause,
            UNKNOWN,
            "No root cause is suspected or implied",
        ),
        "intent": Choice("What does the technician want?", dict(INTENTS)),
        "safety_critical": Noul(SAFETY_INSTRUCTIONS, SAFETY_TRUE, SAFETY_FALSE),
    }


# --- the ids code finds in the text --------------------------------------------------


def found_ids(question: str, conn: sqlite3.Connection) -> dict[str, list[str]]:
    """The machines and alarm codes that the text names, found by code alone.

    A machine counts when a short phrase exactly matches its id, name, alias, model or
    vendor. A model or vendor name counts for every machine of that model or vendor.
    """
    words = _WORD.findall(question)[:MAX_WORDS]
    machines: set[str] = set()
    for size in range(1, MAX_PHRASE + 1):
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start : start + size])
            machines.update(
                item["id"]
                for item in tools.find_machine(conn, phrase)
                if item["score"] == 100
            )
    known = {code for code, _, _ in _rows(conn, "AlarmCode")}
    alarms: set[str] = set()
    for size in (1, 2):
        for start in range(len(words) - size + 1):
            code = normalize_alarm_code(" ".join(words[start : start + size]))
            if code in known:
                alarms.add(code)
    return {"machines": sorted(machines), "alarms": sorted(alarms)}


def shared_name(
    question: str, conn: sqlite3.Connection
) -> tuple[str, list[str]] | None:
    """A model or vendor name in the text that covers two or more machines, with their
    ids. None if there is none, or if the text names one machine outright."""
    words = _WORD.findall(question)[:MAX_WORDS]
    shared: list[tuple[str, list[str]]] = []
    for size in range(1, MAX_PHRASE + 1):
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start : start + size])
            found = tools.find_machine(conn, phrase)
            hits = [item for item in found if item["score"] == 100]
            if any(item["matched"] not in ("model", "vendor") for item in hits):
                return None
            if len(hits) > 1:
                shared.append((phrase, sorted(item["id"] for item in hits)))
    return shared[0] if shared else None


# --- reading the answers -------------------------------------------------------------


def _band(p: float) -> str:
    if p >= GATES["act"]:
        return "act"
    return "confirm" if p >= GATES["confirm"] else "clarify"


def _clean(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 1 else None


@dataclass
class _Read:
    """One model answer, read: what it chose, how sure it was, and the other options."""

    chosen: str | None = None
    p: float | None = None
    probs: dict[str, float] = field(default_factory=dict)

    @property
    def decided(self) -> bool:
        return self.chosen is not None

    @property
    def band(self) -> str:
        return _band(self.p) if self.decided else "none"


def _read(answer: Answer | None, valid: set[str]) -> _Read:
    if answer is None or answer.kind != "choice" or answer.choice not in valid:
        return _Read()
    p = probability(answer)
    if p is None:
        return _Read()
    probs = {
        k: v
        for k, v in answer.probabilities.items()
        if k in valid and _clean(v) is not None
    }
    return _Read(answer.choice, p, probs)


def _chips(
    name: str, read: _Read, options: dict[str, _Option], skip: str, *, top: int | None
) -> list[Chip]:
    """The tap-to-confirm options for a field, most likely first. With top, take that
    many. Otherwise take every option that is likely enough."""
    ranked = sorted(
        ((k, v) for k, v in read.probs.items() if k != skip and k in options),
        key=lambda pair: (-pair[1], pair[0]),
    )
    if top is None:
        ranked = [pair for pair in ranked if pair[1] >= GATES["chip"]]
    else:
        ranked = ranked[:top]
    return [Chip(field=name, value=k, label=options[k].label, p=v) for k, v in ranked]


def _resolve(
    name: str,
    read: _Read,
    options: dict[str, _Option],
    skip: str,
    fallback: str | None,
    *,
    clarify_top: bool,
) -> tuple[str | None, list[Chip]]:
    """The value and the options to show for a machine, alarm or root cause.

    A pick the model is not sure about is dropped, and options are shown instead.
    """
    if not read.decided:
        return fallback, []
    value = None if read.chosen == skip else read.chosen
    # with no machine, ask which one and list the top options
    ask_which = value is None and name == "machine"
    if read.band == "act":
        return value, (
            _chips(name, read, options, skip, top=CLARIFY_CHIPS) if ask_which else []
        )
    if read.band == "confirm":
        return value, _chips(
            name, read, options, skip, top=CLARIFY_CHIPS if ask_which else None
        )
    # not sure enough to use
    top = CLARIFY_CHIPS if clarify_top else None
    return None, _chips(name, read, options, skip, top=top)


def _intent(read: _Read) -> tuple[str, list[Chip]]:
    options = {k: _Option(v, INTENT_LABELS[k]) for k, v in INTENTS.items()}
    if not read.decided:
        return DEFAULT_INTENT, []
    chips = [] if read.band == "act" else _chips("intent", read, options, "", top=None)
    if read.band == "act":
        return read.chosen, chips
    if read.band == "confirm" and read.chosen != "out_of_scope":
        return read.chosen, chips
    # when unsure, never treat the question as out of scope on a guess
    others = sorted(
        ((k, v) for k, v in read.probs.items() if k != "out_of_scope"),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return (others[0][0] if others else DEFAULT_INTENT), chips


def _safety(
    answer: Answer | None,
) -> tuple[float | None, float | None, str, list[Chip]]:
    """How likely the job is safety-critical, how sure the model is, and what to show."""
    p = probability(answer) if answer is not None and answer.kind == "noul" else None
    if p is None:
        return None, None, "none", []
    confidence = max(p, 1 - p)
    if confidence >= GATES["act"]:
        return p, confidence, "act", []
    chips = [
        Chip(field="safety_critical", value=value, label=label, p=prob)
        for value, label, prob in sorted(
            (("yes", "Yes, isolation or a hazard", p), ("no", "No hazard", 1 - p)),
            key=lambda item: -item[2],
        )
        if prob >= GATES["chip"]
    ]
    return p, confidence, "confirm", chips


def _referral(question: str) -> str:
    for pattern, reason in REFERRALS:
        if pattern.search(question):
            return reason
    return DEFAULT_REFERRAL


# --- second looks at the result --------------------------------------------------------


def _chip(name: str, value: str, options: dict[str, _Option], p: float | None) -> Chip:
    return Chip(field=name, value=value, label=options[value].label, p=p)


def _found_chips(ids: list[str], options: dict[str, _Option]) -> list[Chip]:
    """Options for machines that code found in the text."""
    return [
        _chip("machine", i, options, None) for i in ids[:CLARIFY_CHIPS] if i in options
    ]


def _disagreement(
    picked: str | None,
    said: list[str],
    read: _Read,
    name: str,
    options: dict[str, _Option],
    typed_first: bool = False,
) -> tuple[list[Chip], str] | None:
    """Options and a reason when the model picked something other than the one id the
    text names. None if they agree or the text names none or several."""
    if picked is None or len(said) != 1 or said[0] == picked or said[0] not in options:
        return None
    chips = [
        _chip(name, picked, options, read.probs.get(picked, read.p)),
        _chip(name, said[0], options, read.probs.get(said[0])),
    ]
    if typed_first:
        chips.reverse()
    return chips, f"The question names {said[0]}, but I read {picked}."


def _part_clarify(
    question: str, conn: sqlite3.Connection, options: dict[str, _Option]
) -> tuple[list[Chip], str] | None:
    """Options for a question that names a part but no machine: the machines the part
    fits. None if no named part fits a machine."""
    found = [tools.part_stock(conn, p) for p in tools.find_parts(conn, question)]
    stocks = [stock for stock in found if stock is not None]
    fits = sorted({asset for stock in stocks for asset in stock.fits})
    chips = _found_chips(fits, options)
    if not chips:
        return None
    names = ", ".join(stock.part_no for stock in stocks)
    reason = f"Stock for {names} is below. Pick a machine to see the fix it is used in."
    return chips, reason


def _names(ids: list[str]) -> str:
    """ "A", "A and B" or "A, B and C"."""
    return ids[0] if len(ids) == 1 else ", ".join(ids[:-1]) + f" and {ids[-1]}"


def _raisers(code: str | None, conn: sqlite3.Connection) -> list[str]:
    """The machines that raised the alarm, the most frequent first."""
    return [item["id"] for item in tools.machines_raising(conn, code)] if code else []


def _raised_by(
    code: str, raisers: list[str], options: dict[str, _Option]
) -> tuple[str | None, str, list[Chip], str]:
    """Handle an alarm with no machine. If only one machine raised it, use that one.
    Otherwise offer the top few."""
    chips = _found_chips(raisers, options)
    if len(raisers) == 1:
        return raisers[0], "confirm", chips, f"{code} is only raised by {raisers[0]}."
    shown = _names([chip.value for chip in chips])
    return None, "clarify", chips, f"{code} is raised by {shown}. Which one?"


def _unknown_line_reason(number: int, top: str | None, conn: sqlite3.Connection) -> str:
    text = f"There is no Line {number} in this plant"
    line = plant_check.line_of(conn, top) if top else None
    return (
        f"{text}; did you mean {top} on {line}?"
        if line
        else f"{text}. Which machine do you mean?"
    )


# --- the entry point ------------------------------------------------------------------


def triage(
    question: str, history: list[str] | None, conn: sqlite3.Connection, decider: Decider
) -> Triage:
    """Read the question and decide what the crew does next.

    history is the earlier turns, oldest first. After the model's picks, code checks
    them against the text. Some checks turn the result into a question for the
    technician, for example when the text names a different machine, a production line
    the plant lacks, or a model name shared by several machines.
    """
    turns = [str(turn) for turn in (history or [])][-HISTORY_TURNS:]
    options = _options(conn)
    found = found_ids(question, conn)
    state = {"question": question, "history": turns, "found": found}
    try:
        answers = decider.ask(state, _questions(options), PURPOSE)
    except DecisionError:
        answers = {}

    machine = _read(answers.get("machine"), set(options.machine) | {UNKNOWN})
    alarm = _read(answers.get("alarm"), set(options.alarm) | {NONE})
    cause = _read(answers.get("root_cause"), set(options.root_cause) | {UNKNOWN})
    intent_read = _read(answers.get("intent"), set(INTENTS))

    only = lambda ids: ids[0] if len(ids) == 1 else None  # noqa: E731
    asset_id, machine_chips = _resolve(
        "machine",
        machine,
        options.machine,
        UNKNOWN,
        only(found["machines"]),
        clarify_top=True,
    )
    alarm_code, alarm_chips = _resolve(
        "alarm", alarm, options.alarm, NONE, only(found["alarms"]), clarify_top=True
    )
    failure_mode_id, cause_chips = _resolve(
        "root_cause", cause, options.root_cause, UNKNOWN, None, clarify_top=False
    )
    # the text says there is no alarm, so show none unless it also writes a code
    ruled_out = bool(NO_ALARM.search(question)) and not found["alarms"]
    if ruled_out:
        alarm_code, alarm_chips = None, []
    intent, intent_chips = _intent(intent_read)
    flag, flag_confidence, flag_band, flag_chips = _safety(
        answers.get("safety_critical")
    )

    confidence = {
        "machine": machine.p,
        "alarm": 1.0 if ruled_out else alarm.p,
        "root_cause": cause.p,
        "intent": intent_read.p,
        "safety_critical": flag_confidence,
    }
    other_chips = cause_chips + intent_chips + flag_chips
    alarm_band = "act" if ruled_out else alarm.band
    bands = [machine.band, alarm_band, cause.band, intent_read.band, flag_band]

    reason = None
    if intent_read.chosen == "out_of_scope" and intent_read.band == "act":
        return Triage(
            asset_id=asset_id,
            alarm_code=alarm_code,
            failure_mode_id=failure_mode_id,
            intent="out_of_scope",
            safety_critical=flag,
            confidence=confidence,
            status="out_of_scope",
            chips=[],
            reason=_referral(question),
        )
    if asset_id is None:
        status = "clarify"
        reason = "Which machine do you mean?"
        if not machine.decided:
            reason = f"I could not read the question with confidence. {reason}"
    elif all(band == "act" for band in bands):
        status = "act"
    else:
        status = "confirm"
        unsure = [
            name for name, band in zip(confidence, bands, strict=True) if band != "act"
        ]
        reason = "Not sure about: " + ", ".join(unsure) + "."

    exact = plant_check.exact_asset_ids(question, conn)
    clashes = []
    if status != "clarify":
        for picked, ids, read, name, listed in (
            (asset_id, exact, machine, "machine", options.machine),
            (alarm_code, found["alarms"], alarm, "alarm", options.alarm),
        ):
            typed_first = name == "machine"
            clash = _disagreement(picked, ids, read, name, listed, typed_first)
            if clash:
                chips, why = clash
                if name == "machine":
                    machine_chips, asset_id = chips, ids[0]  # the id typed in the question wins
                else:
                    alarm_chips = chips
                clashes.append(why)
        if clashes:
            status, reason = "confirm", " ".join(clashes) + " Which is right?"

    missing = plant_check.unknown_line(question, conn)
    shared = shared_name(question, conn)
    if missing is not None:
        if exact and (asset_id is not None or len(exact) == 1):
            asset_id = asset_id or exact[0]
            status = "confirm"
            top = asset_id
            if all(chip.value != asset_id for chip in machine_chips):
                machine_chips = [
                    _chip("machine", asset_id, options.machine, machine.p),
                    *machine_chips,
                ]
            machine_chips = sorted(
                machine_chips, key=lambda chip: chip.value != asset_id
            )
            reason = " ".join([_unknown_line_reason(missing, top, conn), *clashes])
        else:
            asset_id, status = None, "clarify"
            machine_chips = _chips(
                "machine", machine, options.machine, UNKNOWN, top=CLARIFY_CHIPS
            )
            machine_chips = machine_chips or _found_chips(
                exact or found["machines"], options.machine
            )
            top = machine_chips[0].value if machine_chips else None
            reason = _unknown_line_reason(missing, top, conn)
    elif shared and not exact:
        # a name shared by two machines is a coin flip, so always ask
        phrase, ids = shared
        machine_chips = [
            _chip("machine", i, options.machine, machine.probs.get(i))
            for i in ids
            if i in options.machine
        ]
        # an earlier turn that names exactly one of them settles it; the model's pick
        # alone does not
        earlier = plant_check.exact_asset_ids(" ".join(turns), conn)
        named = [i for i in earlier if i in ids]
        asset_id = named[0] if len(named) == 1 else None
        status = "confirm" if asset_id else "clarify"
        reason = f"{phrase} is {_names(ids)}. Which one?"
        if clashes:
            reason += " " + " ".join(clashes) + " Which is right?"
    elif status == "clarify":
        part = _part_clarify(question, conn, options.machine)
        known = alarm_code or only(found["alarms"])
        raisers = [] if found["machines"] else _raisers(known, conn)
        if part:
            machine_chips, reason = part
        elif raisers:
            asset_id, status, machine_chips, reason = _raised_by(
                known, raisers, options.machine
            )
            if asset_id:
                confidence["machine"] = None  # the graph chose it, not the model
        elif not machine_chips:
            machine_chips = _found_chips(found["machines"], options.machine)

    return Triage(
        asset_id=asset_id,
        alarm_code=alarm_code,
        failure_mode_id=failure_mode_id,
        intent=intent,
        safety_critical=flag,
        confidence=confidence,
        status=status,
        chips=machine_chips + alarm_chips + other_chips,
        reason=reason,
    )
