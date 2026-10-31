"""The shared parts of the decision service: question types, answer types and the cache.

A backend sends one request to a decision service and returns the answers with the
token usage. Two wrappers add what every request needs. CachedDecider checks the cache
first, then the budget, then calls the backend, then stores and books the result.
CacheOnlyDecider replays stored answers when there is no API key.

A reply the provider answered was billed even if it cannot be used, so it is always
booked in the spend ledger, with an estimated size if the usage is missing.
"""

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from app.config import CACHE_DIR, DECISIONS_MODEL, JEV_MODEL, Settings, load_settings
from app.ledger import PRICES, check_budget, record

logger = logging.getLogger(__name__)

CACHE_PATH = CACHE_DIR / "decisions.sqlite"

JEV_BACKEND = "jev"
DECISIONS_BACKEND = "decisions"
# The backends a replay tries, in this order.
REPLAY_ORDER = (
    (JEV_BACKEND, JEV_MODEL),
    (DECISIONS_BACKEND, DECISIONS_MODEL),
)

MIN_CHOICES, MAX_CHOICES = 2, 255
MIN_LEVELS, MAX_LEVELS = 2, 10

# A billed reply with no usable usage is booked as an estimate. Text averages about 4
# characters per token, so 3 overestimates, which errs on the safe side of the budget.
CHARS_PER_TOKEN = 3
USAGE_ESTIMATED = " [usage estimated]"

BODY_LIMIT = 300
# The OpenAI API echoes a masked copy of a bad key in its error reply, so anything
# shaped like a key is removed from error text too. Ordinary words such as
# "risk-assessment" are left whole.
_KEY_SHAPE = re.compile(r"(?<![\w-])sk-[\w*-]+")


class DecisionError(RuntimeError):
    """A backend request failed, or its reply could not be used."""


class NoDecisionBackend(RuntimeError):
    """A request has no live backend to go to and no cached answer to replay."""


def excerpt(body: str, api_key: str) -> str:
    """Up to 300 characters of an error body, with the API key removed first."""
    if api_key:
        body = body.replace(api_key, "[key]")
    return _KEY_SHAPE.sub("[key]", body)[:BODY_LIMIT]


# --- questions and answers ----------------------------------------------------


@dataclass(frozen=True)
class Choice:
    """Pick one option. options maps each option key to its description."""

    instructions: str
    options: dict[str, str]

    def __post_init__(self) -> None:
        if not MIN_CHOICES <= len(self.options) <= MAX_CHOICES:
            raise ValueError(
                f"a choice needs between {MIN_CHOICES} and {MAX_CHOICES} options; "
                f"got {len(self.options)}"
            )


@dataclass(frozen=True)
class Score:
    """Rate against ordered levels, listed from low to high."""

    instructions: str
    levels: list[str]

    def __post_init__(self) -> None:
        if not MIN_LEVELS <= len(self.levels) <= MAX_LEVELS:
            raise ValueError(
                f"a score needs between {MIN_LEVELS} and {MAX_LEVELS} levels; "
                f"got {len(self.levels)}"
            )


@dataclass(frozen=True)
class Noul:
    """A yes or no question. true and false describe each answer, and are given together
    or not at all."""

    instructions: str
    true: str | None = None
    false: str | None = None

    def __post_init__(self) -> None:
        if (self.true is None) != (self.false is None):
            raise ValueError(
                "a noul takes both true and false or neither; got only one of them"
            )


Question = Choice | Score | Noul


@dataclass(frozen=True)
class Answer:
    """One answer, in the same shape from either backend.

    kind says what sort of answer it is. For a yes/no question, p is the probability of
    yes. probabilities holds the chance of each option. A refusal holds nothing else.
    """

    kind: Literal["choice", "score", "noul", "refusal"]
    choice: str | None = None
    score: float | None = None
    p: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    backend: str = ""


@dataclass(frozen=True)
class Usage:
    """The token counts of one real request. A count of 0 input tokens means the usage
    report is broken, so it is refused and an estimate is booked instead. estimated
    marks counts that were made up here rather than reported."""

    input_tokens: int
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    def __post_init__(self) -> None:
        counts = (self.input_tokens, self.cached_input_tokens, self.output_tokens)
        whole = all(
            isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for count in counts
        )
        if (
            not whole
            or self.input_tokens < 1
            or self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError(
                "token counts must be whole numbers, none negative, with at least "
                "1 input token and no more cached tokens than input tokens; got "
                f"input {self.input_tokens!r}, cached {self.cached_input_tokens!r}, "
                f"output {self.output_tokens!r}"
            )


def estimate_usage(request: dict[str, Any]) -> Usage:
    """An estimated usage for a billed reply that reported none. It is based on the
    size of the request. Neither decider model bills output."""
    characters = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
    return Usage(-(-characters // CHARS_PER_TOKEN), estimated=True)


class UnusableReply(DecisionError):
    """The provider answered and billed the request, but the reply cannot be used.
    usage is what to book for it."""

    def __init__(self, message: str, usage: Usage):
        super().__init__(message)
        self.usage = usage


def describe(question: Question) -> dict[str, Any]:
    """The question as plain data, the same for every backend."""
    match question:
        case Choice():
            return {
                "type": "choice",
                "instructions": question.instructions,
                "options": question.options,
            }
        case Score():
            return {
                "type": "score",
                "instructions": question.instructions,
                "levels": question.levels,
            }
        case Noul():
            return {
                "type": "noul",
                "instructions": question.instructions,
                "true": question.true,
                "false": question.false,
            }
    raise TypeError(f"not a question: {question!r}")


# --- the interfaces -----------------------------------------------------------


class Decider(Protocol):
    """What the rest of the app talks to. purpose labels the spend record."""

    name: str
    model: str

    def ask(
        self, state: str | dict, questions: dict[str, Question], purpose: str
    ) -> dict[str, Answer]: ...


class Backend(Protocol):
    """One service that answers questions. decide sends a single request.

    It raises UnusableReply for a billed reply that cannot be used, and DecisionError
    for a failed request that was not billed. Neither carries the original error,
    because that can quote the key."""

    name: str
    model: str

    def decide(
        self, state: str | dict, questions: dict[str, Question]
    ) -> tuple[dict[str, Answer], Usage]: ...


# --- the decision cache -------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions(
    key TEXT PRIMARY KEY,
    answers TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class CacheEntry:
    answers: dict[str, Answer]
    usage: Usage


class DecisionCache:
    """Answers kept in one file, so a request made once is answered from disk after.

    An unreadable entry counts as a miss. A storage problem never stops a save, because
    the request is already paid for. Both log a warning.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else CACHE_PATH

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute(_SCHEMA)
        return conn

    @staticmethod
    def key(
        backend: str, model: str, state: str | dict, questions: dict[str, Question]
    ) -> str:
        request = {
            "backend": backend,
            "model": model,
            "state": state,
            "questions": {qid: describe(q) for qid, q in questions.items()},
        }
        text = json.dumps(request, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, key: str) -> CacheEntry | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT answers, input_tokens, cached_input_tokens, output_tokens"
                " FROM decisions WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            answers = {qid: Answer(**item) for qid, item in json.loads(row[0]).items()}
            return CacheEntry(answers, Usage(*row[1:]))
        except (AttributeError, TypeError, ValueError):
            logger.warning("ignoring an unreadable decision cache entry", exc_info=True)
            return None

    def put(self, key: str, answers: dict[str, Answer], usage: Usage) -> None:
        text = json.dumps(
            {qid: asdict(answer) for qid, answer in answers.items()},
            separators=(",", ":"),
        )
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO decisions(key, answers, input_tokens,"
                    " cached_input_tokens, output_tokens, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        text,
                        usage.input_tokens,
                        usage.cached_input_tokens,
                        usage.output_tokens,
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    ),
                )
        except (sqlite3.Error, OSError):
            logger.warning("could not store a decision in the cache", exc_info=True)


# --- the wrappers -------------------------------------------------------------


def _book(model: str, purpose: str, usage: Usage, cache_hit: bool) -> None:
    if usage.estimated:
        logger.warning(
            "no usable token usage came back for a %s call (purpose: %s); "
            "booking an estimate",
            model,
            purpose,
        )
        purpose += USAGE_ESTIMATED
    record(
        model,
        purpose,
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.output_tokens,
        cache_hit,
    )


class CachedDecider:
    """A backend behind the cache, the budget stop and the spend record.

    The backend's model must have a known price, checked when this is made. A billed
    reply is always booked, even one that cannot be used. Nothing is cached for it.
    """

    def __init__(
        self,
        backend: Backend,
        cache: DecisionCache,
        settings: Settings | None = None,
    ):
        if backend.model not in PRICES:
            raise KeyError(f"no price for model {backend.model!r}")
        self.backend = backend
        self.cache = cache
        self.settings = settings
        self.name = backend.name
        self.model = backend.model

    def ask(
        self, state: str | dict, questions: dict[str, Question], purpose: str
    ) -> dict[str, Answer]:
        key = DecisionCache.key(self.name, self.model, state, questions)
        entry = self.cache.get(key)
        if entry is not None:
            _book(self.model, purpose, entry.usage, cache_hit=True)
            return entry.answers
        check_budget(self.settings if self.settings is not None else load_settings())
        try:
            answers, usage = self.backend.decide(state, questions)
        except UnusableReply as error:
            # The provider billed this request, so book it before the error goes on.
            # Otherwise the budget stop would never see the spend.
            _book(self.model, purpose, error.usage, cache_hit=False)
            raise
        self.cache.put(key, answers, usage)
        _book(self.model, purpose, usage, cache_hit=False)
        return answers


class CacheOnlyDecider:
    """Replay mode for a machine with no API key.

    It looks for a stored answer and books it at no cost. With none stored, it raises
    NoDecisionBackend.
    """

    name = "cache"
    model = "cache"

    def __init__(self, cache: DecisionCache):
        self.cache = cache

    def ask(
        self, state: str | dict, questions: dict[str, Question], purpose: str
    ) -> dict[str, Answer]:
        for backend, model in REPLAY_ORDER:
            entry = self.cache.get(DecisionCache.key(backend, model, state, questions))
            if entry is not None:
                _book(model, purpose, entry.usage, cache_hit=True)
                return entry.answers
        raise NoDecisionBackend("no API key and no cached answer")
