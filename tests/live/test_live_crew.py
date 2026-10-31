"""Live test for the troubleshooting crew. It makes real, paid model calls.

It runs only when the RUN_LIVE setting is 1. Two questions go through the whole crew
against the committed graph: a real breakdown on HP-01, and a trap about a production
line the plant does not have. The model cache and the decision cache are temporary
files, and the spend is capped. A final test asks the first question again and checks
that every call is replayed from the cache for free. The tests print each answer card
and its trace.
"""

import dataclasses
import os
import sqlite3
from contextlib import closing

import pytest

from app import ledger, llm, llm_cache
from app.ask import crew
from app.ask.crew import Models, ask
from app.config import load_settings
from app.decide.base import CachedDecider, DecisionCache
from app.decide.jev import JevBackend
from app.decide.openai_decisions import DecisionsBackend

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="set RUN_LIVE=1 to call the real services",
)

CAP_USD = 0.10
QUESTION = "HP-01 stopped with E-217, oil under the press"
TRAP = "Line 4 robot is down"
PURPOSES = {"triage", "graph agent", "manual agent", "synthesize", "verify"}

STATE: dict = {}  # what one test hands to the next
START: list[int] = []  # the newest ledger id before the first live call


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


@pytest.fixture(scope="module", autouse=True)
def temporary_caches(tmp_path_factory):
    """Temporary caches for the whole file, a spending cap, and the models built to match."""
    settings = load_settings()
    if not settings.has_openai:
        pytest.skip("OPENAI_API_KEY is not set")
    if settings.has_typesafe:
        backend = JevBackend(settings.typesafe_api_key)
    elif settings.has_openai:
        backend = DecisionsBackend(settings.openai_api_key)
    folder = tmp_path_factory.mktemp("live_crew")
    patch = pytest.MonkeyPatch()
    # ask() switches the model cache on at its default path, so point that default
    # at the temporary file for the run
    patch.setattr(llm_cache, "CACHE_PATH", folder / "llm_cache.sqlite")
    START.append(newest_id())
    settings = dataclasses.replace(settings, budget_usd=ledger.spent_usd() + CAP_USD)
    STATE["settings"] = settings
    STATE["decider"] = CachedDecider(
        backend, DecisionCache(folder / "decisions.sqlite"), settings
    )
    STATE["models"] = Models(
        graph=llm.chat_model("terra", "low", settings=settings),
        manual=llm.chat_model("terra", "low", settings=settings),
        synth=llm.chat_model("sol", "low", settings=settings),
    )
    yield
    patch.undo()


def say(capsys, text: str = "") -> None:
    """Print a line so the run shows it, even though pytest hides output."""
    with capsys.disabled():
        print(f"  {text}")


def run(question: str):
    return ask(
        question,
        kg_path=crew.KG_PATH,
        settings=STATE["settings"],
        decider=STATE["decider"],
        models=STATE["models"],
    )


def show(capsys, card, booked) -> None:
    for row in booked:
        say(capsys, describe(row))
    say(capsys, "node  start  seconds  backend  cache_hit  cost")
    for t in card.trace:
        say(
            capsys,
            f"trace {t.name:13} {t.started_at.time().isoformat()} {t.seconds:8.3f}s "
            f"{t.backend} cache_hit={t.cache_hit} ${t.cost_usd:.6f}",
        )
    say(capsys, "card:")
    with capsys.disabled():
        print(card.model_dump_json(indent=2, exclude={"trace"}))


def test_the_press_question_gets_a_full_card(capsys):
    before = newest_id()
    card = run(QUESTION)
    booked = rows_since(before)
    STATE["card"], STATE["booked"] = card, booked
    show(capsys, card, booked)

    assert card.triage.status in ("act", "confirm")
    assert (card.triage.asset_id, card.triage.alarm_code) == ("HP-01", "E-217")
    assert card.causes[0].failure_mode_id == "FM-MAIN-CYLINDER-SEAL-WEAR"
    assert card.fix and card.fix.procedure_id == "PROC-HP-04" and card.fix.steps
    assert card.safety.required and "accumulator" in card.safety.text
    assert {p.part_no: p.bin for p in card.parts}["SEAL-KIT-50"] == "B-12"
    assert 1 <= len(card.breakdowns) <= 3
    assert card.summary and card.claims
    assert [t.name for t in card.trace][0] == "triage"
    assert [t.name for t in card.trace][-1] == "assemble"
    assert {row["purpose"] for row in booked} <= PURPOSES
    assert sum(t.cost_usd for t in card.trace) == pytest.approx(
        sum(r["usd"] for r in booked)
    )
    assert all(row["cache_hit"] == 0 for row in booked)


def test_the_trap_question_about_an_unknown_line_clarifies(capsys):
    before = newest_id()
    card = run(TRAP)
    booked = rows_since(before)
    STATE["trap"] = card
    show(capsys, card, booked)

    assert {row["purpose"] for row in booked} <= PURPOSES
    # the plant has no Line 4, so the crew stops at triage whatever the decider picked
    assert card.triage.status == "clarify" and card.triage.asset_id is None
    assert card.triage.chips and "no Line 4" in card.triage.reason
    assert [t.name for t in card.trace] == ["triage", "assemble"]
    assert {row["purpose"] for row in booked} <= {"triage"}
    assert card.fix is None and card.causes == [] and card.summary is None


def test_a_second_run_replays_every_call_for_free(capsys):
    spent = ledger.spent_usd()
    before = newest_id()
    card = run(QUESTION)
    booked = rows_since(before)
    for row in booked:
        say(capsys, describe(row))
    assert booked and all(row["cache_hit"] == 1 and row["usd"] == 0 for row in booked)
    assert ledger.spent_usd() == spent
    first = STATE["card"]
    assert card.model_dump(exclude={"trace"}) == first.model_dump(exclude={"trace"})


def test_the_whole_file_stayed_under_the_cap(capsys):
    paid = sum(row["usd"] for row in rows_since(START[0]))
    say(capsys, f"paid ${paid:.6f} of the ${CAP_USD:.2f} cap")
    assert paid <= CAP_USD
