"""Cleans up the many ways the raw files write the same thing.

The functions here turn alarm codes, part numbers, dates, shifts, stock values and
machine names into one standard form. AliasTable matches the different names people
use for a machine. None of this calls a model.

The plant runs on Singapore time. Full timestamps are stored in UTC, and a date with
no time of day counts as the start of that day in Singapore.
"""

import datetime as dt
import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from app.config import RAW_DIR
from app.ontology import validate_id

SGT = dt.timezone(dt.timedelta(hours=8), "SGT")
DEFAULT_YEAR = 2026

# When each shift starts in Singapore time. A shift ends when the next one starts,
# so shift C runs from 23:00 to 07:00.
SHIFT_STARTS = {"A": dt.time(7, 0), "B": dt.time(15, 0), "C": dt.time(23, 0)}

# The date styles parse_when understands. Log styles are full timestamps. Note styles
# are plain dates, as people write them in notes, exports and file names.
LOG_STYLES = ("iso_utc", "ddmmyyyy_sgt", "epoch", "dotted_sgt")
NOTE_STYLES = ("DD/MM/YYYY", "YYYY-MM-DD", "D-Mon-YY", "D Mon", "DD-MM", "YYYYMMDD")

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

# --- Dates and times ------------------------------------------------------------

_ISO_UTC = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:Z|\+00:00)?")
_DDMMYYYY_SGT = re.compile(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})")
_DOTTED_SGT = re.compile(r"(\d{4})\.(\d{2})\.(\d{2}) (\d{2}):(\d{2}):(\d{2})")
_EPOCH = re.compile(r"\d{1,12}(?:\.\d+)?")


def _month_number(name: str) -> int:
    """Return the month number for an English month name or short form."""
    word = name.lower().rstrip(".")
    if word == "sept":
        word = "sep"
    for number, full in enumerate(_MONTHS, start=1):
        if word in (full, full[:3]):
            return number
    raise ValueError(f"{name!r} is not a month")


def _two_digit_year(text: str) -> int:
    return 2000 + int(text) if len(text) == 2 else int(text)


def _utc(*parts: int, tz: dt.tzinfo) -> dt.datetime:
    """Convert a clock time in the given time zone to UTC."""
    return dt.datetime(*parts, tzinfo=tz).astimezone(dt.UTC)


def _numbers(pattern: re.Pattern[str], text: str, style: str) -> list[int]:
    match = pattern.fullmatch(text)
    if match is None:
        raise ValueError(f"{text!r} is not a {style} timestamp")
    return [int(group) for group in match.groups()]


def _log_when(style: str, text: str) -> dt.datetime:
    if style == "epoch":
        if not _EPOCH.fullmatch(text):
            raise ValueError(f"{text!r} is not an epoch timestamp")
        return dt.datetime.fromtimestamp(float(text), tz=dt.UTC)
    if style == "iso_utc":
        year, month, day, *clock = _numbers(_ISO_UTC, text, style)
        return _utc(year, month, day, *clock, tz=dt.UTC)
    if style == "ddmmyyyy_sgt":
        day, month, year, *clock = _numbers(_DDMMYYYY_SGT, text, style)
        return _utc(year, month, day, *clock, tz=SGT)
    year, month, day, *clock = _numbers(_DOTTED_SGT, text, style)  # the dotted style
    return _utc(year, month, day, *clock, tz=SGT)


# For each note style: the pattern for the whole text, and how to read the year,
# month and day from it.
_NOTE_DAYS: dict[str, tuple[re.Pattern[str], Callable[..., tuple[int, int, int]]]] = {
    "DD/MM/YYYY": (
        re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"),
        lambda d, m, y: (int(y), int(m), int(d)),
    ),
    "YYYY-MM-DD": (
        re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
        lambda y, m, d: (int(y), int(m), int(d)),
    ),
    "D-Mon-YY": (
        re.compile(r"(\d{1,2})-([A-Za-z]{3,9})\.?-(\d{2}|\d{4})"),
        lambda d, mon, y: (_two_digit_year(y), _month_number(mon), int(d)),
    ),
    "D Mon": (
        re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?"),
        lambda d, mon: (DEFAULT_YEAR, _month_number(mon), int(d)),
    ),
    "DD-MM": (
        re.compile(r"(\d{1,2})-(\d{1,2})"),
        lambda d, m: (DEFAULT_YEAR, int(m), int(d)),
    ),
    "YYYYMMDD": (
        re.compile(r"(\d{4})(\d{2})(\d{2})"),
        lambda y, m, d: (int(y), int(m), int(d)),
    ),
}


def _note_when(style: str, text: str) -> dt.datetime:
    pattern, to_day = _NOTE_DAYS[style]
    match = pattern.fullmatch(text)
    if match is None:
        raise ValueError(f"{text!r} is not a {style} date")
    return _utc(*to_day(*match.groups()), tz=SGT)


def parse_when(raw: str, style: str) -> dt.datetime:
    """Read a date or timestamp written in the given style and return it in UTC.

    A date with no year is read as 2026. A date with no time counts as midnight in
    Singapore. Raises ValueError for an unknown style, text that does not fit the
    style, or a date that does not exist.
    """
    if style not in LOG_STYLES and style not in NOTE_STYLES:
        raise ValueError(f"unknown date style {style!r}")
    text = str(raw).strip()
    try:
        if style in LOG_STYLES:
            return _log_when(style, text)
        return _note_when(style, text)
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError(f"{raw!r} is not a valid {style} date: {error}") from error


def sgt_date(when: dt.datetime) -> dt.date:
    """Return the calendar date of a moment in Singapore."""
    return when.astimezone(SGT).date()


# An ISO 8601 date, or date and time, as an export writes it. The time of day and
# the time zone offset are optional.
_ISO_STAMP = (
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_ISO_STAMP_ALL = re.compile(_ISO_STAMP)


def _iso_stamp_date(text: str) -> dt.date:
    """Return the Singapore date of an ISO 8601 date or date-time.

    A stamp with a time zone is converted to Singapore time first. One with no time
    zone is taken as local time. Raises ValueError if the text is not a valid stamp.
    """
    if _ISO_STAMP_ALL.fullmatch(text) is None:
        raise ValueError(f"{text!r} is not an ISO 8601 date")
    when = dt.datetime.fromisoformat(text)
    if when.tzinfo is None:
        return when.date()
    return sgt_date(when)


def note_date(raw: object) -> dt.date | None:
    """Read a date cell in any of the note styles, or an ISO date-time, as a date.

    Returns None if the text fits no style or the date does not exist.
    """
    if not isinstance(raw, str):
        return None
    for style in NOTE_STYLES:
        try:
            return sgt_date(parse_when(raw, style))
        except ValueError:
            continue
    try:
        return _iso_stamp_date(raw.strip())
    except ValueError:
        return None


# Dates that can sit inside a longer line, such as a stock count title or the first
# line of a handover. The day and month form needs a month name, so a number like
# "5 marked" is not mistaken for a date.
_DATES_IN_TEXT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{4}\b"), "DD/MM/YYYY"),
    (re.compile(rf"\b{_ISO_STAMP}(?!\w)"), "ISO"),
    (re.compile(r"\b\d{1,2}-[A-Za-z]{3,9}-(?:\d{2}|\d{4})\b"), "D-Mon-YY"),
    (
        re.compile(
            r"\b\d{1,2}[ \t]+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"[a-z]*\b",
            re.IGNORECASE,
        ),
        "D Mon",
    ),
)


def find_date(text: str) -> dt.date | None:
    """Find the first complete date inside a line of text.

    Dates are read day first, and a date with no year means 2026. Returns None if
    there is no date, or if it names a day that does not exist.
    """
    found = sorted(
        (match.start(), match.group(0), style)
        for pattern, style in _DATES_IN_TEXT
        for match in pattern.finditer(text)
    )
    for _, date_text, style in found:
        try:
            if style == "ISO":
                return _iso_stamp_date(date_text)
            return sgt_date(parse_when(date_text.replace(".", "/"), style))
        except ValueError:
            continue
    return None


# --- Alarm codes and part numbers -----------------------------------------------

_ALARM_PREFIX = re.compile(r"(ALM|MSG|ERR)(?:[\s:_.-]+|(?=\d))", re.ASCII)
_ALARM_BODY = re.compile(r"([A-Z]*)[\s-]*(\d+)", re.ASCII)
_PRESS_FAMILY = "E"


def normalize_alarm_code(raw: str) -> str | None:
    """Turn any spelling of an alarm code into its standard form.

    For example, "ERR 217" becomes "E-217" and "VT18" becomes "VT-018". Returns None
    if the result is not a valid alarm code.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().upper().rstrip(":;,.").strip()
    press = False
    prefix = _ALARM_PREFIX.match(text)
    if prefix:
        press = prefix.group(1) == "ERR"
        text = text[prefix.end() :]
    body = _ALARM_BODY.fullmatch(text)
    if body is None:
        return None
    letters, digits = body.groups()
    if press and not letters:
        letters = _PRESS_FAMILY
    code = f"{letters}-{digits.zfill(3)}"
    return code if validate_id("AlarmCode", code) else None


def part_key(raw: str) -> str:
    """Keep only the capital letters and digits, so two spellings of a part match."""
    return re.sub(r"[^A-Z0-9]", "", "" if raw is None else str(raw).upper())


def dashed_part(raw: str) -> str | None:
    """Turn a part name into its dashed capital form, such as "SEAL-KIT-50".

    Returns None if the text has no separator or has characters a part id cannot hold.
    """
    if raw is None:
        return None
    text = re.sub(r"[\s-]+", "-", str(raw).strip().upper())
    return text if validate_id("Part", text) else None


# --- Inventory and maintenance values -------------------------------------------

_QUANTITY = re.compile(r"(\d+)(?:\s*[A-Za-z]+\.?)?", re.ASCII)
_BIN = re.compile(r"([A-Za-z]+)[\s-]*(\d+)", re.ASCII)
_LEAD_TIME = re.compile(
    r"(\d+(?:\.\d+)?)\s*(d|day|days|w|wk|wks|week|weeks)?", re.ASCII | re.IGNORECASE
)
# A duration as people write it: an optional word such as "approx", a number, and an
# optional unit. No unit means minutes, and so does any unit that starts with m, which
# covers misspellings of "minutes".
_DOWNTIME = re.compile(
    r"(?:~|[a-z]+\.?)?\s*(\d+(?:\.\d+)?)\s*"
    r"(m[a-z]{0,6}|h|hr|hrs|hour|hours)?",
    re.ASCII | re.IGNORECASE,
)
_HOUR_UNITS = {"h", "hr", "hrs", "hour", "hours"}
_LINE = re.compile(r"\bline\s*(\d+)\b", re.ASCII | re.IGNORECASE)


def _whole_number(value: object) -> int | None:
    """Return the value as a whole number, or None if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def parse_quantity(value: object) -> int | None:
    """Read a stock quantity. A dash means 0. A blank or non-numeric cell gives None."""
    if value is None:
        return None
    number = _whole_number(value)
    if number is not None or not isinstance(value, str):
        return number
    text = value.strip()
    if text == "-":
        return 0
    match = _QUANTITY.fullmatch(text)
    return int(match.group(1)) if match else None


def normalize_bin(value: object) -> str | None:
    """Write a storage bin in dashed form, so "B12" becomes "B-12".

    A blank gives None. Text that is not a letter and a number is returned as written.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _BIN.fullmatch(text)
    return f"{match.group(1).upper()}-{match.group(2)}" if match else text


def lead_time_days(value: object) -> int | None:
    """Read a lead time as days. Weeks are converted. A blank or other text gives None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return round(value) if value >= 0 else None
    match = _LEAD_TIME.fullmatch(str(value).strip())
    if match is None:
        return None
    amount, unit = float(match.group(1)), (match.group(2) or "d").lower()
    return round(amount * (7 if unit.startswith("w") else 1))


def parse_cost(value: object) -> float | None:
    """Read a cost as a number, ignoring currency signs and commas. Other text gives None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    digits = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    try:
        return float(digits)
    except ValueError:
        return None


def parse_downtime(value: object) -> int | None:
    """Read downtime as whole minutes from a cell or a phrase in a note.

    A plain number is minutes. Hours are converted. A leading word such as "approx"
    is ignored. A blank or other text gives None.
    """
    if value is None:
        return None
    number = _whole_number(value)
    if number is not None or not isinstance(value, str):
        return number
    match = _DOWNTIME.fullmatch(value.strip())
    if match is None:
        return None
    unit = (match.group(2) or "min").lower()
    minutes = float(match.group(1)) * (60 if unit in _HOUR_UNITS else 1)
    return round(minutes)


def line_id(text: str) -> str | None:
    """Return the line id for text that names a line, such as "LINE-03". Otherwise None."""
    match = _LINE.search(text or "")
    if match is None:
        return None
    candidate = f"LINE-{int(match.group(1)):02d}"
    return candidate if validate_id("Line", candidate) else None


# --- Shifts ---------------------------------------------------------------------

_SHIFT_WORD = re.compile(r"(?:shift\s+)?([abc])(?:\s+shift)?|(?:night(?:\s+shift)?)")


def parse_shift(text: object) -> str | None:
    """Return the shift letter a handover names. "Night" means C. Otherwise None."""
    if not isinstance(text, str):
        return None
    match = _SHIFT_WORD.fullmatch(text.strip().lower())
    if match is None:
        return None
    return match.group(1).upper() if match.group(1) else "C"


def shift_at(clock: dt.time) -> str:
    """Return the shift running at a clock time in Singapore."""
    if SHIFT_STARTS["A"] <= clock < SHIFT_STARTS["B"]:
        return "A"
    if SHIFT_STARTS["B"] <= clock < SHIFT_STARTS["C"]:
        return "B"
    return "C"


# --- Paths ----------------------------------------------------------------------


def document_id(doc_path: str) -> str:
    """Return a short, stable id for a raw file, made from its path."""
    return "DOC-" + hashlib.sha256(doc_path.encode("utf-8")).hexdigest()[:8]


def chunk_id(document: str, page: int | None, ord: int) -> str:
    """Return the id of a chunk, made from its document, page and position."""
    return f"{document}-{page or 0}-{ord}"


def doc_path_for(path: Path | str, raw_dir: Path = RAW_DIR) -> str:
    """Return a file path relative to the raw data folder, with forward slashes."""
    path = Path(path)
    try:
        return path.resolve().relative_to(raw_dir.resolve()).as_posix()
    except ValueError:
        return f"{path.parent.name}/{path.name}" if path.parent.name else path.name


# --- The machine alias table ----------------------------------------------------


def _name_key(text: object) -> str:
    return text.strip().casefold() if isinstance(text, str) else ""


@dataclass(frozen=True)
class MachineEntry:
    """One machine from the Machines sheet, with the fields name matching needs."""

    tag: str
    serial: str = ""
    model: str = ""
    aliases: tuple[str, ...] = ()


class AliasTable:
    """The names people and machines use for each asset, built from the Machines sheet.

    Logs name a machine by serial, notes and exports by alias or tag, and manuals
    only by model.
    """

    def __init__(self, machines: Iterable[MachineEntry]) -> None:
        self._machines = tuple(machines)
        names: dict[str, set[str]] = {}
        for machine in self._machines:
            for name in (machine.tag, machine.serial, *machine.aliases):
                key = _name_key(name)
                if key:
                    names.setdefault(key, set()).add(machine.tag)
        self._names = names

    @property
    def tags(self) -> list[str]:
        """Return the asset tags in sheet order."""
        return [machine.tag for machine in self._machines]

    def asset_for(self, text: str) -> str | None:
        """Return the tag of the asset a name refers to, ignoring case and spaces.

        Returns None if nothing matches, or if two assets share the name.
        """
        tags = self._names.get(_name_key(text), set())
        return next(iter(tags)) if len(tags) == 1 else None

    def assets_for_model(self, model: str) -> list[str]:
        """Return the tags of every asset of a model, in sheet order."""
        key = part_key(model)
        if not key:
            return []
        return [m.tag for m in self._machines if part_key(m.model) == key]

    def aliases(self, tag: str) -> list[str]:
        """Return the aliases listed for a tag. Empty for an unknown tag."""
        wanted = _name_key(tag)
        for machine in self._machines:
            if _name_key(machine.tag) == wanted:
                return list(machine.aliases)
        return []
