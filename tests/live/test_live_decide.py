"""Live tests for the decider. They call the real TypeSafe and OpenAI services.

They run only when the RUN_LIVE setting is 1. Each service gets one request with three
questions about a real breakdown report, then the same request again to show that it is
replayed from the cache. The tests print the answers, the response time and the ledger
rows. They write to the real spend ledger, so the spend counts against the budget. The
decision cache is a temporary file. Both requests cost well under 0.001 dollars.
"""

import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

import pytest

from app import config, ledger
from app.config import Settings, load_settings
from app.decide.base import Answer, CachedDecider, Choice, DecisionCache, Noul, Score
from app.decide.jev import JevBackend
from app.decide.openai_decisions import DecisionsBackend
from app.generator.catalogue import ASSETS

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="set RUN_LIVE=1 to call the real services",
)

STATE = "HP-01 stopped with E-217, oil under the press"
MACHINES = {asset.id: f"{asset.name}, {asset.machine_type}" for asset in ASSETS} | {
    "unknown": "The report does not name a machine"
}
LEVELS = [
    "Routine: it can wait for planned maintenance",
    "Soon: it needs attention this shift",
    "Urgent: stop and act now",
]
QUESTIONS = {
    "machine": Choice("Which machine does this report concern?", MACHINES),
    "urgency": Score("How urgent is this report?", LEVELS),
    "lockout": Noul(
        "Does the machine need a lockout before anyone works on it?",
        true="Someone must reach inside the guarded area or open a pressurised line",
        false="The machine can be inspected from outside without isolating it",
    ),
}
MODELS = {"jev": config.JEV_MODEL, "decisions": config.DECISIONS_MODEL}

RUNS: dict[str, "Run"] = {}  # service name -> what its live request returned


@dataclass(frozen=True)
class Run:
    name: str
    answers: dict[str, Answer]
    replay: dict[str, Answer]
    seconds: float
    rows: list[sqlite3.Row]  # the paid call, then the replay


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="module")
def cache_path(tmp_path_factory):
    return tmp_path_factory.mktemp("live_decisions") / "decisions.sqlite"


def say(capsys, text: str) -> None:
    """Print a line so the run shows it, even though pytest hides output."""
    with capsys.disabled():
        print(f"\n  {text}")


def newest_id() -> int:
    ledger.spent_usd()  # make sure the ledger file exists
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()[0]


def rows_since(last_id: int) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM calls WHERE id > ? ORDER BY id", (last_id,)
        ).fetchall()


def describe(row: sqlite3.Row) -> str:
    return (
        f"ledger #{row['id']} model={row['model']} purpose={row['purpose']!r} "
        f"input={row['input_tokens']} cached={row['cached_input_tokens']} "
        f"output={row['output_tokens']} usd={row['usd']:.6f} "
        f"cache_hit={row['cache_hit']}"
    )


def confidence_of(answer: Answer) -> str:
    return "n/a" if answer.confidence is None else f"{answer.confidence:.2f}"


def show(answer: Answer) -> str:
    """One answer with its probabilities, for the log."""
    match answer.kind:
        case "choice":
            top = sorted(answer.probabilities.items(), key=lambda item: -item[1])[:3]
            spread = ", ".join(f"{key} {value:.2f}" for key, value in top)
            return f"{answer.choice} (confidence {confidence_of(answer)}; {spread})"
        case "score":
            spread = ", ".join(
                f"L{level} {value:.2f}" for level, value in answer.probabilities.items()
            )
            return f"{answer.score:.2f} on 0 to 2 (confidence {confidence_of(answer)}; {spread})"
        case "noul":
            return f"p(yes) {answer.p:.3f}"
    return answer.kind


def brief(answer: Answer) -> str:
    """One answer in a few characters, for the comparison table."""
    match answer.kind:
        case "choice":
            return f"{answer.choice} ({confidence_of(answer)})"
        case "score":
            return f"{answer.score:.2f} ({confidence_of(answer)})"
        case "noul":
            return f"p {answer.p:.3f}"
    return answer.kind


def make_backend(name: str, settings: Settings):
    if name == "jev":
        if not settings.has_typesafe:
            pytest.skip("no TYPESAFE_API_KEY in .env")
        return JevBackend(settings.typesafe_api_key)
    if not settings.has_openai:
        pytest.skip("no OPENAI_API_KEY in .env")
    return DecisionsBackend(settings.openai_api_key)


def run_backend(backend, settings: Settings, cache_path) -> Run:
    """Ask the three questions once for real, then once more from the cache."""
    decider = CachedDecider(backend, DecisionCache(cache_path), settings)
    before = newest_id()
    started = time.perf_counter()
    answers = decider.ask(STATE, QUESTIONS, f"live: decide {backend.name}")
    seconds = time.perf_counter() - started
    replay = decider.ask(STATE, QUESTIONS, f"live: decide {backend.name} replay")
    return Run(backend.name, answers, replay, seconds, rows_since(before))


def check_run(run: Run) -> None:
    """What every service must return for this request."""
    assert set(run.answers) == set(QUESTIONS)

    machine = run.answers["machine"]
    assert (machine.kind, machine.backend) == ("choice", run.name)
    assert machine.choice == "HP-01"  # the report names the machine
    assert set(machine.probabilities) == set(MACHINES)
    assert sum(machine.probabilities.values()) == pytest.approx(1.0, abs=0.05)
    assert 0.0 <= machine.confidence <= 1.0

    urgency = run.answers["urgency"]
    assert (urgency.kind, urgency.backend) == ("score", run.name)
    assert 0.0 <= urgency.score <= 2.0
    assert set(urgency.probabilities) == {"0", "1", "2"}
    assert sum(urgency.probabilities.values()) == pytest.approx(1.0, abs=0.05)
    assert 0.0 <= urgency.confidence <= 1.0

    lockout = run.answers["lockout"]
    assert (lockout.kind, lockout.backend) == ("noul", run.name)
    assert 0.0 <= lockout.p <= 1.0
    assert lockout.probabilities == {"yes": lockout.p, "no": 1 - lockout.p}
    assert lockout.confidence is None

    # The second ask came from the cache, as the same answers.
    assert run.replay == run.answers
    paid, replayed = run.rows
    assert (paid["model"], paid["cache_hit"]) == (MODELS[run.name], 0)
    assert paid["usd"] > 0 and paid["input_tokens"] > 0
    assert (replayed["model"], replayed["cache_hit"], replayed["usd"]) == (
        MODELS[run.name],
        1,
        0,
    )
    assert (replayed["input_tokens"], replayed["cached_input_tokens"]) == (
        paid["input_tokens"],
        paid["cached_input_tokens"],
    )
    assert paid["usd"] < 0.01  # well under a cent


@pytest.mark.parametrize("name", ["jev", "decisions"])
def test_a_live_request_with_three_questions(name, settings, cache_path, capsys):
    run = run_backend(make_backend(name, settings), settings, cache_path)
    RUNS[name] = run

    say(capsys, f"{name} ({MODELS[name]}): {run.seconds:.2f} s for the paid request")
    for question_id, answer in run.answers.items():
        say(capsys, f"  {question_id:<8} {show(answer)}")
    for row in run.rows:
        say(capsys, f"  {describe(row)}")

    check_run(run)


def test_both_backends_side_by_side(capsys):
    if len(RUNS) < 2:
        pytest.skip("needs both the jev and the decisions run")
    names = list(RUNS)
    width = 20
    lines = [f"{'':9}" + "".join(f"{name:<{width}}" for name in names)]
    for question_id in QUESTIONS:
        cells = "".join(
            f"{brief(RUNS[n].answers[question_id]):<{width}}" for n in names
        )
        lines.append(f"{question_id:<9}{cells}")
    seconds = "".join(f"{RUNS[n].seconds:<{width}.2f}" for n in names)
    lines.append(f"{'seconds':<9}{seconds}")
    say(capsys, "\n  ".join(lines))

    assert {RUNS[n].answers["machine"].choice for n in names} == {"HP-01"}
