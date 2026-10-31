"""Live test for the graph agent and the manual agent. It makes real, paid model calls.

It runs only when the RUN_LIVE setting is 1. Both agents answer one real question about
HP-01 and the E-217 alarm, using the committed graph and the local embedding model. The
spend is capped at 0.05 dollars above the starting spend, and the model cache is a
temporary file. A final test runs both agents again and checks that every call is
replayed from the cache for free. The tests print the tool calls, the facts and the
passages.
"""

import dataclasses
import os
import sqlite3
from contextlib import closing

import pytest
from langchain_core.globals import get_llm_cache, set_llm_cache

from app import ledger, llm, llm_cache
from app.ask.graph_agent import graph_agent
from app.ask.manual_agent import manual_agent
from app.ask.tools import connect_ro
from app.ask.types import Hints
from app.config import load_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="set RUN_LIVE=1 to call the real services",
)

CAP_USD = 0.05
QUESTION = "HP-01 stopped with E-217, oil under the press"
HINTS = Hints(asset_id="HP-01", alarm_code="E-217", intent="troubleshoot")

STATE: dict = {}  # what one test hands to the next
START: list[int] = []  # the newest ledger id before the first live call


@pytest.fixture(scope="module", autouse=True)
def temporary_cache(tmp_path_factory):
    """A temporary model cache for the whole file, and a spending cap."""
    previous = get_llm_cache()
    llm_cache.enable_cache(
        tmp_path_factory.mktemp("live_ask_cache") / "llm_cache.sqlite"
    )
    START.append(newest_id())
    STATE["settings"] = dataclasses.replace(
        load_settings(), budget_usd=ledger.spent_usd() + CAP_USD
    )
    yield
    set_llm_cache(previous)


@pytest.fixture
def conn():
    connection = connect_ro()
    yield connection
    connection.close()


def say(capsys, text: str = "") -> None:
    """Print a line so the run shows it, even though pytest hides output."""
    with capsys.disabled():
        print(f"  {text}")


def newest_id() -> int:
    ledger.spent_usd()  # make sure the ledger file exists
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as db:
        return db.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()[0]


def rows_since(last_id: int) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT * FROM calls WHERE id > ? ORDER BY id", (last_id,)
        ).fetchall()


def describe(row: sqlite3.Row) -> str:
    return (
        f"ledger #{row['id']} model={row['model']} purpose={row['purpose']!r} "
        f"input={row['input_tokens']} cached={row['cached_input_tokens']} "
        f"output={row['output_tokens']} usd={row['usd']:.6f} cache_hit={row['cache_hit']}"
    )


def terra():
    return llm.chat_model("terra", "low", settings=STATE["settings"])


def run_graph_agent(conn):
    return graph_agent(QUESTION, HINTS, conn, model=terra())


def run_manual_agent(conn):
    return manual_agent(QUESTION, HINTS, conn, model=terra())


def test_the_graph_agent_runs_a_tool_loop(conn, capsys):
    before = newest_id()
    facts = run_graph_agent(conn)
    booked = rows_since(before)
    STATE["facts"] = facts
    for row in booked:
        say(capsys, describe(row))
    say(capsys, "tool calls:")
    for call in facts.tool_calls:
        say(
            capsys,
            f"  {call.filled_by:5} {call.name} {call.args} -> {call.result_count}",
        )
    for cause in facts.causes:
        say(
            capsys,
            f"cause {cause.failure_mode_id} {cause.likelihood} support {cause.support}/{cause.of_total}",
        )
    for fix in facts.fixes:
        say(capsys, f"fix {fix.procedure_id} {fix.title} loto={fix.requires_loto}")
    for part in facts.parts:
        say(
            capsys,
            f"part {part.part_no} qty {part.qty} on hand {part.qty_on_hand} bin {part.bin} enough={part.enough}",
        )
    for b in facts.breakdowns:
        say(
            capsys,
            f"breakdown {b.incident_id} {b.date} {b.technician} {b.resolution_text}",
        )
    say(capsys, f"notes: {facts.notes}")

    assert len(booked) >= 2  # at least one tool round and the answer to it
    assert {row["purpose"] for row in booked} == {"graph agent"}
    assert all(row["cache_hit"] == 0 and row["usd"] > 0 for row in booked)
    assert any(call.filled_by == "agent" for call in facts.tool_calls)
    assert facts.causes and facts.fixes and facts.parts and facts.breakdowns
    assert (facts.asset_id, facts.alarm_code) == ("HP-01", "E-217")


def test_the_manual_agent_returns_passages_with_pages(conn, capsys):
    before = newest_id()
    result = run_manual_agent(conn)
    booked = rows_since(before)
    STATE["manual"] = result
    for row in booked:
        say(capsys, describe(row))
    say(capsys, f"queries: {result.queries}")
    for passage in result.passages:
        say(
            capsys,
            f"passage {passage.chunk_id} page {passage.page} {passage.matched_by} "
            f"score {passage.score:.4f} | {passage.text[:80]!r}",
        )

    assert [row["purpose"] for row in booked] == ["manual agent"]
    assert booked[0]["cache_hit"] == 0
    assert 1 <= len(result.passages) <= 5
    assert all(p.page is not None for p in result.passages)
    assert len({p.doc_path for p in result.passages}) == 1  # only the press manual


def test_a_second_run_replays_every_call_for_free(conn, capsys):
    spent = ledger.spent_usd()
    before = newest_id()
    facts, manual = run_graph_agent(conn), run_manual_agent(conn)
    booked = rows_since(before)
    for row in booked:
        say(capsys, describe(row))
    assert booked and all(row["cache_hit"] == 1 and row["usd"] == 0 for row in booked)
    assert ledger.spent_usd() == spent
    assert facts.model_dump() == STATE["facts"].model_dump()
    assert manual.model_dump() == STATE["manual"].model_dump()


def test_the_whole_file_stayed_under_the_cap(capsys):
    paid = sum(row["usd"] for row in rows_since(START[0]))
    say(capsys, f"paid ${paid:.6f} of the ${CAP_USD:.2f} cap")
    assert paid <= CAP_USD
