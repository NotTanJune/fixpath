"""Tests for the machine log parser. They use small hand-written logs for each
vendor and the five real logs, which are checked against a separate reading of the
files. No test calls a model; a call would fail the test.
"""

import datetime as dt
import re
from pathlib import Path

import pytest
from ingest_stubs import (
    ALIASES,
    count_by_type,
    no_model,  # noqa: F401  (an autouse fixture)
    nodes_of,
)

from app.config import RAW_DIR
from app.ingest import parsers
from app.ingest.normalize import AliasTable, MachineEntry
from app.ingest.types import CandidateEdge, CandidateNode, Extraction


# --- The machine logs: small files ----------------------------------------------

KESSLER = """\
# Kessler KX-500 alarm history export, machine KX5-10442
2026-03-31T16:00:00Z HB ok
2026-03-31T22:30:00Z MSG SP-001 Warm-up cycle complete
2026-04-03T02:14:55Z ALM SP-104 Spindle temperature high
2026-04-03T02:14:55Z ALM SP-104 Spindle temperature high
2026-04-03T02:20:00Z ALM SP104 Spindle temperature hi
2026-04-04T00:00:00Z HB ok
2026-04-0
2026-04-05T01:00:00Z ALM SP-1
2026-04-06T22:30:00Z MSG SP001 Warm-up cycle complete
"""

BRAMWELL = """\
BRAMWELL BH-250 EVENT LOG  S/N BH250-0091
01/04/2026 00:00:00 | --- | HEARTBEAT
01/04/2026 06:45:00 | ERR 200 | PUMP START
03/04/2026 10:14:55 | ERR 217 | MAIN CYLINDER PRESSURE L
04/04/2026 09:00:00 | E-217 | MAIN CYLINDER PRESSURE
05/04/2026 09:00:00 | Err:217 | MAIN CYLINDER PRESSURE L
02/04/2026 04:00:00 | --- | HEARTBE
06/04/2026 09:00:00 | ERR 21
"""

ORBIS = """\
#orbis-cell OR-6 sn=OR6-2231 tz=UTC
1774972800;HB;;sev=0
1774994594;RB-551;GRIPPER PART-PRESENT SIGNAL LOST;sev=2
1775045976;RB-563;AXIS 2 FOLLOWING ERROR;sev=3
1775550386;RB551;GRIPPER PART-PRESENT SIGNAL LOST;sev=2
17758
1775635200;HB;;
1775700000;RB-560;AXIS 2 OVERCURRENT;sev=1
"""

VELTRA = """\
Veltra VT-LD20 station log / SN VT20-5512 / local time
[2026.04.01 00:00:00] -- idle
[2026.04.02 01:42:50] VT-012 Reference pressure out of tolerance
[2026.04.04 19:36:34] VT21 Fill time exceeded
[2026.04.05 20:0
[2026.04.08 09:46:15] VT-021 Fill time exceeded
"""


def write_log(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def alarms_by_code(extraction: Extraction) -> dict[str, CandidateNode]:
    return {node.ref.key: node for node in nodes_of(extraction, "AlarmCode")}


def raised(extraction: Extraction) -> dict[str, CandidateEdge]:
    return {edge.dst.key: edge for edge in extraction.edges if edge.rel == "RAISED"}


def test_a_kessler_log_gives_the_asset_the_codes_and_the_raised_edges(tmp_path):
    path = write_log(tmp_path, "cnc.log", KESSLER)
    extraction = parsers.parse_log(path, ALIASES)
    assert extraction.doc_path.endswith("/cnc.log")
    assert (extraction.doc_type, extraction.extractor) == ("log", "log-parser")
    assert extraction.chunks == []

    (asset,) = nodes_of(extraction, "Asset")
    assert (asset.ref.key, asset.canonical, asset.props) == (
        "CNC-01",
        True,
        {"id": "CNC-01"},
    )
    assert asset.evidence[0].quote == KESSLER.splitlines()[0]

    codes = alarms_by_code(extraction)
    assert list(codes) == ["SP-001", "SP-104"]
    assert all(node.canonical for node in codes.values())
    assert codes["SP-001"].props == {"code": "SP-001", "text": "Warm-up cycle complete"}
    # the longest text wins over a copy that the export cut short
    assert codes["SP-104"].props["text"] == "Spindle temperature high"
    assert "severity" not in codes["SP-104"].props

    edges = raised(extraction)
    assert list(edges) == ["SP-001", "SP-104"]
    assert all(e.src == asset.ref and e.rel == "RAISED" for e in edges.values())
    assert edges["SP-001"].props == {
        "count": 2,
        "first_seen": "2026-03-31T22:30:00+00:00",
        "last_seen": "2026-04-06T22:30:00+00:00",
    }
    # the exact duplicate of the first SP-104 line is dropped, the variant spelling counts
    assert edges["SP-104"].props == {
        "count": 2,
        "first_seen": "2026-04-03T02:14:55+00:00",
        "last_seen": "2026-04-03T02:20:00+00:00",
    }
    (evidence,) = edges["SP-104"].evidence
    assert evidence.quote == "2026-04-03T02:14:55Z ALM SP-104 Spindle temperature high"
    assert (evidence.extractor, evidence.page) == ("log-parser", None)
    assert evidence.doc_path == extraction.doc_path
    assert 0 < evidence.confidence <= 1
    (code_evidence,) = codes["SP-104"].evidence
    assert code_evidence.quote == evidence.quote

    assert extraction.notes == [
        "2 of 9 lines could not be parsed",
        "1 duplicate line dropped",
    ]


def test_a_code_cut_off_inside_the_code_is_not_read_as_another_code(tmp_path):
    text = "# Kessler KX-500 alarm history export, machine KX5-10442\n"
    text += "2026-04-05T01:00:00Z ALM SP-1\n"
    text += "2026-04-05T01:00:00Z ALM SP-10 \n"
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES)
    assert nodes_of(extraction, "AlarmCode") == []
    assert extraction.notes == ["2 of 2 lines could not be parsed"]


def test_a_bramwell_log_reads_sgt_times_and_every_spelling_of_the_code(tmp_path):
    path = write_log(tmp_path, "hp.log", BRAMWELL)
    extraction = parsers.parse_log(path, ALIASES)
    (asset,) = nodes_of(extraction, "Asset")
    assert (asset.ref.key, asset.canonical) == ("HP-01", True)
    codes = alarms_by_code(extraction)
    assert list(codes) == ["E-200", "E-217"]
    assert codes["E-217"].props == {
        "code": "E-217",
        "text": "MAIN CYLINDER PRESSURE L",
    }
    edges = raised(extraction)
    assert edges["E-200"].props == {
        "count": 1,
        "first_seen": "2026-03-31T22:45:00+00:00",
        "last_seen": "2026-03-31T22:45:00+00:00",
    }
    assert edges["E-217"].props == {
        "count": 3,
        "first_seen": "2026-04-03T02:14:55+00:00",
        "last_seen": "2026-04-05T01:00:00+00:00",
    }
    assert edges["E-217"].evidence[0].quote == (
        "03/04/2026 10:14:55 | ERR 217 | MAIN CYLINDER PRESSURE L"
    )
    # a cut heartbeat is still a heartbeat; a cut alarm line is not parseable
    assert extraction.notes == ["1 of 7 lines could not be parsed"]


def test_the_bramwell_text_is_marked_as_less_certain_than_a_full_message(tmp_path):
    bramwell = parsers.parse_log(write_log(tmp_path, "hp.log", BRAMWELL), ALIASES)
    kessler = parsers.parse_log(write_log(tmp_path, "cnc.log", KESSLER), ALIASES)
    cut = alarms_by_code(bramwell)["E-217"].evidence[0].confidence
    full = alarms_by_code(kessler)["SP-104"].evidence[0].confidence
    assert cut < full
    # the edge is about the event, which the line states in full
    assert raised(bramwell)["E-217"].evidence[0].confidence == full


def test_an_orbis_log_takes_the_severity_from_the_sev_field(tmp_path):
    path = write_log(tmp_path, "robot.log", ORBIS)
    extraction = parsers.parse_log(path, ALIASES)
    (asset,) = nodes_of(extraction, "Asset")
    assert asset.ref.key == "RW-01"
    codes = alarms_by_code(extraction)
    assert list(codes) == ["RB-551", "RB-560", "RB-563"]
    assert codes["RB-551"].props == {
        "code": "RB-551",
        "text": "GRIPPER PART-PRESENT SIGNAL LOST",
        "severity": "warning",
    }
    assert codes["RB-560"].props["severity"] == "info"
    assert codes["RB-563"].props["severity"] == "critical"
    edges = raised(extraction)
    assert edges["RB-551"].props == {
        "count": 2,
        "first_seen": "2026-03-31T22:03:14+00:00",
        "last_seen": "2026-04-07T08:26:26+00:00",
    }
    assert extraction.notes == ["1 of 7 lines could not be parsed"]


def test_an_orbis_severity_outside_one_to_three_is_left_out(tmp_path):
    text = "#orbis-cell OR-6 sn=OR6-2231 tz=UTC\n1774994594;RB-551;GRIPPER;sev=9\n"
    extraction = parsers.parse_log(write_log(tmp_path, "r.log", text), ALIASES)
    assert "severity" not in alarms_by_code(extraction)["RB-551"].props


def test_a_veltra_log_reads_sgt_times_and_the_short_code_form(tmp_path):
    path = write_log(tmp_path, "lt.log", VELTRA)
    extraction = parsers.parse_log(path, ALIASES)
    (asset,) = nodes_of(extraction, "Asset")
    assert asset.ref.key == "LT-01"
    assert list(alarms_by_code(extraction)) == ["VT-012", "VT-021"]
    edges = raised(extraction)
    assert edges["VT-012"].props["first_seen"] == "2026-04-01T17:42:50+00:00"
    assert edges["VT-021"].props == {
        "count": 2,
        "first_seen": "2026-04-04T11:36:34+00:00",
        "last_seen": "2026-04-08T01:46:15+00:00",
    }
    assert edges["VT-021"].evidence[0].quote == (
        "[2026.04.04 19:36:34] VT21 Fill time exceeded"
    )
    assert extraction.notes == ["1 of 5 lines could not be parsed"]


def test_lines_out_of_time_order_still_give_the_first_and_last_time(tmp_path):
    text = (
        "# Kessler KX-500 alarm history export, machine KX5-10442\n"
        "2026-05-02T00:00:00Z ALM CL-210 Coolant pressure low\n"
        "2026-05-01T00:00:00Z ALM CL-210 Coolant pressure low\n"
        "2026-05-03T00:00:00Z ALM CL-210 Coolant pressure low\n"
    )
    edge = raised(parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES))[
        "CL-210"
    ]
    assert edge.props["first_seen"] == "2026-05-01T00:00:00+00:00"
    assert edge.props["last_seen"] == "2026-05-03T00:00:00+00:00"
    assert edge.evidence[0].quote.startswith("2026-05-02")  # first in the file


def test_a_message_cut_short_when_first_seen_is_replaced_by_a_fuller_copy(tmp_path):
    text = (
        "# Kessler KX-500 alarm history export, machine KX5-10442\n"
        "2026-05-01T00:00:00Z ALM SP-104 Spindle temp\n"
        "2026-05-02T00:00:00Z ALM SP-104 Spindle temperature high\n"
        "2026-05-03T00:00:00Z ALM SP-104 Spindle t\n"
    )
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES)
    node = alarms_by_code(extraction)["SP-104"]
    assert node.props["text"] == "Spindle temperature high"
    # the evidence is still the first line where the code appears
    assert node.evidence[0].quote == "2026-05-01T00:00:00Z ALM SP-104 Spindle temp"


def test_only_consecutive_identical_lines_are_duplicates(tmp_path):
    line = "2026-05-01T00:00:00Z ALM CL-210 Coolant pressure low"
    other = "2026-05-01T00:05:00Z ALM TC-311 Tool change timeout"
    text = "# Kessler KX-500 alarm history export, machine KX5-10442\n"
    text += f"{line}\n{line}\n{line}\n{other}\n{line}\n"
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES)
    assert raised(extraction)["CL-210"].props["count"] == 2
    assert extraction.notes == ["2 duplicate lines dropped"]


def test_windows_line_endings_and_a_byte_order_mark_are_handled(tmp_path):
    text = "\ufeff" + KESSLER.replace("\n", "\r\n")
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES)
    assert nodes_of(extraction, "Asset")[0].ref.key == "CNC-01"
    assert raised(extraction)["SP-104"].props["count"] == 2


def test_a_log_with_only_heartbeats_gives_the_asset_and_nothing_else(tmp_path):
    text = "# Kessler KX-500 alarm history export, machine KX5-10442\n"
    text += "2026-03-31T16:00:00Z HB ok\n\n2026-03-31T20:00:00Z HB ok\n"
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES)
    assert count_by_type(extraction) == {"Asset": 1}
    assert extraction.edges == [] and extraction.notes == []


def test_a_serial_that_is_not_in_the_alias_table_stays_a_raw_mention(tmp_path):
    text = KESSLER.replace("KX5-10442", "ZZ9-00000")
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES)
    (asset,) = nodes_of(extraction, "Asset")
    assert (asset.ref.key, asset.canonical) == ("ZZ9-00000", False)
    assert all(e.src == asset.ref for e in extraction.edges)
    assert any("ZZ9-00000" in note for note in extraction.notes)


def test_a_tag_that_is_not_a_valid_asset_id_leaves_the_asset_a_raw_mention(tmp_path):
    aliases = AliasTable([MachineEntry(tag="machine one", serial="KX5-10442")])
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", KESSLER), aliases)
    (asset,) = nodes_of(extraction, "Asset")
    assert (asset.ref.key, asset.canonical) == ("KX5-10442", False)
    assert asset.props == {}
    assert all(e.src == asset.ref for e in extraction.edges)
    assert any(
        "machine one" in note and "not a valid asset id" in note
        for note in extraction.notes
    )


def test_a_header_from_no_known_vendor_gives_an_empty_extraction_and_a_note(tmp_path):
    text = "# Some other recorder v2\n2026-04-05T01:00:00Z ALM SP-104 Hot\n"
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", text), ALIASES)
    assert extraction.nodes == [] and extraction.edges == []
    assert "not recognised" in extraction.notes[0]
    assert (extraction.doc_type, extraction.extractor) == ("log", "log-parser")


def test_an_empty_log_gives_an_empty_extraction_and_a_note(tmp_path):
    extraction = parsers.parse_log(write_log(tmp_path, "a.log", ""), ALIASES)
    assert extraction.nodes == [] and extraction.notes


# --- The machine logs: the five real files --------------------------------------

UTC = dt.UTC
SGT = dt.timezone(dt.timedelta(hours=8))


def at(text: str, fmt: str, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.strptime(text, fmt).replace(tzinfo=tz).astimezone(UTC)


# file, asset, an event line of its own, how to read that line's time. The
# patterns are written here again on purpose: they are the check on the parser.
REAL_LOGS = {
    "CNC01_alarms_apr-sep.log": (
        "CNC-01",
        re.compile(
            r"^(?P<when>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (?:ALM|MSG) "
            r"(?P<code>[A-Z]{2}-?\d{3}) \S"
        ),
        lambda s: at(s, "%Y-%m-%dT%H:%M:%SZ", UTC),
    ),
    "cnc-02 alarm export.log": (
        "CNC-02",
        re.compile(
            r"^(?P<when>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (?:ALM|MSG) "
            r"(?P<code>[A-Z]{2}-?\d{3}) \S"
        ),
        lambda s: at(s, "%Y-%m-%dT%H:%M:%SZ", UTC),
    ),
    "HP01.log": (
        "HP-01",
        re.compile(
            r"^(?P<when>\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}) \| "
            r"(?P<code>ERR \d{3}|E-\d{3}|Err:\d{3}) \| \S"
        ),
        lambda s: at(s, "%d/%m/%Y %H:%M:%S", SGT),
    ),
    "robot_cell_events.log": (
        "RW-01",
        re.compile(r"^(?P<when>\d{9,10});(?P<code>RB-?\d{3});[^;]+;sev=\d$"),
        lambda s: dt.datetime.fromtimestamp(int(s), UTC),
    ),
    "leaktest_VT-LD20.log": (
        "LT-01",
        re.compile(
            r"^\[(?P<when>\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2})\] "
            r"(?P<code>VT-?\d{2,3}) \S"
        ),
        lambda s: at(s, "%Y.%m.%d %H:%M:%S", SGT),
    ),
}


def oracle(path: Path) -> dict[str, dict]:
    """Read a real log with the patterns above: for each code the count, the first
    and last time and the first line, dropping an exact repeat of the line before."""
    from app.ingest.normalize import normalize_alarm_code

    _, event, when_of = REAL_LOGS[path.name]
    found: dict[str, dict] = {}
    previous = None
    for line in path.read_text().splitlines():
        match = event.match(line)
        if match and line != previous:
            code = normalize_alarm_code(match["code"])
            when = when_of(match["when"])
            entry = found.setdefault(
                code, {"count": 0, "first": when, "last": when, "line": line}
            )
            entry["count"] += 1
            entry["first"], entry["last"] = (
                min(entry["first"], when),
                max(entry["last"], when),
            )
        previous = line
    return found


@pytest.fixture(scope="module", params=sorted(REAL_LOGS))
def real_log(request) -> tuple[Path, Extraction]:
    path = RAW_DIR / "logs" / request.param
    return path, parsers.parse_log(path, ALIASES)


def test_every_real_log_maps_its_header_serial_to_the_right_asset(real_log):
    path, extraction = real_log
    (asset,) = nodes_of(extraction, "Asset")
    assert asset.canonical
    assert asset.ref.key == REAL_LOGS[path.name][0]
    assert extraction.doc_path == f"logs/{path.name}"
    assert extraction.chunks == []


def test_fewer_than_five_percent_of_each_real_log_cannot_be_parsed(real_log):
    path, extraction = real_log
    lines = [line for line in path.read_text().splitlines()[1:] if line.strip()]
    note = next(
        (n for n in extraction.notes if "could not be parsed" in n), "0 of 0 lines"
    )
    bad, total = map(int, re.match(r"(\d+) of (\d+) lines", note).groups())
    assert total == len(lines)
    assert bad / total < 0.05


def test_the_counts_and_times_of_every_real_log_match_an_independent_read(real_log):
    path, extraction = real_log
    expected = oracle(path)
    assert expected, "the oracle found no events"
    edges = raised(extraction)
    assert sorted(edges) == sorted(expected)
    for code, want in expected.items():
        props = edges[code].props
        assert props["count"] == want["count"], code
        assert props["first_seen"] == want["first"].isoformat(), code
        assert props["last_seen"] == want["last"].isoformat(), code
        assert edges[code].evidence[0].quote == want["line"], code


def test_every_real_log_node_and_edge_is_well_formed(real_log):
    path, extraction = real_log
    text = path.read_text()
    from app.ontology import validate_id

    asset = nodes_of(extraction, "Asset")[0]
    for node in nodes_of(extraction, "AlarmCode"):
        assert node.canonical and validate_id("AlarmCode", node.ref.key)
        assert node.props["code"] == node.ref.key and node.props["text"]
        assert node.label == node.ref.key
        assert node.evidence[0].quote in text
    for edge in extraction.edges:
        assert edge.src == asset.ref
        assert edge.props["count"] >= 1
        assert edge.props["first_seen"] <= edge.props["last_seen"]
        assert len(edge.evidence) == 1
        assert edge.evidence[0].quote in text
        assert len(edge.evidence[0].quote) <= 300
    assert not any("HB" in node.ref.key for node in extraction.nodes)


def test_only_the_orbis_log_carries_a_severity(real_log):
    path, extraction = real_log
    for node in nodes_of(extraction, "AlarmCode"):
        if path.name == "robot_cell_events.log":
            assert node.props["severity"] in ("info", "warning", "critical")
        else:
            assert "severity" not in node.props


def test_the_real_logs_start_at_the_first_alarm_in_utc():
    first = {
        "HP01.log": ("E-200", "2026-03-31T22:45:00+00:00"),
        "CNC01_alarms_apr-sep.log": ("SP-001", "2026-03-31T22:30:00+00:00"),
        "leaktest_VT-LD20.log": ("VT-012", "2026-04-01T17:42:50+00:00"),
        "robot_cell_events.log": ("RB-551", "2026-03-31T22:03:14+00:00"),
    }
    for name, (code, when) in first.items():
        extraction = parsers.parse_log(RAW_DIR / "logs" / name, ALIASES)
        assert raised(extraction)[code].props["first_seen"] == when
