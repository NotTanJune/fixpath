"""Reads the machine event logs, one per machine, each in its vendor's format.

parse_log reads one log and reports the machine, each alarm code in it, and how often
and when each alarm appeared. is_log_header tells the file router whether a text file
is one of these logs.
"""

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.ingest.evidence import CUT_TEXT_CONFIDENCE, LOG_CONFIDENCE, counted, quote
from app.ingest.normalize import (
    AliasTable,
    doc_path_for,
    normalize_alarm_code,
    parse_when,
)
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    Extraction,
    NodeRef,
)
from app.ontology import validate_id


@dataclass(frozen=True)
class _LogFormat:
    """How one vendor writes its event log: the header line, event lines and idle
    lines. Each pattern must match the whole line."""

    vendor: str
    header: re.Pattern[str]
    event: re.Pattern[str]
    heartbeat: re.Pattern[str]
    style: str  # how the vendor writes timestamps
    cuts_text: bool = False  # the log cuts long messages short


_LOG_FORMATS = (
    _LogFormat(
        vendor="Kessler",
        header=re.compile(r"#\s*Kessler\b.*\bmachine\s+(?P<serial>\S+).*"),
        event=re.compile(
            r"(?P<when>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (?:ALM|MSG) "
            r"(?P<code>\S+) (?P<text>\S.*)"
        ),
        heartbeat=re.compile(r"\S+ HB\b.*"),
        style="iso_utc",
    ),
    _LogFormat(
        vendor="Bramwell",
        header=re.compile(r"BRAMWELL\b.*\bS/N\s+(?P<serial>\S+).*", re.IGNORECASE),
        event=re.compile(
            r"(?P<when>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}) \| "
            r"(?P<code>[^|]+?) \| (?P<text>\S.*)"
        ),
        heartbeat=re.compile(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2} \| --- \|.*"),
        style="ddmmyyyy_sgt",
        cuts_text=True,
    ),
    _LogFormat(
        vendor="Orbis",
        header=re.compile(r"#orbis-cell\b.*\bsn=(?P<serial>\S+).*"),
        event=re.compile(
            r"(?P<when>\d{9,11});(?P<code>[^;]+);(?P<text>[^;]+);sev=(?P<sev>\d)"
        ),
        heartbeat=re.compile(r"\d+;HB(?:;.*)?"),
        style="epoch",
    ),
    _LogFormat(
        vendor="Veltra",
        header=re.compile(r"Veltra\b.*\bSN\s+(?P<serial>\S+).*"),
        event=re.compile(
            r"\[(?P<when>\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})\] "
            r"(?P<code>\S+) (?P<text>\S.*)"
        ),
        heartbeat=re.compile(r"\[[^\]]*\] -- idle"),
        style="dotted_sgt",
    ),
)

# Orbis writes severity as a number from 1 (info) to 3 (critical).
_ORBIS_SEVERITY = {"1": "info", "2": "warning", "3": "critical"}


def _header_format(header: str) -> tuple[_LogFormat, str] | None:
    """The vendor format and machine serial named by a log's header line."""
    for fmt in _LOG_FORMATS:
        match = fmt.header.fullmatch(header.strip())
        if match:
            return fmt, match["serial"]
    return None


def is_log_header(line: str) -> bool:
    """Whether the line is the header of a known vendor's event log."""
    return _header_format(line) is not None


@dataclass
class _Seen:
    """What a log has said so far about one alarm code."""

    count: int
    first_seen: dt.datetime
    last_seen: dt.datetime
    line: str  # the first line where the code appears
    text: str
    severity: str | None


def _event(
    fmt: _LogFormat, line: str
) -> tuple[str, dt.datetime, str, str | None] | None:
    """The code, time, text and severity of an event line, or None if the line is cut
    short, garbled or not an alarm."""
    match = fmt.event.fullmatch(line)
    if match is None:
        return None
    code = normalize_alarm_code(match["code"])
    if code is None:
        return None
    try:
        when = parse_when(match["when"], fmt.style)
    except ValueError:
        return None
    severity = _ORBIS_SEVERITY.get(match.groupdict().get("sev") or "")
    return code, when, match["text"].strip(), severity


def _read_events(
    fmt: _LogFormat, lines: list[str]
) -> tuple[dict[str, _Seen], int, int]:
    """Group the event lines by alarm code. Also counts unreadable lines and exact
    repeats of the line before, which are dropped. Idle lines are skipped."""
    seen: dict[str, _Seen] = {}
    unparseable = duplicates = 0
    previous = None
    for line in lines:
        repeated = line == previous
        previous = line
        if fmt.heartbeat.fullmatch(line):
            continue
        if repeated:
            duplicates += 1
            continue
        event = _event(fmt, line)
        if event is None:
            unparseable += 1
            continue
        code, when, text, severity = event
        entry = seen.get(code)
        if entry is None:
            seen[code] = _Seen(1, when, when, line, text, severity)
            continue
        entry.count += 1
        entry.first_seen = min(entry.first_seen, when)
        entry.last_seen = max(entry.last_seen, when)
        if len(text) > len(entry.text):
            entry.text = text
        entry.severity = entry.severity or severity
    return seen, unparseable, duplicates


def parse_log(
    path: Path, aliases: AliasTable, settings: Settings | None = None
) -> Extraction:
    """Read one machine's event log.

    The header line gives the vendor and the machine. Idle lines and exact repeats are
    skipped, and unreadable lines are counted in the notes. The longest message seen
    for a code is kept, since some exports cut messages short. Settings are unused,
    because no model reads a log.
    """
    path = Path(path)
    doc_path = doc_path_for(path)
    extraction = Extraction(doc_path=doc_path, doc_type="log", extractor="log-parser")
    notes = extraction.notes
    filled = [
        line
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if line.strip()
    ]
    if not filled:
        notes.append("the log is empty")
        return extraction
    header, body = filled[0], filled[1:]
    known = _header_format(header)
    if known is None:
        notes.append(f"log header not recognised: {header[:80]!r}")
        return extraction
    fmt, serial = known
    seen, unparseable, duplicates = _read_events(fmt, body)

    tag = aliases.asset_for(serial)
    valid = tag is not None and validate_id("Asset", tag)
    asset = NodeRef(type="Asset", key=tag if valid else serial)
    extraction.nodes.append(
        CandidateNode(
            ref=asset,
            label=asset.key,
            props={"id": tag} if valid else {},
            evidence=quote(doc_path, header.strip(), "log-parser", LOG_CONFIDENCE),
            canonical=valid,
        )
    )
    if tag is None:
        notes.append(f"serial {serial} is not in the Machines sheet")
    elif not valid:
        notes.append(f"tag {tag} for serial {serial} is not a valid asset id")
    text_confidence = CUT_TEXT_CONFIDENCE if fmt.cuts_text else LOG_CONFIDENCE
    for code in sorted(seen):
        entry = seen[code]
        props = {"code": code, "text": entry.text}
        if entry.severity:
            props["severity"] = entry.severity
        alarm = NodeRef(type="AlarmCode", key=code)
        extraction.nodes.append(
            CandidateNode(
                ref=alarm,
                label=code,
                props=props,
                evidence=quote(doc_path, entry.line, "log-parser", text_confidence),
                canonical=True,
            )
        )
        extraction.edges.append(
            CandidateEdge(
                src=asset,
                rel="RAISED",
                dst=alarm,
                props={
                    "count": entry.count,
                    "first_seen": entry.first_seen.isoformat(),
                    "last_seen": entry.last_seen.isoformat(),
                },
                evidence=quote(doc_path, entry.line, "log-parser", LOG_CONFIDENCE),
            )
        )
    if unparseable:
        notes.append(f"{unparseable} of {len(body)} lines could not be parsed")
    if duplicates:
        notes.append(
            counted(
                duplicates, "{n} duplicate line dropped", "{n} duplicate lines dropped"
            )
        )
    return extraction
