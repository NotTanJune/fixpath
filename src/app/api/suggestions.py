"""The suggested demo questions, and a check of which ones saved answers can cover.

A question is replayable when every step of its run is already saved. `check_replayable`
tests this on copies of the saved answers with no keys, so nothing is paid for and no
real file changes. `warm` runs every suggested question once with real keys, within a
spending cap, to save those answers.
"""

import dataclasses
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from langchain_core.globals import get_llm_cache, set_llm_cache

from app import ledger, llm_cache
from app.ask import crew
from app.config import KG_PATH, Settings, load_settings
from app.decide import base as decide_base
from app.decide.base import CacheOnlyDecider, DecisionCache
from app.ingest.report import ledger_mark, ledger_rows_since

QUESTIONS = [
    "HP-01 stopped with E-217, oil under the press",
    "CNC-02 spindle alarm SP-104, grinding noise",
    "RW-01 gripper keeps dropping parts, RB-551",
    "Leak tester failing the master part, VT-018",
    "Line 4 robot is down",
    "Is SEAL-KIT-50 in stock?",
]


@dataclass(frozen=True)
class Scratch:
    """The keyless settings and decider that a scratch block uses."""

    settings: Settings  # keyless
    decider: CacheOnlyDecider  # over the copy of the decision cache


def _copy_database(source: Path, target: Path) -> None:
    """Copy a SQLite file safely. If it is missing, the copy starts empty later."""
    if Path(source).is_file():
        with closing(
            sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True)
        ) as old:
            with closing(sqlite3.connect(target)) as new:
                old.backup(new)


@contextmanager
def scratch_state() -> Iterator[Scratch]:
    """Run code against copies of the saved answers, with no keys.

    Nothing inside the block can pay for an answer or change a real file. The original
    paths come back when the block ends. These switches apply to the whole process, so
    no question should run at the same time."""
    keyless = Settings()
    saved = (
        llm_cache.CACHE_PATH,
        ledger.LEDGER_PATH,
        decide_base.CACHE_PATH,
        get_llm_cache(),
    )
    with tempfile.TemporaryDirectory() as folder:
        scratch = Path(folder)
        _copy_database(llm_cache.CACHE_PATH, scratch / "llm_cache.sqlite")
        _copy_database(decide_base.CACHE_PATH, scratch / "decisions.sqlite")
        llm_cache.CACHE_PATH = scratch / "llm_cache.sqlite"
        decide_base.CACHE_PATH = scratch / "decisions.sqlite"
        ledger.LEDGER_PATH = scratch / "ledger.sqlite"
        try:
            yield Scratch(
                keyless, CacheOnlyDecider(DecisionCache(scratch / "decisions.sqlite"))
            )
        finally:
            llm_cache.CACHE_PATH, ledger.LEDGER_PATH, decide_base.CACHE_PATH = saved[:3]
            set_llm_cache(saved[3])


def check_replayable(
    question: str, kg_path: Path = KG_PATH, *, run: Callable = crew.ask
) -> bool:
    """Say whether saved answers alone can answer the question.

    A cache miss, a missing database or a stop returns False. Any other failure is a bug
    and is raised."""
    with scratch_state() as scratch:
        try:
            run(
                question,
                kg_path=kg_path,
                settings=scratch.settings,
                decider=scratch.decider,
            )
        except (*crew.FATAL, FileNotFoundError):
            return False
        return all(row["cache_hit"] for row in ledger_rows_since(0))


# --- make warm -----------------------------------------------------------------------

WARM_CAP_USD = 0.15


class NoKeys(RuntimeError):
    """Raised when warm has no OpenAI key. The message never contains a key."""


class WarmStopped(RuntimeError):
    """Raised when warm stops early. It keeps the question it reached and the earlier results."""

    def __init__(self, question: str, done: list["Warmed"], reason: str):
        super().__init__(f"stopped at {question!r}: {reason}")
        self.question = question
        self.done = done
        self.reason = reason


@dataclass(frozen=True)
class Warmed:
    """The outcome of one question that warm ran, with its cost."""

    question: str
    status: str
    usd: float
    paid_calls: int
    cache_hits: int


def warm(
    questions: list[str] | None = None,
    kg_path: Path = KG_PATH,
    *,
    settings: Settings | None = None,
    cap_usd: float = WARM_CAP_USD,
    run: Callable = crew.ask,
) -> list[Warmed]:
    """Run every suggested question once with real keys and save the answers.

    A server without keys can then replay them. Spending is capped above what the
    ledger already holds, so a run can pass the cap by at most one call. Raise NoKeys
    without an OpenAI key, and WarmStopped if a question cannot finish."""
    settings = settings or load_settings()
    if not settings.has_openai:
        raise NoKeys("make warm needs OPENAI_API_KEY in .env")
    start = ledger.spent_usd()
    capped = dataclasses.replace(settings, budget_usd=start + cap_usd)
    results: list[Warmed] = []
    for question in questions or QUESTIONS:
        mark = ledger_mark()
        try:
            card = run(question, kg_path=kg_path, settings=capped)
        except crew.FATAL as error:
            raise WarmStopped(question, results, str(error)) from error
        rows = ledger_rows_since(mark)
        hits = sum(int(row["cache_hit"]) for row in rows)
        results.append(
            Warmed(
                question,
                card.triage.status,
                round(sum(row["usd"] for row in rows), 6),
                len(rows) - hits,
                hits,
            )
        )
    return results
