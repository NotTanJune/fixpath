"""Live tests for the extractors that use a model. They call the real Sol model.

They run only when the RUN_LIVE setting is 1. Three real handovers, one loose note and
the BH-250 manual are each read once. The model cache is a temporary file, so every call
is paid for. The tests write to the real spend ledger. The whole file costs well under
0.50 dollars, and the last test checks that and prints the ledger rows.
"""

import os
import sqlite3
from contextlib import closing

import pytest
from langchain_core.globals import get_llm_cache, set_llm_cache

from app import ledger, llm_cache
from app.config import RAW_DIR
from app.ingest import parsers
from app.ingest.extract_manual import extract_manual
from app.ingest.extract_text import extract_note
from app.ingest.types import Extraction

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="set RUN_LIVE=1 to call the real services",
)

BUDGET_USD = 0.50
ALIASES = parsers.build_alias_table(RAW_DIR / "inventory" / "spares_2026-10.xlsx")
HANDOVERS = RAW_DIR / "handovers"
MANUALS = RAW_DIR / "manuals"

# (file, machine, alarm, date, shift, downtime in minutes, a part used)
HANDOVER_CASES = [
    (
        HANDOVERS / "shift notes 12 Sep night.txt",
        "RW-01",
        "RB-551",
        "2026-09-12",
        "C",
        180,
        "GRIPPER-FINGER",
    ),
    (
        HANDOVERS / "Handover_20260822_B.txt",
        "CNC-02",
        "SP-104",
        "2026-08-22",
        "B",
        360,
        "BRG-7014-ACD",
    ),
    (
        HANDOVERS / "HO 05-05 night shift.txt",
        "LT-01",
        "VT-012",
        "2026-05-05",
        "C",
        120,
        None,
    ),
]
LOOSE_NOTE = RAW_DIR / "maintenance" / "note_from_arun_CNC2_spindle.txt"
BH_250 = MANUALS / "BH-250 service manual.pdf"

RESULTS: dict[str, Extraction] = {}  # file name -> what the live call returned
FIRST_ROW: list[int] = []  # the newest ledger id before the first live call


@pytest.fixture(scope="module", autouse=True)
def temporary_cache(tmp_path_factory):
    """Use a temporary model cache for the whole file."""
    previous = get_llm_cache()
    llm_cache.enable_cache(tmp_path_factory.mktemp("live_cache") / "llm_cache.sqlite")
    ledger.spent_usd()  # make sure the ledger file exists
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        FIRST_ROW.append(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()[0]
        )
    yield
    set_llm_cache(previous)


def say(capsys, text: str) -> None:
    """Print a line so the run shows it, even though pytest hides output."""
    with capsys.disabled():
        print(f"\n  {text}")


def read(path, extractor) -> Extraction:
    """The extraction of a file, made once per run."""
    if path.name not in RESULTS:
        RESULTS[path.name] = extractor(path, ALIASES)
    return RESULTS[path.name]


def ledger_rows() -> list[sqlite3.Row]:
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM calls WHERE id > ? ORDER BY id", (FIRST_ROW[0],)
        ).fetchall()


def edges_of(extraction: Extraction, rel: str) -> set[tuple[str, str]]:
    return {(e.src.key, e.dst.key) for e in extraction.edges if e.rel == rel}


# --- Handovers and the loose note ---


@pytest.mark.parametrize(
    ("path", "machine", "alarm", "date", "shift", "downtime", "part"),
    HANDOVER_CASES,
    ids=lambda value: value.name if hasattr(value, "name") else None,
)
def test_a_real_handover_gives_its_one_incident(
    capsys, path, machine, alarm, date, shift, downtime, part
):
    result = read(path, extract_note)
    assert result.doc_type == "handover"
    assert result.extractor == "sol-text"
    assert len(result.chunks) == 1

    (incident,) = [n for n in result.nodes if n.ref.type == "Incident"]
    assert incident.ref.key == f"handovers/{path.name}#1"
    assert incident.props["date"] == date  # taken from the note text, not the file name
    assert incident.props["shift"] == shift
    assert incident.props["downtime_min"] == downtime
    assert incident.props["start"] is not None
    assert incident.evidence[0].quote in path.read_text(encoding="utf-8")

    key = incident.ref.key
    assert (key, machine) in edges_of(result, "OCCURRED_ON")
    assert (key, alarm) in edges_of(result, "TRIGGERED_BY")
    if part:
        assert (key, part) in edges_of(result, "USED")

    say(capsys, f"{path.name}: {incident.props}")
    for edge in result.edges:
        say(capsys, f"  {edge.rel} {edge.dst.type} {edge.dst.key} {edge.props}")
    say(capsys, f"  notes: {result.notes}")


def test_the_loose_note_gives_its_one_incident(capsys):
    result = read(LOOSE_NOTE, extract_note)
    assert result.doc_type == "maintenance_note"
    (incident,) = [n for n in result.nodes if n.ref.type == "Incident"]
    assert incident.props["date"] == "2026-05-04"  # "On 4 May", at about 0039
    assert incident.props["shift"] == "C"
    key = incident.ref.key
    assert (key, "CNC-02") in edges_of(result, "OCCURRED_ON")
    assert (key, "SP-104") in edges_of(result, "TRIGGERED_BY")
    used = edges_of(result, "USED")
    assert (key, "BRG-7014-ACD") in used
    assert (key, "SEAL-SPDL-70") in used
    say(capsys, f"{LOOSE_NOTE.name}: {incident.props}")


# --- The BH-250 manual ---


def test_the_bh_250_manual_gives_its_alarms_procedures_and_edges(capsys):
    result = read(BH_250, extract_manual)
    assert result.doc_type == "manual"
    assert result.extractor == "sol-manual"

    def keys(node_type):
        return {n.ref.key for n in result.nodes if n.ref.type == node_type}

    # the five press alarm codes and the info code, and the three procedures
    assert keys("AlarmCode") == {"E-200", "E-217", "E-221", "E-230", "E-245", "E-252"}
    assert keys("Procedure") == {"PROC-HP-04", "PROC-HP-05", "PROC-HP-06"}
    assert keys("Asset") == {"HP-01"}
    assert len(keys("Component")) == 3
    assert len(keys("FailureMode")) == 3
    assert len(keys("Part")) == 12
    assert all(n.canonical for n in result.nodes)

    relations: dict[str, int] = {}
    for edge in result.edges:
        relations[edge.rel] = relations.get(edge.rel, 0) + 1
    assert relations == {
        "HAS_COMPONENT": 3,
        "AFFECTS": 3,
        "INDICATES": 7,
        "RESOLVED_BY": 3,
        "REQUIRES": 5,
        "FITS": 13,
    }
    assert {dst for src, dst in edges_of(result, "RESOLVED_BY")} == keys("Procedure")
    assert {src for src, _ in edges_of(result, "INDICATES")} == keys("AlarmCode") - {
        "E-200"
    }
    assert len(result.chunks) == 8
    assert result.notes == []

    say(capsys, f"{BH_250.name}: {len(result.chunks)} chunks, edges {relations}")
    for node in result.nodes:
        if node.ref.type in ("AlarmCode", "Procedure"):
            say(capsys, f"  {node.ref.type} {node.ref.key}: {node.label}")


# --- The cost ---


def test_the_whole_run_costs_less_than_the_budget(capsys):
    rows = ledger_rows()
    assert rows, "the live tests ran before this one and each paid for a call"
    total = sum(row["usd"] for row in rows)
    for row in rows:
        say(
            capsys,
            f"ledger #{row['id']} model={row['model']} purpose={row['purpose']!r} "
            f"input={row['input_tokens']} cached={row['cached_input_tokens']} "
            f"output={row['output_tokens']} usd={row['usd']:.6f} cache_hit={row['cache_hit']}",
        )
    say(capsys, f"{len(rows)} calls, total {total:.6f} dollars")
    assert total < BUDGET_USD
    assert {row["purpose"] for row in rows} == {
        "extract handover",
        "extract note",
        "extract manual",
    }
    assert all(row["cache_hit"] == 0 for row in rows)
