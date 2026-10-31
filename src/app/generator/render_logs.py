"""Write each machine's alarm log in the format its vendor uses.

Every machine gets one log file. It holds the machine's alarm events plus a regular
"still alive" line, in time order. Each vendor has its own header, time zone, time
format and spelling of alarm codes. A little export noise is added: a few lines are
cut short and a few appear twice.

render_logs() writes the files and returns what each mentions. _kessler(),
_bramwell(), _orbis() and _veltra() write one alarm line in each vendor format.
"""

import datetime as dt
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from app.generator import catalogue as cat
from app.generator.mentions import FileMentions, Mentions, file_key
from app.generator.world import World
from app.ontology import AlarmCodeProps

CUT_PROBABILITY = 0.03
DUPLICATE_PROBABILITY = 0.01
HEARTBEAT_EVERY = dt.timedelta(hours=4)
LAST_HEARTBEAT = dt.datetime(2026, 9, 30, 20, 0, tzinfo=cat.SGT)

LOG_FILES = {
    "CNC-01": "CNC01_alarms_apr-sep.log",
    "CNC-02": "cnc-02 alarm export.log",
    "HP-01": "HP01.log",
    "RW-01": "robot_cell_events.log",
    "LT-01": "leaktest_VT-LD20.log",
}

_ORBIS_SEVERITY = {"info": 1, "warning": 2, "critical": 3}

# --- The four vendor formats ----------------------------------------------------
# Each function returns the line and the way it spelled the alarm code.


def _dashless(code: str) -> str:
    prefix, number = code.split("-")
    return prefix + number


def _sgt(moment: dt.datetime, pattern: str) -> str:
    return moment.astimezone(cat.SGT).strftime(pattern)


def _kessler_time(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kessler(
    rng: random.Random, moment: dt.datetime, alarm: AlarmCodeProps, *, drop_dash: bool
) -> tuple[str, str]:
    """Kessler logs use UTC time. The CNC-02 export drops the dash from half of the
    codes."""
    code = _dashless(alarm.code) if drop_dash and rng.random() < 0.5 else alarm.code
    kind = "MSG" if alarm.severity == "info" else "ALM"
    return f"{_kessler_time(moment)} {kind} {code} {alarm.text}", code


def _bramwell(
    rng: random.Random, moment: dt.datetime, alarm: AlarmCodeProps
) -> tuple[str, str]:
    """Bramwell logs use Singapore time with the day first, capital letters and short
    text. The code takes one of three spellings."""
    number = alarm.code.split("-")[1]
    (code,) = rng.choices(
        (f"ERR {number}", alarm.code, f"Err:{number}"), weights=(60, 25, 15)
    )
    stamp = _sgt(moment, "%d/%m/%Y %H:%M:%S")
    return f"{stamp} | {code} | {alarm.text.upper()[:24]}", code


def _orbis(
    rng: random.Random, moment: dt.datetime, alarm: AlarmCodeProps
) -> tuple[str, str]:
    """Orbis logs use a plain seconds count for the time and a severity number.
    Some codes lose their dash."""
    code = _dashless(alarm.code) if rng.random() < 0.4 else alarm.code
    severity = _ORBIS_SEVERITY[alarm.severity]
    return f"{int(moment.timestamp())};{code};{alarm.text.upper()};sev={severity}", code


def _veltra(
    rng: random.Random, moment: dt.datetime, alarm: AlarmCodeProps
) -> tuple[str, str]:
    """Veltra logs use Singapore time with the year first. Some codes are shortened."""
    prefix, number = alarm.code.split("-")
    code = f"{prefix}{int(number)}" if rng.random() < 0.3 else alarm.code
    return f"[{_sgt(moment, '%Y.%m.%d %H:%M:%S')}] {code} {alarm.text}", code


@dataclass(frozen=True)
class _Style:
    header: str  # has places for the model and serial number
    alarm: Callable[[random.Random, dt.datetime, AlarmCodeProps], tuple[str, str]]
    heartbeat: Callable[[dt.datetime], str]


_KESSLER_HEADER = "# Kessler {model} alarm history export, machine {serial}"


def _kessler_beat(moment: dt.datetime) -> str:
    return f"{_kessler_time(moment)} HB ok"


_STYLES = {
    "CNC-01": _Style(
        _KESSLER_HEADER, partial(_kessler, drop_dash=False), _kessler_beat
    ),
    "CNC-02": _Style(_KESSLER_HEADER, partial(_kessler, drop_dash=True), _kessler_beat),
    "HP-01": _Style(
        "BRAMWELL {model} EVENT LOG  S/N {serial}",
        _bramwell,
        lambda moment: f"{_sgt(moment, '%d/%m/%Y %H:%M:%S')} | --- | HEARTBEAT",
    ),
    "RW-01": _Style(
        "#orbis-cell {model} sn={serial} tz=UTC",
        _orbis,
        lambda moment: f"{int(moment.timestamp())};HB;;sev=0",
    ),
    "LT-01": _Style(
        "Veltra {model} station log / SN {serial} / local time",
        _veltra,
        lambda moment: f"[{_sgt(moment, '%Y.%m.%d %H:%M:%S')}] -- idle",
    ),
}

# --- One log -------------------------------------------------------------------

# A log line, the alarm code it carries (none for a still-alive line) and the way the
# code was spelled.
_Line = tuple[str, str | None, str]


def _heartbeat_times() -> Iterator[dt.datetime]:
    moment = cat.PERIOD_START
    while moment <= LAST_HEARTBEAT:
        yield moment
        moment += HEARTBEAT_EVERY


def _body(world: World, rng: random.Random, asset_id: str) -> list[_Line]:
    """The log lines in time order. A still-alive line comes before an alarm in the same
    second."""
    style = _STYLES[asset_id]
    entries: list[tuple[dt.datetime, int, AlarmCodeProps | None]] = [
        (moment, 0, None) for moment in _heartbeat_times()
    ]
    entries += [
        (event.timestamp, 1, cat.ALARM_BY_CODE[event.code].props)
        for event in world.alarm_events
        if event.asset == asset_id
    ]
    entries.sort(key=lambda entry: entry[:2])
    lines: list[_Line] = []
    for moment, _, alarm in entries:
        if alarm is None:
            lines.append((style.heartbeat(moment), None, ""))
        else:
            text, written = style.alarm(rng, moment, alarm)
            lines.append((text, alarm.code, written))
    return lines


def _add_noise(rng: random.Random, lines: list[_Line]) -> list[_Line]:
    """Cut a few lines short and repeat a few others."""
    noisy: list[_Line] = []
    for text, code, written in lines:
        if rng.random() < CUT_PROBABILITY:
            text = text[: rng.randint(1, len(text) - 1)]
        noisy.append((text, code, written))
        if rng.random() < DUPLICATE_PROBABILITY:
            noisy.append((text, code, written))
    return noisy


def render_logs(world: World, rng: random.Random, out: Path) -> Mentions:
    """Write the five logs. Each file mentions its machine and the alarm codes still
    readable in it, since a cut line can lose its code."""
    folder = out / "raw" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    mentions: Mentions = {}
    for asset in cat.ASSETS:
        header = _STYLES[asset.id].header.format(
            model=asset.model, serial=cat.SERIALS[asset.id]
        )
        body = _add_noise(rng, _body(world, rng, asset.id))
        path = folder / LOG_FILES[asset.id]
        text = "\n".join([header, *(line for line, _, _ in body)]) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        mentions[file_key(out, path)] = FileMentions(
            assets=[asset.id],
            alarm_codes=[
                code for line, code, written in body if code and written in line
            ],
        )
    return mentions
