"""Checks whether a question names a production line that the plant does not have.

A question such as "Line 4 robot is down" is about a line the plant lacks, so no model
should guess a machine for it. Triage turns it into a question for the technician.

The checks skip phrases that only look like a line, such as a line in an NC program,
file, log or document, or a measurement like "line 2 bar". unknown_line is the entry
point. exact_asset_ids finds machine ids written in the question, and line_of gives
the line a machine belongs to.
"""

import json
import re
import sqlite3

PROGRAM_WORDS = frozenset(
    {
        "program",
        "programs",
        "programme",
        "subprogram",
        "subroutine",
        "macro",
        "script",
        "g-code",
        "gcode",
        "file",
        "files",
        "log",
        "logs",
        "page",
        "pages",
        "sheet",
        "spreadsheet",
        "csv",
        "drawing",
        "listing",
        "printout",
    }
)
UNITS = (
    "bar",
    "mbar",
    "psi",
    "mpa",
    "kpa",
    "pa",
    "mm",
    "cm",
    "m",
    "um",
    "min",
    "mins",
    "s",
    "sec",
    "h",
    "hr",
    "l",
    "lpm",
    "kg",
    "n",
    "nm",
    "rpm",
    "c",
    "degc",
)
NUMBER_WORDS = {
    word: number
    for number, word in enumerate(
        "one two three four five six seven eight nine ten eleven twelve".split(),
        start=1,
    )
}

_LINE = re.compile(
    rf"\bline(?:[\s-]*(\d{{1,2}})|[\s-]+({'|'.join(NUMBER_WORDS)}))(?!\w)",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
_TOKEN = r"([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)"
# the word right before "line N"
_BEFORE = re.compile(_TOKEN + r"[\s,:(]*$")
# a document word and a number before "line N", as in "page 3 line 12"
_BEFORE_NUMBERED = re.compile(
    _TOKEN + r"\s+(?:\d+|" + "|".join(NUMBER_WORDS) + r")[\s,:(]*$", re.IGNORECASE
)
# a unit right after the number, as in "2 bar" or "3mm"
_UNIT = re.compile(
    r"\s*(?:%|(?:" + "|".join(UNITS) + r")(?![A-Za-z0-9]))", re.IGNORECASE
)
_AT = re.compile(r"\bat[\s,:(]*$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+|\n")
# "of the log" or "in the file" right after "line N"
_AFTER = re.compile(r"\s+(?:of|in|on)\s+(?:the\s+)?" + _TOKEN, re.IGNORECASE)


def plant_lines(conn: sqlite3.Connection) -> set[int]:
    """The line numbers the plant has."""
    numbers = set()
    for row in conn.execute("SELECT id, label, props FROM nodes WHERE type = 'Line'"):
        name = json.loads(row["props"]).get("name") or row["label"] or row["id"]
        found = re.search(r"\d+", name) or re.search(r"\d+", row["id"])
        if found:
            numbers.add(int(found.group()))
    return numbers


def _marked(question: str, match: re.Match) -> bool:
    """Whether "line N" means a position in a program, file or document, or a
    measurement, rather than a production line."""
    if _UNIT.match(question, match.end()):
        return True
    head = question[: match.start()]
    near = (
        _BEFORE.search(head),
        _BEFORE_NUMBERED.search(head),
        _AFTER.match(question, match.end()),
    )
    if any(found and found.group(1).casefold() in PROGRAM_WORDS for found in near):
        return True
    if _AT.search(head):
        sentence = _SENTENCE_END.split(head)[-1]
        return any(word.casefold() in PROGRAM_WORDS for word in _WORD.findall(sentence))
    return False


def named_lines(question: str) -> list[int]:
    """The production line numbers the question names, in order."""
    named = []
    for match in _LINE.finditer(question):
        if _marked(question, match):
            continue
        digits, word = match.groups()
        named.append(int(digits) if digits else NUMBER_WORDS[word.casefold()])
    return named


def unknown_line(question: str, conn: sqlite3.Connection) -> int | None:
    """The first named line the plant does not have, or None."""
    lines = plant_lines(conn)
    return next((n for n in named_lines(question) if n not in lines), None)


def exact_asset_ids(question: str, conn: sqlite3.Connection) -> list[str]:
    """The machine ids written in the question, not aliases or model numbers."""
    ids = {
        row["id"] for row in conn.execute("SELECT id FROM nodes WHERE type = 'Asset'")
    }
    seen = (word.upper() for word in _WORD.findall(question))
    return sorted({word for word in seen if word in ids})


def line_of(conn: sqlite3.Connection, asset_id: str) -> str | None:
    """The name of the line the machine belongs to, or None."""
    row = conn.execute(
        "SELECT n.label, n.props FROM edges e JOIN nodes n ON n.id = e.src"
        " WHERE e.rel = 'HAS_MACHINE' AND e.dst = ? AND n.type = 'Line' ORDER BY n.id",
        (asset_id,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["props"]).get("name") or row["label"]
