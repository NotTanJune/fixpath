"""Tests for the generated machine logs.

Every render goes to a temporary folder. Each percentage is a chance per line, so the
tests use a random source that always returns one chosen number, which makes each
case exact instead of counting samples."""

import datetime as dt
import random
import re
from pathlib import Path

import pytest
from conftest import ALARM, SEED, FixedRandom

from app.generator import catalogue
from app.generator.render_logs import render_logs
from app.generator.world import World

SGT = dt.timezone(dt.timedelta(hours=8))
UTC = dt.UTC

# --- Machine logs ------------------------------------------------------------------

LOG_FILES = {
    "CNC-01": "CNC01_alarms_apr-sep.log",
    "CNC-02": "cnc-02 alarm export.log",
    "HP-01": "HP01.log",
    "RW-01": "robot_cell_events.log",
    "LT-01": "leaktest_VT-LD20.log",
}
LOG_HEADERS = {
    "CNC-01": "# Kessler KX-500 alarm history export, machine KX5-10442",
    "CNC-02": "# Kessler KX-500 alarm history export, machine KX5-10457",
    "HP-01": "BRAMWELL BH-250 EVENT LOG  S/N BH250-0091",
    "RW-01": "#orbis-cell OR-6 sn=OR6-2231 tz=UTC",
    "LT-01": "Veltra VT-LD20 station log / SN VT20-5512 / local time",
}
SEVERITY_NUMBER = {"info": 1, "warning": 2, "critical": 3}
FIRST_HEARTBEAT = dt.datetime(2026, 4, 1, 0, 0, tzinfo=SGT)
LAST_HEARTBEAT = dt.datetime(2026, 9, 30, 20, 0, tzinfo=SGT)


def heartbeat_times() -> list[dt.datetime]:
    times = [FIRST_HEARTBEAT]
    while times[-1] < LAST_HEARTBEAT:
        times.append(times[-1] + dt.timedelta(hours=4))
    return times


def spelling(asset: str, code: str, u: float) -> str:
    """How the brief spells a code in the log of this machine, given the number the
    random generator returns."""
    prefix, number = code.split("-")
    if asset == "CNC-02":
        return prefix + number if u < 0.5 else code
    if asset == "HP-01":
        return f"ERR {number}" if u < 0.6 else code if u < 0.85 else f"Err:{number}"
    if asset == "RW-01":
        return prefix + number if u < 0.4 else code
    if asset == "LT-01":
        return f"{prefix}{int(number)}" if u < 0.3 else code
    return code


def all_spellings(asset: str, code: str) -> set[str]:
    return {spelling(asset, code, u) for u in (0.0, 0.5, 0.7, 0.9)}


def log_line(asset: str, moment: dt.datetime, code_text: str | None, code: str | None):
    """The line the brief gives for an alarm, or for a heartbeat when code is None."""
    alarm = ALARM[code] if code else None
    if asset in ("CNC-01", "CNC-02"):
        stamp = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if alarm is None:
            return f"{stamp} HB ok"
        kind = "MSG" if alarm.severity == "info" else "ALM"
        return f"{stamp} {kind} {code_text} {alarm.text}"
    if asset == "HP-01":
        stamp = moment.astimezone(SGT).strftime("%d/%m/%Y %H:%M:%S")
        if alarm is None:
            return f"{stamp} | --- | HEARTBEAT"
        return f"{stamp} | {code_text} | {alarm.text.upper()[:24]}"
    if asset == "RW-01":
        stamp = int(moment.timestamp())
        if alarm is None:
            return f"{stamp};HB;;sev=0"
        severity = SEVERITY_NUMBER[alarm.severity]
        return f"{stamp};{code_text};{alarm.text.upper()};sev={severity}"
    stamp = moment.astimezone(SGT).strftime("%Y.%m.%d %H:%M:%S")
    if alarm is None:
        return f"[{stamp}] -- idle"
    return f"[{stamp}] {code_text} {alarm.text}"


def expected_log(world: World, asset: str, u: float) -> list[str]:
    """Header and body of a log that has no export noise, when every coin flip
    returns u. A heartbeat sorts before an alarm at the same second."""
    entries = [(moment, 0, None) for moment in heartbeat_times()]
    entries += [
        (event.timestamp, 1, event.code)
        for event in world.alarm_events
        if event.asset == asset
    ]
    entries.sort(key=lambda entry: entry[:2])
    body = [
        log_line(asset, moment, spelling(asset, code, u) if code else None, code)
        for moment, _, code in entries
    ]
    return [LOG_HEADERS[asset], *body]


def acceptable_lines(world: World, asset: str) -> set[str]:
    """Every full line a log of this machine may hold, in any allowed spelling."""
    lines = {log_line(asset, moment, None, None) for moment in heartbeat_times()}
    for event in world.alarm_events:
        if event.asset == asset:
            lines |= {
                log_line(asset, event.timestamp, text, event.code)
                for text in all_spellings(asset, event.code)
            }
    return lines


def alarm_line_pattern(asset: str) -> re.Pattern[str]:
    """Matches a line that holds any spelling of any alarm code of the machine."""
    spellings = set()
    for spec in catalogue.ALARMS:
        if asset in spec.machines:
            spellings |= {spec.props.code, *spec.props.aliases}
    names = "|".join(re.escape(s) for s in sorted(spellings, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{names})(?![A-Za-z0-9])")


def read_log(folder: Path, asset: str) -> list[str]:
    text = (folder / LOG_FILES[asset]).read_text(encoding="utf-8")
    assert text.endswith("\n")
    return text.splitlines()


@pytest.fixture(scope="module")
def logs(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("logs")
    mentions = render_logs(world, random.Random(SEED + 1), out)
    return out / "raw" / "logs", mentions


def render_logs_with(world: World, value: float, out: Path) -> Path:
    render_logs(world, FixedRandom(value), out)
    return out / "raw" / "logs"


def test_the_five_log_files_have_exactly_these_names(logs):
    folder, _ = logs
    assert sorted(path.name for path in folder.iterdir()) == sorted(LOG_FILES.values())


@pytest.mark.parametrize("asset", list(LOG_FILES))
def test_each_log_starts_with_its_header(logs, asset):
    folder, _ = logs
    assert read_log(folder, asset)[0] == LOG_HEADERS[asset]


@pytest.mark.parametrize("asset", list(LOG_FILES))
def test_each_log_has_between_1000_and_2500_lines(logs, asset):
    folder, _ = logs
    assert 1000 <= len(read_log(folder, asset)) <= 2500


@pytest.mark.parametrize("asset", list(LOG_FILES))
def test_alarm_code_lines_are_95_to_102_percent_of_the_truth_events(world, logs, asset):
    folder, _ = logs
    pattern = alarm_line_pattern(asset)
    found = sum(1 for line in read_log(folder, asset)[1:] if pattern.search(line))
    truth = sum(1 for event in world.alarm_events if event.asset == asset)
    assert 0.95 * truth <= found <= 1.02 * truth, (found, truth)


@pytest.mark.parametrize("asset", list(LOG_FILES))
def test_every_log_line_is_a_real_line_or_a_cut_of_one(world, logs, asset):
    """No invented lines and no spelling outside the brief's. Cut lines are
    prefixes of real ones, and a duplicate sits right after its original."""
    folder, _ = logs
    acceptable = acceptable_lines(world, asset)
    for line in read_log(folder, asset)[1:]:
        assert line in acceptable or any(a.startswith(line) for a in acceptable), line


@pytest.mark.parametrize("asset", list(LOG_FILES))
def test_the_intact_lines_run_in_time_order(world, logs, asset):
    folder, _ = logs
    position = {}
    for u in (0.0, 0.5, 0.7, 0.9):
        for index, line in enumerate(expected_log(world, asset, u)[1:]):
            position[line] = index
    seen = [position[line] for line in read_log(folder, asset)[1:] if line in position]
    assert seen == sorted(seen)


@pytest.mark.parametrize(
    "u", [0.031, 0.29, 0.31, 0.39, 0.41, 0.49, 0.51, 0.59, 0.61, 0.84, 0.86, 0.95]
)
def test_the_line_formats_and_code_spellings_follow_the_brief(world, tmp_path, u):
    """With no noise (u is at least 0.03), a log is exactly the brief's lines. The
    values of u fall just under and just over each spelling threshold: 0.3 for the
    short Veltra form, 0.4 for the Orbis code without the dash, 0.5 for the CNC-02
    code without the dash, and 0.6 and 0.85 for the three Bramwell spellings."""
    folder = render_logs_with(world, u, tmp_path)
    for asset in LOG_FILES:
        assert read_log(folder, asset) == expected_log(world, asset, u), asset


def test_cnc_01_never_drops_the_dash_and_cnc_02_does(world, tmp_path):
    drop = render_logs_with(world, 0.05, tmp_path)
    assert not re.search(r"\b(SP|CL|TC)\d{3}\b", "\n".join(read_log(drop, "CNC-01")))
    assert re.search(r"\bSP\d{3}\b", "\n".join(read_log(drop, "CNC-02")))


def test_the_heartbeats_run_every_four_hours_from_april_to_september(world, tmp_path):
    folder = render_logs_with(world, 0.5, tmp_path)
    times = heartbeat_times()
    assert times[0] == FIRST_HEARTBEAT and times[-1] == LAST_HEARTBEAT
    assert len(times) == 1098
    for asset in LOG_FILES:
        lines = read_log(folder, asset)
        beats = [log_line(asset, moment, None, None) for moment in times]
        beat_set = set(beats)
        assert [line for line in lines if line in beat_set] == beats, asset


@pytest.mark.parametrize(
    ("u", "cuts", "duplicates"),
    [
        (0.5, False, False),
        (0.031, False, False),
        (0.029, True, False),
        (0.011, True, False),
        (0.009, True, True),
    ],
)
def test_export_noise_cuts_three_percent_and_duplicates_one_percent(
    world, tmp_path, u, cuts, duplicates
):
    """A line is cut when random() is under 0.03 and duplicated when it is under
    0.01. The header is never touched."""
    folder = render_logs_with(world, u, tmp_path)
    for asset in LOG_FILES:
        clean = expected_log(world, asset, u)
        noisy = read_log(folder, asset)
        assert noisy[0] == clean[0]
        body = noisy[1:]
        if duplicates:
            assert len(body) == 2 * (len(clean) - 1)
            assert body[0::2] == body[1::2]
            body = body[0::2]
        else:
            assert len(body) == len(clean) - 1
        for original, line in zip(clean[1:], body, strict=True):
            if cuts:
                assert 0 < len(line) < len(original) and original.startswith(line)
            else:
                assert line == original


def codes_in_text(folder: Path, asset: str) -> list[str]:
    """The canonical alarm codes that still appear in a log, in any spelling."""
    pattern = alarm_line_pattern(asset)
    canonical = {
        text: spec.props.code
        for spec in catalogue.ALARMS
        if asset in spec.machines
        for text in {spec.props.code, *spec.props.aliases}
    }
    return sorted(
        {
            canonical[match.group()]
            for line in read_log(folder, asset)[1:]
            for match in pattern.finditer(line)
        }
    )


def test_a_log_mentions_its_asset_and_the_alarm_codes_that_appear_in_it(logs):
    folder, mentions = logs
    assert sorted(mentions) == sorted(f"raw/logs/{name}" for name in LOG_FILES.values())
    for asset, name in LOG_FILES.items():
        mention = mentions[f"raw/logs/{name}"]
        assert mention.assets == [asset]
        assert (
            mention.incidents == [] and mention.parts == [] and mention.conflicts == []
        )
        assert mention.alarm_codes == codes_in_text(folder, asset)


def test_a_code_that_every_cut_removed_is_not_mentioned(world, tmp_path):
    """With every line cut, some codes lose all their spellings. A log only
    mentions the codes that still appear in its text."""
    mentions = render_logs(world, FixedRandom(0.02), tmp_path)
    folder = tmp_path / "raw" / "logs"
    lost = 0
    for asset, name in LOG_FILES.items():
        mentioned = mentions[f"raw/logs/{name}"].alarm_codes
        assert mentioned == codes_in_text(folder, asset)
        truth = {event.code for event in world.alarm_events if event.asset == asset}
        lost += len(truth - set(mentioned))
    assert lost > 0
