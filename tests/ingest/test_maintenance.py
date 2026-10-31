"""Tests for the maintenance CSV parser. They use small hand-written exports, the
fallback for unfamiliar column headers, and the real export, which is checked against
a separate reading of the file. Only the header fallback test uses a fake model; any
other model call fails the test.
"""

import csv
import datetime as dt
import io
import re
from pathlib import Path

import pytest
from ingest_stubs import (
    ALIASES,
    EXPORT,
    UNKNOWN_MAINTENANCE,
    count_by_relation,
    count_by_type,
    fake_model,  # noqa: F401, F811  (a fixture)
    maintenance_answer,
    no_model,  # noqa: F401  (an autouse fixture)
    nodes_of,
    refuse,
)

from app import llm
from app.ingest import parsers
from app.ingest.types import CandidateEdge, CandidateNode, Extraction


# --- The maintenance CSV: small files -------------------------------------------

CSV_HEADER = "Date,Mach.,Problem desc,Action taken,Parts,Tech,Downtime (min),Ref"


def csv_row(*cells: str) -> str:
    """One CSV line, quoted the way the export quotes it."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(cells)
    return buffer.getvalue()


CSV_ROWS = [
    csv_row(
        "2026-04-14",
        "PR-2",
        "press losing pressure mid-stroke, oil on the floor",
        "Repl main cylinder seal kit",
        "HLP46 x1; SK-50 x1",
        "NA",
        "",
        "WO-1142",
    ),
    csv_row(
        "17/04/2026",
        "Kessler 1",
        "ALM CL-210: coolant pressure dropping, parts coming out hot",
        "Clean coolant filter & flush lines",
        "FLT CL 25 x1",
        "Chen Jun Hao",
        "175",
        "WO-1801",
    ),
    csv_row(
        "2026-04-22",
        "HP-01",
        "Err:245: press losing pressure mid-stroke, oil on the floor",
        "Repl main cylinder seal kit",
        "OILHLP4620L x1; SEAL KIT 50 x1",
        "Arun Prakash",
        "455",
        "WO-3033",
    ),
    csv_row(
        "27-Apr-26",
        "RC-1",
        "RB-560: robot arm jerky on axis 2, drive alarm",
        "Inspect axis 2 & reset servo drive",
        "brg 6205 x1; FUSE SRV 10 x1",
        "Rajesh Pillai",
        "approx 275",
        "WO-4209",
    ),
]


def write_csv(tmp_path: Path, rows: list[str], header: str = CSV_HEADER) -> Path:
    path = tmp_path / "maint.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def incidents(extraction: Extraction) -> dict[str, CandidateNode]:
    return {node.ref.key: node for node in nodes_of(extraction, "Incident")}


def edges_of(extraction: Extraction, ref: str, rel: str) -> list[CandidateEdge]:
    return [e for e in extraction.edges if e.src.key == ref and e.rel == rel]


def test_each_csv_row_becomes_one_non_canonical_incident_draft(tmp_path):
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, CSV_ROWS), ALIASES)
    assert extraction.doc_path.endswith("/maint.csv")
    assert (extraction.doc_type, extraction.extractor) == (
        "maintenance_note",
        "csv-parser",
    )
    assert extraction.chunks == [] and extraction.notes == []
    drafts = incidents(extraction)
    assert list(drafts) == ["WO-1142", "WO-1801", "WO-3033", "WO-4209"]
    assert count_by_type(extraction) == {"Incident": 4}
    assert not any(node.canonical for node in drafts.values())

    first = drafts["WO-1142"]
    assert first.label == "press losing pressure mid-stroke, oil on the floor"
    assert first.props == {
        "date": "2026-04-14",
        "shift": None,
        "downtime_min": None,
        "symptom_text": "press losing pressure mid-stroke, oil on the floor",
        "technician": "NA",
        "resolution_text": "Repl main cylinder seal kit",
    }
    (evidence,) = first.evidence
    assert evidence.quote == CSV_ROWS[0]
    assert (evidence.extractor, evidence.page) == ("csv-parser", None)
    assert evidence.doc_path == extraction.doc_path
    assert 0 < evidence.confidence <= 1


def test_the_date_cell_reads_all_three_formats_as_the_singapore_date(tmp_path):
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, CSV_ROWS), ALIASES)
    dates = {k: n.props["date"] for k, n in incidents(extraction).items()}
    assert dates == {
        "WO-1142": "2026-04-14",
        "WO-1801": "2026-04-17",
        "WO-3033": "2026-04-22",
        "WO-4209": "2026-04-27",
    }


def test_downtime_reads_an_integer_approx_n_or_nothing(tmp_path):
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, CSV_ROWS), ALIASES)
    downtime = {k: n.props["downtime_min"] for k, n in incidents(extraction).items()}
    assert downtime == {
        "WO-1142": None,
        "WO-1801": 175,
        "WO-3033": 455,
        "WO-4209": 275,
    }


def test_the_machine_resolves_through_the_alias_table(tmp_path):
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, CSV_ROWS), ALIASES)
    where = {
        ref: edges_of(extraction, ref, "OCCURRED_ON")[0].dst
        for ref in incidents(extraction)
    }
    assert {ref: (d.type, d.key) for ref, d in where.items()} == {
        "WO-1142": ("Asset", "HP-01"),
        "WO-1801": ("Asset", "CNC-01"),
        "WO-3033": ("Asset", "HP-01"),
        "WO-4209": ("Asset", "RW-01"),
    }


def test_an_alarm_prefix_becomes_a_triggered_by_edge_and_leaves_the_symptom(tmp_path):
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, CSV_ROWS), ALIASES)
    drafts = incidents(extraction)
    triggered = {
        ref: [e.dst.key for e in edges_of(extraction, ref, "TRIGGERED_BY")]
        for ref in drafts
    }
    assert triggered == {
        "WO-1142": [],
        "WO-1801": ["CL-210"],
        "WO-3033": ["E-245"],
        "WO-4209": ["RB-560"],
    }
    assert drafts["WO-1801"].props["symptom_text"] == (
        "coolant pressure dropping, parts coming out hot"
    )
    assert drafts["WO-3033"].props["symptom_text"] == (
        "press losing pressure mid-stroke, oil on the floor"
    )
    edge = edges_of(extraction, "WO-1801", "TRIGGERED_BY")[0]
    assert edge.dst.type == "AlarmCode" and edge.props == {}
    assert edge.evidence[0].quote == CSV_ROWS[1]


def test_parts_become_used_edges_with_dashed_keys_when_possible(tmp_path):
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, CSV_ROWS), ALIASES)

    def used(ref):
        return [
            (e.dst.type, e.dst.key, e.props["qty"])
            for e in edges_of(extraction, ref, "USED")
        ]

    assert used("WO-1142") == [("Part", "HLP46", 1), ("Part", "SK-50", 1)]
    assert used("WO-1801") == [("Part", "FLT-CL-25", 1)]
    assert used("WO-3033") == [("Part", "OILHLP4620L", 1), ("Part", "SEAL-KIT-50", 1)]
    assert used("WO-4209") == [("Part", "BRG-6205", 1), ("Part", "FUSE-SRV-10", 1)]
    assert all(
        e.evidence[0].quote in CSV_ROWS
        for e in extraction.edges
        if e.rel in ("USED", "OCCURRED_ON")
    )


def test_quantities_other_than_one_are_kept(tmp_path):
    row = "2026-05-01,CNC-01,tool fault,Repl,BRG 7014 ACD x2; HLP46 x3,AP,60,WO-1"
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    quantities = [e.props["qty"] for e in extraction.edges if e.rel == "USED"]
    assert quantities == [2, 3]


def test_rows_with_the_same_ref_stay_separate_drafts(tmp_path):
    rows = [
        "26/04/2026,VT station,fill takes too long,Repl fixture seals,SEALFIX30 x2,SL,200,WO-3841",
        "2026-04-26,VT station,fill takes too long,Repl fixture seals,SEALFIX30 x2,SL,220,WO-3841",
    ]
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, rows), ALIASES)
    drafts = nodes_of(extraction, "Incident")
    assert [d.ref.key for d in drafts] == ["WO-3841", "WO-3841"]
    assert [d.props["downtime_min"] for d in drafts] == [200, 220]
    assert [d.props["date"] for d in drafts] == ["2026-04-26", "2026-04-26"]
    assert len(extraction.edges) == 4  # one OCCURRED_ON and one USED each


def test_text_before_the_colon_that_is_not_an_alarm_code_stays_in_the_symptom(
    tmp_path,
):
    row = "2026-05-01,CNC-01,noise: loud bang from the spindle,Repl,,AP,60,WO-2"
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    draft = incidents(extraction)["WO-2"]
    assert draft.props["symptom_text"] == "noise: loud bang from the spindle"
    assert edges_of(extraction, "WO-2", "TRIGGERED_BY") == []


@pytest.mark.parametrize(
    ("problem", "code", "symptom"),
    [
        (
            "Err:252 relief valve pressure out of range",
            "E-252",
            "relief valve pressure out of range",
        ),
        (
            "VT018 leak rate above limit on master part",
            "VT-018",
            "leak rate above limit on master part",
        ),
        ("RB-560 axis 2 overcurrent", "RB-560", "axis 2 overcurrent"),
        ("E221 pressure spikes", "E-221", "pressure spikes"),
        ("ALM CL-210 coolant low", "CL-210", "coolant low"),
        ("ERR 230 pressure hunting", "E-230", "pressure hunting"),
    ],
)
def test_an_alarm_code_that_starts_the_text_with_no_colon_is_still_the_alarm(
    tmp_path, problem, code, symptom
):
    row = f'2026-05-01,CNC-01,"{problem}",Repl,,AP,60,WO-7'
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    draft = incidents(extraction)["WO-7"]
    assert [e.dst.key for e in edges_of(extraction, "WO-7", "TRIGGERED_BY")] == [code]
    assert draft.props["symptom_text"] == symptom


@pytest.mark.parametrize(
    "problem",
    [
        "CNC-01 stopped during the cut",
        "Err 3 times then it stopped",
        "VT18 leak",  # two digits and no colon is not clearly a code
        "press 2 leaking",
        "coolant low, ERR252 seen",
        "SEAL-KIT-50 leaking",
        "M30 program end missing",
    ],
)
def test_text_that_only_looks_a_little_like_a_code_is_not_an_alarm(tmp_path, problem):
    row = f'2026-05-01,CNC-01,"{problem}",Repl,,AP,60,WO-8'
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    assert edges_of(extraction, "WO-8", "TRIGGERED_BY") == []
    assert incidents(extraction)["WO-8"].props["symptom_text"] == problem


def test_a_machine_that_the_table_does_not_know_stays_a_raw_mention(tmp_path):
    row = "2026-05-01,mystery rig,stopped,Reset,,AP,60,WO-3"
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    (edge,) = edges_of(extraction, "WO-3", "OCCURRED_ON")
    assert (edge.dst.type, edge.dst.key) == ("Asset", "mystery rig")
    assert any("1 machine" in note and "alias" in note for note in extraction.notes)


def test_a_row_with_no_machine_has_no_occurred_on_edge_and_is_noted(tmp_path):
    row = "2026-05-01,,stopped,Reset,,AP,60,WO-4"
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    assert "WO-4" in incidents(extraction)
    assert edges_of(extraction, "WO-4", "OCCURRED_ON") == []
    assert any("no machine" in note for note in extraction.notes)


def test_a_date_that_cannot_be_read_leaves_the_date_empty_and_is_noted(tmp_path):
    row = "yesterday,CNC-01,stopped,Reset,,AP,60,WO-5"
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    assert incidents(extraction)["WO-5"].props["date"] is None
    assert any("1 row" in note and "date" in note for note in extraction.notes)


def test_a_row_with_no_ref_is_keyed_by_its_place_in_the_file(tmp_path):
    row = "2026-05-01,CNC-01,stopped,Reset,,AP,60,"
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row, row]), ALIASES)
    keys = [d.ref.key for d in nodes_of(extraction, "Incident")]
    assert keys == [f"{extraction.doc_path}#1", f"{extraction.doc_path}#2"]
    assert any("no work order" in note for note in extraction.notes)


def test_a_part_without_a_quantity_counts_as_one_and_is_noted(tmp_path):
    row = "2026-05-01,CNC-01,stopped,Reset,SK-50; HLP46 x2,AP,60,WO-6"
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, [row]), ALIASES)
    used = edges_of(extraction, "WO-6", "USED")
    assert [(e.dst.key, e.props["qty"]) for e in used] == [("SK-50", 1), ("HLP46", 2)]
    assert any("1 part" in note and "quantity" in note for note in extraction.notes)


def test_blank_cells_give_none_and_short_rows_are_padded(tmp_path):
    rows = ["2026-05-01,CNC-01,stopped,,,,,WO-7", "", "2026-05-02,CNC-01,stopped"]
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, rows), ALIASES)
    drafts = nodes_of(extraction, "Incident")
    assert len(drafts) == 2  # the blank line is not a row
    assert drafts[0].props["technician"] is None
    assert drafts[0].props["resolution_text"] is None
    assert drafts[0].props["downtime_min"] is None
    assert drafts[1].ref.key.endswith("#2")


def test_quoted_commas_a_byte_order_mark_and_windows_line_endings(tmp_path):
    text = "\ufeff" + "\r\n".join([CSV_HEADER, CSV_ROWS[0], CSV_ROWS[1]]) + "\r\n"
    path = tmp_path / "maint.csv"
    path.write_bytes(text.encode("utf-8"))
    extraction = parsers.parse_maintenance_csv(path, ALIASES)
    drafts = incidents(extraction)
    assert drafts["WO-1142"].props["symptom_text"].count(",") == 1
    assert drafts["WO-1142"].evidence[0].quote == CSV_ROWS[0]


def test_a_quoted_field_that_runs_over_two_lines_stays_one_incident(tmp_path):
    rows = [
        '2026-05-01,CNC-01,"stopped\nwith a bang",Reset,,AP,60,WO-12',
        "2026-05-02,CNC-01,x,Reset,,AP,60,WO-13",
    ]
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, rows), ALIASES)
    drafts = incidents(extraction)
    assert list(drafts) == ["WO-12", "WO-13"]
    assert drafts["WO-12"].props["symptom_text"] == "stopped\nwith a bang"
    assert drafts["WO-12"].evidence[0].quote == rows[0]
    assert drafts["WO-13"].evidence[0].quote == rows[1]


def test_headers_are_matched_whatever_their_case_or_order(tmp_path):
    header = "REF,tech,downtime (MIN),parts,Action Taken,problem DESC,mach.,DATE"
    row = "WO-8,AP,45,SK-50 x1,Reset,stopped,CNC-01,2026-05-01"
    extraction = parsers.parse_maintenance_csv(
        write_csv(tmp_path, [row], header), ALIASES
    )
    draft = incidents(extraction)["WO-8"]
    assert draft.props["date"] == "2026-05-01"
    assert draft.props["downtime_min"] == 45
    assert draft.props["technician"] == "AP"
    assert edges_of(extraction, "WO-8", "OCCURRED_ON")[0].dst.key == "CNC-01"


def test_a_semicolon_separated_export_parses_too(tmp_path):
    header = CSV_HEADER.replace(",", ";")
    row = '2026-05-01;CNC-01;stopped;Reset;"SK-50 x1; HLP46 x2";AP;60;WO-9'
    extraction = parsers.parse_maintenance_csv(
        write_csv(tmp_path, [row], header), ALIASES
    )
    used = edges_of(extraction, "WO-9", "USED")
    assert [(e.dst.key, e.props["qty"]) for e in used] == [("SK-50", 1), ("HLP46", 2)]


def test_a_file_with_no_rows_gives_an_empty_extraction_and_a_note(tmp_path):
    extraction = parsers.parse_maintenance_csv(write_csv(tmp_path, []), ALIASES)
    assert extraction.nodes == [] and extraction.edges == []
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    nothing = parsers.parse_maintenance_csv(empty, ALIASES)
    assert nothing.nodes == [] and nothing.notes


def test_an_unknown_export_layout_calls_the_model_once_and_still_parses(
    tmp_path, fake_model
):
    fake_model.answer = maintenance_answer()
    row = '2026-05-01,CNC-01,"ALM SP-104: noisy",Reset,SK-50 x1,AP,45,WO-10'
    path = write_csv(
        tmp_path, [row, row.replace("WO-10", "WO-11")], ",".join(UNKNOWN_MAINTENANCE)
    )
    extraction = parsers.parse_maintenance_csv(path, ALIASES)
    assert len(fake_model.calls) == 1
    assert fake_model.calls[0]["purpose"] == "map headers"
    assert list(incidents(extraction)) == ["WO-10", "WO-11"]
    draft = incidents(extraction)["WO-10"]
    assert draft.props["date"] == "2026-05-01"
    assert draft.props["downtime_min"] == 45
    assert edges_of(extraction, "WO-10", "TRIGGERED_BY")[0].dst.key == "SP-104"
    assert extraction.notes == []


def test_the_real_export_never_reaches_the_model():
    assert llm.structured is refuse
    assert (
        len(nodes_of(parsers.parse_maintenance_csv(EXPORT, ALIASES), "Incident")) > 50
    )


# --- The maintenance CSV: the real export ---------------------------------------


def export_rows() -> list[tuple[dict, str]]:
    """Each record of the real export as a dict, with its source line."""
    lines = EXPORT.read_text().splitlines()
    records = list(csv.DictReader(lines))
    return list(zip(records, lines[1:], strict=True))


PREFIX = re.compile(r"^((?:ALM |ERR |Err:)\d{3}|(?:ALM )?[A-Z]{1,3}-?\d{2,3}): ")


def expected_date(cell: str) -> str:
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(cell, fmt).date().isoformat()
        except ValueError:
            continue
    raise AssertionError(f"unexpected date {cell!r}")


@pytest.fixture(scope="module")
def export_extraction() -> Extraction:
    return parsers.parse_maintenance_csv(EXPORT, ALIASES)


def test_the_export_yields_one_draft_per_row_with_normalized_dates(export_extraction):
    rows = export_rows()
    drafts = nodes_of(export_extraction, "Incident")
    assert len(drafts) == len(rows) == 57
    assert export_extraction.doc_path == "maintenance/maint_log_export_2026.csv"
    for node, (record, line) in zip(drafts, rows, strict=True):
        assert node.ref.key == record["Ref"]
        assert not node.canonical
        assert node.props["date"] == expected_date(record["Date"])
        assert node.props["shift"] is None
        assert node.evidence[0].quote == line


def test_four_work_orders_appear_twice_and_stay_separate(export_extraction):
    refs = [n.ref.key for n in nodes_of(export_extraction, "Incident")]
    repeated = {ref for ref in refs if refs.count(ref) == 2}
    assert len(repeated) == 4
    assert max(refs.count(ref) for ref in refs) == 2


def test_the_export_values_match_an_independent_read(export_extraction):
    drafts = nodes_of(export_extraction, "Incident")
    for node, (record, _) in zip(drafts, export_rows(), strict=True):
        cell = record["Downtime (min)"].strip()
        digits = re.sub(r"\D", "", cell)
        assert node.props["downtime_min"] == (int(digits) if digits else None)
        assert node.props["technician"] == (record["Tech"] or None)
        assert node.props["resolution_text"] == (record["Action taken"] or None)
        problem = record["Problem desc"]
        prefix = PREFIX.match(problem)
        symptom = problem[prefix.end() :] if prefix else problem
        assert node.props["symptom_text"] == symptom


def test_the_export_edges_match_an_independent_read(export_extraction):
    from app.ingest.normalize import dashed_part, normalize_alarm_code

    drafts = nodes_of(export_extraction, "Incident")
    for node, (record, _) in zip(drafts, export_rows(), strict=True):
        key = node.ref.key
        # the machine
        machine = [
            e
            for e in edges_of(export_extraction, key, "OCCURRED_ON")
            if e.evidence[0].quote == node.evidence[0].quote
        ]
        assert len(machine) == 1
        assert machine[0].dst.key == ALIASES.asset_for(record["Mach."])
        assert machine[0].dst.key in ALIASES.tags
        # the alarm prefix
        prefix = PREFIX.match(record["Problem desc"])
        alarms = [
            e
            for e in edges_of(export_extraction, key, "TRIGGERED_BY")
            if e.evidence[0].quote == node.evidence[0].quote
        ]
        if prefix:
            assert [e.dst.key for e in alarms] == [
                normalize_alarm_code(prefix.group(1))
            ]
        else:
            assert alarms == []
        # the parts
        wanted = []
        for item in filter(None, (p.strip() for p in record["Parts"].split(";"))):
            name, qty = re.fullmatch(r"(.+?) x(\d+)", item).groups()
            wanted.append((dashed_part(name) or name, int(qty)))
        used = [
            (e.dst.key, e.props["qty"])
            for e in edges_of(export_extraction, key, "USED")
            if e.evidence[0].quote == node.evidence[0].quote
        ]
        assert used == wanted


def test_the_export_has_no_warnings_and_only_incident_nodes(export_extraction):
    assert export_extraction.notes == []
    assert count_by_type(export_extraction) == {"Incident": 57}
    assert set(count_by_relation(export_extraction)) == {
        "OCCURRED_ON",
        "TRIGGERED_BY",
        "USED",
    }
    assert count_by_relation(export_extraction)["OCCURRED_ON"] == 57


def test_every_export_quote_is_a_line_of_the_file(export_extraction):
    lines = set(EXPORT.read_text().splitlines())
    for node in export_extraction.nodes:
        assert node.evidence[0].quote in lines
    for edge in export_extraction.edges:
        assert edge.evidence[0].quote in lines
        assert len(edge.evidence[0].quote) <= 300
