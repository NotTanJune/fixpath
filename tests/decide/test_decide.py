"""Tests for the decider, which asks a model to pick answers to a set of questions. They
cover the decision cache, the cost ledger and budget, the two backends (JEV and OpenAI
Decisions) and the function that chooses between them.

No test uses the network. The backends talk to fakes, and the cache and ledger are
temporary files."""

import dataclasses
import hashlib
import json
import logging
import socket
import sqlite3
import traceback
from contextlib import closing

import httpx
import httpx2
import openai
import pytest
from openai.types.decision import Decision

from app import config, ledger
from app.config import Settings
from app.decide import base, jev, openai_decisions
from app.decide.base import (
    Answer,
    CachedDecider,
    CacheOnlyDecider,
    Choice,
    DecisionCache,
    DecisionError,
    NoDecisionBackend,
    Noul,
    Score,
    Usage,
)
from app.decide.jev import JevBackend
from app.decide.openai_decisions import DecisionsBackend
from app.ledger import BudgetExceeded

DEFAULT_CACHE_PATH = base.CACHE_PATH  # read before any fixture redirects it

JEV, LUNA = config.JEV_MODEL, config.DECISIONS_MODEL
SETTINGS = Settings(budget_usd=40.0)
# A $0.01 budget. spend_the_budget() uses it up.
TIGHT = Settings(budget_usd=0.01)

STATE = "HP-01 stopped with E-217, oil under the press"
QUESTIONS = {
    "machine": Choice(
        "Which machine is this about?",
        {
            "HP-01": "Hydraulic press",
            "CNC-01": "Machining centre 1",
            "unknown": "No machine is named",
        },
    ),
    "urgency": Score("How urgent is it?", ["Can wait", "This shift", "Right now"]),
    "lockout": Noul(
        "Does the repair need a lockout?",
        true="Someone must work inside the guarded area",
        false="The machine can be inspected from outside",
    ),
}
ANSWERS = {
    "machine": Answer(
        "choice",
        choice="HP-01",
        probabilities={"HP-01": 0.95, "CNC-01": 0.03, "unknown": 0.02},
        confidence=0.9,
        backend="jev",
    ),
    "urgency": Answer(
        "score",
        score=1.7,
        probabilities={"0": 0.1, "1": 0.1, "2": 0.8},
        confidence=0.55,
        backend="jev",
    ),
    "lockout": Answer(
        "noul", p=0.98, probabilities={"yes": 0.98, "no": 1 - 0.98}, backend="jev"
    ),
}
USAGE = Usage(input_tokens=400, output_tokens=30)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep the developer's ledger, decision cache and network out of every test."""
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")
    monkeypatch.setattr(base, "CACHE_PATH", tmp_path / "decisions.sqlite")
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("a default test must not use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class FakeBackend:
    """A backend that never calls out. It counts its calls and returns canned
    answers, or raises the error it was given."""

    def __init__(self, name="jev", model=JEV, answers=ANSWERS, usage=USAGE, error=None):
        self.name, self.model = name, model
        self.answers, self.usage, self.error = answers, usage, error
        self.calls: list[tuple] = []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        if self.error is not None:
            raise self.error
        return self.answers, self.usage


def ledger_rows() -> list[dict]:
    if not ledger.LEDGER_PATH.exists():  # nothing has been recorded or checked yet
        return []
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY id")]


def spend_the_budget() -> None:
    """Put $0.042 in the ledger, which is more than the TIGHT budget."""
    ledger.record(JEV, "earlier", 1_000_000, 0, 0, False)


def base_warnings(caplog) -> list[str]:
    """The warnings that the decision wrappers logged, as text."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.decide.base" and record.levelno >= logging.WARNING
    ]


def estimated_tokens(request) -> int:
    """The token estimate for a request that went out: its length in characters divided
    by 3, rounded up. It is worked out from the request the fake transport saw, not from
    the code under test."""
    return -(-len(request.content.decode("utf-8")) // 3)


def cached(backend=None, settings=SETTINGS, path=None) -> CachedDecider:
    return CachedDecider(backend or FakeBackend(), DecisionCache(path), settings)


# --- the question types -----------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 256])
def test_a_choice_needs_between_2_and_255_options(count):
    options = {f"option-{i}": f"the option {i}" for i in range(count)}
    with pytest.raises(ValueError, match=rf"between 2 and 255 options; got {count}"):
        Choice("Which one?", options)


@pytest.mark.parametrize("count", [2, 3, 255])
def test_a_choice_accepts_2_to_255_options(count):
    options = {f"option-{i}": f"the option {i}" for i in range(count)}
    assert Choice("Which one?", options).options == options


@pytest.mark.parametrize("count", [0, 1, 11])
def test_a_score_needs_between_2_and_10_levels(count):
    levels = [f"level {i}" for i in range(count)]
    with pytest.raises(ValueError, match=rf"between 2 and 10 levels; got {count}"):
        Score("How bad?", levels)


@pytest.mark.parametrize("count", [2, 3, 10])
def test_a_score_accepts_2_to_10_levels(count):
    levels = [f"level {i}" for i in range(count)]
    assert Score("How bad?", levels).levels == levels


def test_a_noul_takes_both_descriptions_or_neither():
    assert Noul("Is it?").true is None and Noul("Is it?").false is None
    both = Noul("Is it?", true="It is.", false="It is not.")
    assert (both.true, both.false) == ("It is.", "It is not.")
    for kwargs in ({"true": "It is."}, {"false": "It is not."}):
        with pytest.raises(ValueError, match="both true and false or neither"):
            Noul("Is it?", **kwargs)


def test_the_questions_are_frozen():
    for question in (
        Choice("Which?", {"a": "x", "b": "y"}),
        Score("How?", ["low", "high"]),
        Noul("Is it?"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            question.instructions = "changed"


# --- the answer type --------------------------------------------------------


def test_an_answer_is_frozen_and_defaults_to_nothing_but_its_kind():
    answer = Answer("refusal")
    assert (answer.choice, answer.score, answer.p, answer.confidence) == (
        None,
        None,
        None,
        None,
    )
    assert (answer.probabilities, answer.backend) == ({}, "")
    with pytest.raises(dataclasses.FrozenInstanceError):
        answer.kind = "choice"


def test_usage_rejects_counts_that_would_lower_the_spend():
    assert Usage(10, 4, 2) == Usage(
        input_tokens=10, cached_input_tokens=4, output_tokens=2
    )
    for args in [(0,), (-1,), (10, -1), (10, 0, -1), (10, 11), (10.5,), ("10",)]:
        with pytest.raises(ValueError, match="token counts"):
            Usage(*args)


def test_a_usage_is_a_real_count_unless_it_is_marked_as_an_estimate():
    assert Usage(10).estimated is False
    assert Usage(10, 4, 2) == Usage(10, 4, 2, estimated=False)
    assert Usage(10, estimated=True).estimated is True
    assert Usage(10, estimated=True) != Usage(10)


@pytest.mark.parametrize(
    ("value_length", "tokens"),
    [(1, 3), (2, 4), (3, 4), (4, 4), (5, 5), (100, 36)],
)
def test_the_estimate_is_the_request_json_characters_over_three_rounded_up(
    value_length, tokens
):
    request = {"k": "v" * value_length}
    # The JSON is {"k":"vvv"}: 8 characters of frame around the value.
    assert len(json.dumps(request, separators=(",", ":"))) == 8 + value_length

    assert base.estimate_usage(request) == Usage(tokens, 0, 0, estimated=True)


def test_the_estimate_counts_characters_not_json_escapes_or_bytes():
    # The value is one accented letter, e with an acute accent, written as a Python
    # escape to keep this file ASCII. The JSON is 9 characters, so 3 tokens. As a
    # \u00e9 JSON escape it would be 14 characters, and as utf-8 bytes 10: both more.
    assert base.estimate_usage({"k": "\u00e9"}).input_tokens == 3


def test_the_estimate_rules_are_the_ones_app_llm_uses():
    from app import llm

    assert base.CHARS_PER_TOKEN == llm.CHARS_PER_TOKEN == 3
    assert base.USAGE_ESTIMATED == llm.USAGE_ESTIMATED == " [usage estimated]"


def test_an_unusable_reply_is_a_decision_error_that_carries_the_usage_it_cost():
    usage = Usage(500, 0, 5)
    error = base.UnusableReply("the reply made no sense", usage)
    assert isinstance(error, DecisionError)
    assert str(error) == "the reply made no sense"
    assert error.usage is usage


# --- the error text scrubber ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "risk-assessment failed",
        "disk-full on node",
        "task-queue is busy",
        "ask-me-later and mask-wearing",
    ],
)
def test_ordinary_words_that_end_in_sk_are_not_mistaken_for_a_key(text):
    assert base.excerpt(text, "unrelated-key") == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sk-abc123", "[key]"),
        ("sk-proj-****WXYZ", "[key]"),
        ("bad sk-abc123 and sk-proj-****WXYZ.", "bad [key] and [key]."),
        ("key=sk-abc123", "key=[key]"),
        ("(sk-abc123)", "([key])"),
        ('"sk-abc123"', '"[key]"'),
        (
            '{"message":"Incorrect API key provided: '
            'sk-proj-********************QQQQ."}',
            '{"message":"Incorrect API key provided: [key]."}',
        ),
    ],
)
def test_anything_shaped_like_an_openai_key_is_still_removed(text, expected):
    assert base.excerpt(text, "unrelated-key") == expected


def test_the_exact_key_is_removed_wherever_it_is_and_whatever_its_shape():
    assert base.excerpt("bad key ts-live-ZZZZ9999 sent", "ts-live-ZZZZ9999") == (
        "bad key [key] sent"
    )


def test_an_empty_key_leaves_the_body_alone_instead_of_replacing_nothing():
    # str.replace("", "[key]") would put [key] between every pair of characters.
    assert base.excerpt("plain body", "") == "plain body"
    assert base.excerpt("a" * 400, "") == "a" * 300
    assert base.excerpt("bad sk-abc123", "") == "bad [key]"


# --- the decision cache -----------------------------------------------------


def expected_key(backend, model, state, questions):
    """The cache key as documented: a hash of the request written in a fixed form."""
    text = json.dumps(
        {"backend": backend, "model": model, "state": state, "questions": questions},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_cache_is_data_cache_decisions_sqlite_apart_from_the_ledger():
    assert DEFAULT_CACHE_PATH == config.CACHE_DIR / "decisions.sqlite"
    assert DEFAULT_CACHE_PATH != ledger.LEDGER_PATH
    assert DecisionCache().path == base.CACHE_PATH  # the module value, read on use


def test_the_key_is_the_sha256_of_canonical_json_of_the_whole_request():
    questions = {
        "machine": {
            "type": "choice",
            "instructions": "Which machine is this about?",
            "options": {
                "HP-01": "Hydraulic press",
                "CNC-01": "Machining centre 1",
                "unknown": "No machine is named",
            },
        },
        "urgency": {
            "type": "score",
            "instructions": "How urgent is it?",
            "levels": ["Can wait", "This shift", "Right now"],
        },
        "lockout": {
            "type": "noul",
            "instructions": "Does the repair need a lockout?",
            "true": "Someone must work inside the guarded area",
            "false": "The machine can be inspected from outside",
        },
    }
    assert DecisionCache.key("jev", JEV, STATE, QUESTIONS) == expected_key(
        "jev", JEV, STATE, questions
    )
    state = {"text": STATE, "turns": ["earlier", "later"]}
    assert DecisionCache.key("decisions", LUNA, state, QUESTIONS) == expected_key(
        "decisions", LUNA, state, questions
    )


def test_the_key_changes_with_every_part_of_the_request():
    keys = {
        DecisionCache.key("jev", JEV, STATE, QUESTIONS),
        DecisionCache.key("decisions", JEV, STATE, QUESTIONS),
        DecisionCache.key("jev", LUNA, STATE, QUESTIONS),
        DecisionCache.key("jev", JEV, STATE + "!", QUESTIONS),
        DecisionCache.key("jev", JEV, {"text": STATE}, QUESTIONS),
        DecisionCache.key("jev", JEV, STATE, {"other": QUESTIONS["machine"]}),
        DecisionCache.key("jev", JEV, STATE, {"machine": Noul("Which machine?")}),
        DecisionCache.key("jev", JEV, STATE, {"machine": Score("Which?", ["a", "b"])}),
        DecisionCache.key("jev", JEV, STATE, {"machine": Score("Which?", ["b", "a"])}),
        DecisionCache.key("jev", JEV, STATE, {"q": Noul("Is it?")}),
        DecisionCache.key("jev", JEV, STATE, {"q": Noul("Is it?", "yes", "no")}),
        DecisionCache.key("jev", JEV, STATE, {"q": Noul("Is it?", "no", "yes")}),
    }
    assert len(keys) == 12


def test_the_key_ignores_the_order_of_questions_and_options():
    options = {"a": "first", "b": "second"}
    forward = {"x": Choice("Pick", options), "y": Noul("Is it?")}
    backward = {
        "y": Noul("Is it?"),
        "x": Choice("Pick", dict(reversed(options.items()))),
    }
    assert DecisionCache.key("jev", JEV, STATE, forward) == DecisionCache.key(
        "jev", JEV, STATE, backward
    )


def test_an_entry_comes_back_as_it_went_in_with_every_kind_of_answer(tmp_path):
    answers = {
        **ANSWERS,
        "other": Answer("refusal", backend="decisions"),
    }
    usage = Usage(input_tokens=500, cached_input_tokens=128, output_tokens=40)
    path = tmp_path / "elsewhere" / "deeper" / "decisions.sqlite"  # not made yet
    DecisionCache(path).put("k1", answers, usage)

    entry = DecisionCache(path).get("k1")  # a new object, so it reads the file

    assert entry.answers == answers
    assert entry.usage == usage
    assert DecisionCache(path).get("missing") is None


def test_putting_a_key_again_replaces_the_entry():
    cache = DecisionCache()
    cache.put("k", {"a": Answer("refusal")}, Usage(5))
    cache.put("k", ANSWERS, USAGE)
    assert cache.get("k").answers == ANSWERS and cache.get("k").usage == USAGE


def test_an_unreadable_entry_is_a_miss_and_is_logged(caplog):
    cache = DecisionCache()
    cache.put("k", ANSWERS, USAGE)
    with closing(sqlite3.connect(cache.path)) as conn, conn:
        conn.execute("UPDATE decisions SET answers = 'not json' WHERE key = 'k'")
    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        assert cache.get("k") is None
    assert "unreadable decision cache entry" in caplog.text

    with closing(sqlite3.connect(cache.path)) as conn, conn:
        conn.execute('UPDATE decisions SET answers = \'{"q": {"nope": 1}}\'')
    assert cache.get("k") is None  # fields that Answer does not have


def test_a_cache_that_cannot_be_written_logs_and_does_not_raise(tmp_path, caplog):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")
    cache = DecisionCache(blocker / "decisions.sqlite")  # no folder can be made here
    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        cache.put("k", ANSWERS, USAGE)
    assert "could not store a decision" in caplog.text


# --- CachedDecider ----------------------------------------------------------


def test_a_miss_calls_the_backend_stores_the_answers_and_books_the_call():
    backend = FakeBackend()
    decider = cached(backend)

    answers = decider.ask(STATE, QUESTIONS, "unit test")

    assert answers == ANSWERS
    assert backend.calls == [(STATE, QUESTIONS)]
    key = DecisionCache.key("jev", JEV, STATE, QUESTIONS)
    assert decider.cache.get(key).answers == ANSWERS
    (row,) = ledger_rows()
    assert (row["model"], row["purpose"]) == (JEV, "unit test")
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        400,
        0,
        30,
    )
    assert row["cache_hit"] == 0
    assert row["usd"] == ledger.cost(JEV, 400, 0, 30) > 0


def test_a_hit_returns_the_stored_answers_without_a_request_and_books_a_free_line():
    backend = FakeBackend()
    decider = cached(backend)
    decider.ask(STATE, QUESTIONS, "first")

    again = decider.ask(STATE, QUESTIONS, "second")

    assert again == ANSWERS
    assert len(backend.calls) == 1  # no second request
    first, second = ledger_rows()
    assert (second["purpose"], second["model"]) == ("second", JEV)
    assert (second["cache_hit"], second["usd"]) == (1, 0)
    # The replayed line keeps the token counts of the real call.
    assert (second["input_tokens"], second["output_tokens"]) == (400, 30)
    assert ledger.spent_usd() == first["usd"]


def test_a_second_decider_on_the_same_file_replays_the_first_ones_answers(tmp_path):
    path = tmp_path / "shared.sqlite"
    cached(FakeBackend(), path=path).ask(STATE, QUESTIONS, "writer")
    never_called = FakeBackend(error=AssertionError("the backend must not be called"))

    answers = cached(never_called, path=path).ask(STATE, QUESTIONS, "reader")

    assert answers == ANSWERS and never_called.calls == []


def test_a_different_state_or_question_is_a_miss():
    backend = FakeBackend()
    decider = cached(backend)
    decider.ask(STATE, QUESTIONS, "p")
    decider.ask(STATE + " again", QUESTIONS, "p")
    decider.ask(STATE, {"lockout": QUESTIONS["lockout"]}, "p")
    assert len(backend.calls) == 3


def test_backends_with_other_names_or_models_do_not_share_entries(tmp_path):
    path = tmp_path / "shared.sqlite"
    cached(FakeBackend(), path=path).ask(STATE, QUESTIONS, "p")
    other = FakeBackend(name="decisions", model=LUNA)

    cached(other, path=path).ask(STATE, QUESTIONS, "p")

    assert len(other.calls) == 1
    assert [row["model"] for row in ledger_rows()] == [JEV, LUNA]


def test_the_budget_stops_a_request_before_the_backend_is_called():
    spend_the_budget()
    backend = FakeBackend()
    decider = cached(backend, settings=TIGHT)

    with pytest.raises(BudgetExceeded):
        decider.ask(STATE, QUESTIONS, "unit test")

    assert backend.calls == []
    assert decider.cache.get(DecisionCache.key("jev", JEV, STATE, QUESTIONS)) is None
    assert len(ledger_rows()) == 1  # only the earlier spend, no new line


def test_a_hit_is_served_even_when_the_budget_is_spent():
    decider = cached(FakeBackend(), settings=TIGHT)
    decider.ask(STATE, QUESTIONS, "first")  # nothing spent yet, so this goes out
    spend_the_budget()

    assert decider.ask(STATE, QUESTIONS, "replay") == ANSWERS  # no check on a hit

    with pytest.raises(BudgetExceeded):  # a new request still stops
        decider.ask(STATE + " new", QUESTIONS, "new")


def test_the_budget_comes_from_the_settings_loaded_on_use_when_none_is_given(
    monkeypatch,
):
    monkeypatch.setattr(base, "load_settings", lambda: TIGHT)
    decider = CachedDecider(FakeBackend(), DecisionCache())  # no settings passed
    decider.ask(STATE, QUESTIONS, "fits the budget")  # nothing spent yet
    spend_the_budget()
    with pytest.raises(BudgetExceeded):
        decider.ask(STATE + " new", QUESTIONS, "over the budget")


def test_a_failed_request_stores_nothing_and_books_nothing():
    backend = FakeBackend(error=DecisionError("the backend said no"))
    decider = cached(backend)

    with pytest.raises(DecisionError, match="the backend said no"):
        decider.ask(STATE, QUESTIONS, "unit test")

    assert decider.cache.get(DecisionCache.key("jev", JEV, STATE, QUESTIONS)) is None
    assert ledger_rows() == []


def test_an_unusable_reply_is_booked_before_it_is_raised_again_and_stores_nothing(
    caplog,
):
    error = base.UnusableReply("the reply made no sense", Usage(500, 0, 5))
    decider = cached(FakeBackend(error=error))

    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        with pytest.raises(base.UnusableReply) as raised:
            decider.ask(STATE, QUESTIONS, "unit test")

    assert raised.value is error and error.__context__ is None
    (row,) = ledger_rows()
    assert row["purpose"] == "unit test"  # the usage is real, so no estimate mark
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        500,
        0,
        5,
    )
    assert row["cache_hit"] == 0
    assert row["usd"] == ledger.cost(JEV, 500, 0, 5) > 0
    assert base_warnings(caplog) == []
    assert decider.cache.get(DecisionCache.key("jev", JEV, STATE, QUESTIONS)) is None


def test_an_unusable_reply_with_an_estimate_is_booked_marked_with_one_warning(caplog):
    error = base.UnusableReply(
        "the reply made no sense", Usage(input_tokens=700, estimated=True)
    )
    decider = cached(FakeBackend(error=error))

    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        with pytest.raises(base.UnusableReply):
            decider.ask(STATE, QUESTIONS, "unit test")

    (row,) = ledger_rows()
    assert row["purpose"] == "unit test [usage estimated]"
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        700,
        0,
        0,
    )
    assert row["usd"] == ledger.cost(JEV, 700, 0, 0) > 0
    (warning,) = base_warnings(caplog)
    assert JEV in warning and "unit test" in warning


def test_a_good_reply_with_an_estimate_is_booked_marked_stored_and_replayed_free(
    caplog,
):
    backend = FakeBackend(usage=Usage(input_tokens=700, estimated=True))
    decider = cached(backend)

    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        first = decider.ask(STATE, QUESTIONS, "unit test")
        second = decider.ask(STATE, QUESTIONS, "unit test")

    assert first == second == ANSWERS
    assert len(backend.calls) == 1  # the answers were stored
    paid, replayed = ledger_rows()
    assert paid["purpose"] == "unit test [usage estimated]"
    assert paid["input_tokens"] == 700 and paid["usd"] == ledger.cost(JEV, 700, 0, 0)
    assert (replayed["cache_hit"], replayed["usd"], replayed["input_tokens"]) == (
        1,
        0,
        700,
    )
    (warning,) = base_warnings(caplog)  # for the paid call; the replay warns of nothing
    assert JEV in warning and "unit test" in warning


def test_a_paid_call_is_booked_and_returned_even_if_the_cache_cannot_store_it(
    monkeypatch, caplog
):
    decider = cached()
    decider.cache.get("x")  # makes the file and its table
    connect = decider.cache._connect

    def read_only():
        # SQLite's own read-only mode: reads work and a write raises "attempt to write
        # a readonly database", whoever runs the test. File permissions would not do,
        # because root ignores them.
        conn = connect()
        conn.execute("PRAGMA query_only = ON")
        return conn

    monkeypatch.setattr(decider.cache, "_connect", read_only)

    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        assert decider.ask(STATE, QUESTIONS, "unit test") == ANSWERS

    assert "could not store a decision" in caplog.text
    assert "readonly database" in caplog.text  # the write failed in SQLite itself
    (row,) = ledger_rows()
    assert row["cache_hit"] == 0 and row["usd"] > 0


def test_the_decider_reports_the_name_and_model_of_its_backend():
    decider = cached(FakeBackend(name="decisions", model=LUNA))
    assert (decider.name, decider.model) == ("decisions", LUNA)


def test_a_backend_with_no_price_is_refused_before_any_request():
    backend = FakeBackend(model="jev-latest")
    with pytest.raises(KeyError, match="no price for model 'jev-latest'"):
        CachedDecider(backend, DecisionCache(), SETTINGS)
    assert backend.calls == []


# --- CacheOnlyDecider -------------------------------------------------------


def store(backend: str, model: str, answers=ANSWERS, usage=USAGE) -> DecisionCache:
    cache = DecisionCache()
    cache.put(DecisionCache.key(backend, model, STATE, QUESTIONS), answers, usage)
    return cache


def test_a_replay_serves_an_answer_stored_under_the_jev_key():
    decider = CacheOnlyDecider(store("jev", JEV))
    assert decider.ask(STATE, QUESTIONS, "replay") == ANSWERS
    (row,) = ledger_rows()
    assert (row["model"], row["purpose"], row["cache_hit"], row["usd"]) == (
        JEV,
        "replay",
        1,
        0,
    )
    assert (row["input_tokens"], row["output_tokens"]) == (400, 30)


def test_a_replay_serves_an_answer_stored_under_the_decisions_key():
    answers = {"lockout": Answer("noul", p=0.9, backend="decisions")}
    decider = CacheOnlyDecider(store("decisions", LUNA, answers, Usage(300, 100, 8)))

    assert decider.ask(STATE, QUESTIONS, "replay") == answers

    (row,) = ledger_rows()
    assert (row["model"], row["cache_hit"], row["usd"]) == (LUNA, 1, 0)
    assert (row["input_tokens"], row["cached_input_tokens"]) == (300, 100)


def test_a_replay_prefers_the_jev_key_when_both_are_stored():
    cache = store("decisions", LUNA, {"lockout": Answer("noul", p=0.1)})
    cache.put(DecisionCache.key("jev", JEV, STATE, QUESTIONS), ANSWERS, USAGE)
    assert CacheOnlyDecider(cache).ask(STATE, QUESTIONS, "replay") == ANSWERS


def test_a_replay_miss_raises_with_the_documented_message_and_books_nothing():
    decider = CacheOnlyDecider(store("jev", JEV))
    with pytest.raises(NoDecisionBackend) as raised:
        decider.ask(STATE + " other", QUESTIONS, "replay")
    assert str(raised.value) == "no API key and no cached answer"
    assert ledger_rows() == []


def test_a_replay_serves_what_a_cached_decider_stored(tmp_path):
    path = tmp_path / "shared.sqlite"
    cached(FakeBackend(), path=path).ask(STATE, QUESTIONS, "live")

    replayed = CacheOnlyDecider(DecisionCache(path)).ask(STATE, QUESTIONS, "replay")

    assert replayed == ANSWERS
    assert [row["cache_hit"] for row in ledger_rows()] == [0, 1]


def test_the_replay_decider_names_itself_cache():
    decider = CacheOnlyDecider(DecisionCache())
    assert (decider.name, decider.model) == ("cache", "cache")


# --- the JEV backend --------------------------------------------------------

KEY = "ts-test-key-not-real-0123456789"
# A made-up key in the shape of an OpenAI key, and the masked copy of it that the
# OpenAI API echoes in a 401 reply. Neither is real. Both backends take any key
# string, so the chain tests of the two backends share them.
CHAIN_KEY = "sk-proj-abcdEFGH1234567890QQQQ"
MASKED_KEY = "sk-proj-********************QQQQ"


def assert_key_free(error: BaseException) -> None:
    """The error and every error chained to it hold no key, not even a masked one.

    A traceback prints the whole chain, so the check reads the printed text."""
    text = "".join(traceback.format_exception(error))
    assert CHAIN_KEY not in text
    assert MASKED_KEY not in text
    assert "QQQQ" not in text  # the tail that both the key and its masked copy end in
    assert error.__cause__ is None
    assert error.__context__ is None


JEV_QUESTIONS = {
    "machine": {
        "type": "choice",
        "instructions": "Which machine is this about?",
        "criteria": {
            "HP-01": "Hydraulic press",
            "CNC-01": "Machining centre 1",
            "unknown": "No machine is named",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is it?",
        "criteria": ["Can wait", "This shift", "Right now"],
    },
    "lockout": {
        "type": "noul",
        "instructions": "Does the repair need a lockout?",
        "criteria": {
            "true": "Someone must work inside the guarded area",
            "false": "The machine can be inspected from outside",
        },
    },
}
# Replies in the shape of the API reference at docs.typesafe.ai/api/system-one.
JEV_ANSWERS = {
    "machine": {
        "type": "choice",
        "choice": "HP-01",
        "confidence": 0.9,
        "probabilities": {"CNC-01": 0.03, "HP-01": 0.95, "unknown": 0.02},
    },
    "urgency": {
        "type": "score",
        "score": 1.7,
        "confidence": 0.55,
        "legend": {"0": "Can wait", "1": "This shift", "2": "Right now"},
        "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
    },
    "lockout": {"type": "noul", "noul": 0.98},
}
JEV_USAGE = {"input_tokens": 400, "output_tokens": 30}


def jev_ok(answers=None, usage=JEV_USAGE):
    """A reply maker for a successful JEV answer."""
    body = {"model": JEV, "answers": JEV_ANSWERS if answers is None else answers}
    if usage is not None:
        body["usage"] = usage
    return lambda: httpx.Response(200, json=body)


def jev_status(status, text="", headers=None):
    return lambda: httpx.Response(status, text=text, headers=headers)


def mock_http(*replies):
    """An httpx client that answers each request with the next reply and records the
    requests. The last reply repeats."""
    seen: list[httpx.Request] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return reply(request) if reply.__code__.co_argcount else reply()

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


@pytest.fixture
def sleeps(monkeypatch):
    """The delays the JEV backend waits between attempts. Nothing really sleeps."""
    delays: list[float] = []
    monkeypatch.setattr(jev.time, "sleep", delays.append)
    return delays


def jev_backend(*replies) -> tuple[JevBackend, list[httpx.Request]]:
    http, seen = mock_http(*replies)
    return JevBackend(KEY, http=http), seen


def sent(request: httpx.Request):
    return json.loads(request.content)


def test_jev_sends_one_authorised_post_to_the_system_one_endpoint():
    backend, seen = jev_backend(jev_ok())

    backend.decide(STATE, QUESTIONS)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 30.0
    )


@pytest.mark.parametrize("question_id", ["machine", "urgency", "lockout"])
def test_jev_sends_the_exact_json_for_each_question_type(question_id):
    backend, seen = jev_backend(jev_ok())

    backend.decide(STATE, {question_id: QUESTIONS[question_id]})

    assert sent(seen[0]) == {
        "model": JEV,
        "state": STATE,
        "questions": {question_id: JEV_QUESTIONS[question_id]},
    }


def test_jev_sends_all_the_questions_of_a_request_together():
    backend, seen = jev_backend(jev_ok())
    backend.decide(STATE, QUESTIONS)
    assert len(seen) == 1
    assert sent(seen[0])["questions"] == JEV_QUESTIONS


def test_jev_sends_a_noul_without_criteria_unless_both_descriptions_are_given():
    backend, seen = jev_backend(jev_ok({"q": {"type": "noul", "noul": 0.5}}))
    backend.decide(STATE, {"q": Noul("Is it?")})
    assert sent(seen[0])["questions"] == {
        "q": {"type": "noul", "instructions": "Is it?"}
    }


def test_jev_sends_a_string_state_as_a_string_and_a_dict_state_as_an_object():
    backend, seen = jev_backend(jev_ok())
    state = {"text": STATE, "turns": ["one", "two"], "n": 3}

    backend.decide(STATE, QUESTIONS)
    backend.decide(state, QUESTIONS)

    assert sent(seen[0])["state"] == STATE
    assert sent(seen[1])["state"] == state


def test_jev_sends_the_model_it_was_given():
    http, seen = mock_http(jev_ok())
    backend = JevBackend(KEY, model="jev-1.13.0", http=http)
    backend.decide(STATE, QUESTIONS)
    assert (backend.name, backend.model) == ("jev", "jev-1.13.0")
    assert sent(seen[0])["model"] == "jev-1.13.0"


def test_jev_defaults_to_the_pinned_model_and_never_shows_its_key():
    backend = JevBackend(KEY)  # builds its own client, which does not connect yet
    assert (backend.name, backend.model) == ("jev", JEV)
    assert KEY not in repr(backend) and KEY not in str(backend)


def test_jev_answers_are_normalised_for_each_question_type():
    backend, _ = jev_backend(jev_ok())

    answers, usage = backend.decide(STATE, QUESTIONS)

    assert answers == ANSWERS
    assert usage == Usage(input_tokens=400, cached_input_tokens=0, output_tokens=30)


def test_a_jev_noul_has_yes_and_no_probabilities_and_no_confidence():
    backend, _ = jev_backend(jev_ok({"q": {"type": "noul", "noul": 0.25}}))
    answers, _ = backend.decide(STATE, {"q": Noul("Is it?")})
    assert answers["q"] == Answer(
        "noul", p=0.25, probabilities={"yes": 0.25, "no": 0.75}, backend="jev"
    )
    assert answers["q"].confidence is None


def test_a_jev_score_is_keyed_by_level_index_as_a_string():
    answer = {
        "type": "score",
        "score": 0.5,
        "confidence": 0.4,
        "legend": {"0": "a", "1": "b"},
        "probabilities": {"0": 0.5, "1": 0.5},
    }
    backend, _ = jev_backend(jev_ok({"q": answer}))
    answers, _ = backend.decide(STATE, {"q": Score("Rate it", ["a", "b"])})
    assert answers["q"].probabilities == {"0": 0.5, "1": 0.5}
    assert (answers["q"].kind, answers["q"].score, answers["q"].choice) == (
        "score",
        0.5,
        None,
    )


def test_jev_ignores_answers_to_questions_that_were_not_asked():
    backend, _ = jev_backend(jev_ok())
    answers, _ = backend.decide(STATE, {"lockout": QUESTIONS["lockout"]})
    assert list(answers) == ["lockout"]


# --- JEV retries and errors -------------------------------------------------


def test_a_429_is_retried_and_the_retry_after_header_is_honoured(sleeps):
    backend, seen = jev_backend(
        jev_status(429, "slow down", {"retry-after": "3"}), jev_ok()
    )

    answers, _ = backend.decide(STATE, QUESTIONS)

    assert answers == ANSWERS
    assert len(seen) == 2
    assert sleeps == [3.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
def test_the_server_errors_are_retried(status, sleeps):
    backend, seen = jev_backend(jev_status(status, "try later"), jev_ok())
    assert backend.decide(STATE, QUESTIONS)[0] == ANSWERS
    assert len(seen) == 2


def test_the_retry_wait_is_capped_at_ten_seconds(sleeps):
    backend, _ = jev_backend(jev_status(429, "", {"retry-after": "120"}), jev_ok())
    backend.decide(STATE, QUESTIONS)
    assert sleeps == [10.0]


def test_without_a_usable_retry_after_the_wait_grows_with_each_attempt(sleeps):
    backend, _ = jev_backend(jev_status(503), jev_status(503), jev_ok())
    backend.decide(STATE, QUESTIONS)
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize(
    ("header", "expected"),
    [("soon", 2.0), ("nan", 2.0), ("inf", 2.0), ("-5", 0.0), ("0", 0.0), ("7.5", 7.5)],
)
def test_a_retry_after_that_is_not_a_finite_number_is_replaced_by_the_default(
    header, expected, sleeps
):
    backend, _ = jev_backend(jev_status(429, "", {"retry-after": header}), jev_ok())
    backend.decide(STATE, QUESTIONS)
    assert sleeps == [expected]


def test_three_failed_attempts_raise_after_exactly_three_requests(sleeps):
    backend, seen = jev_backend(jev_status(503, "overloaded"))

    with pytest.raises(DecisionError, match="JEV error 503: overloaded"):
        backend.decide(STATE, QUESTIONS)

    assert len(seen) == 3
    assert len(sleeps) == 2  # it waits between attempts, not after the last


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_errors_raise_at_once_with_the_status_and_no_retry(status, sleeps):
    backend, seen = jev_backend(jev_status(status, "no good"))

    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)

    assert str(raised.value) == f"JEV error {status}: no good"
    assert len(seen) == 1 and sleeps == []


def test_an_error_message_holds_at_most_300_characters_of_the_body():
    backend, _ = jev_backend(jev_status(400, "x" * 1000))
    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)
    assert str(raised.value) == "JEV error 400: " + "x" * 300


def test_an_error_message_never_holds_the_key_even_when_the_server_echoes_it():
    def echo(request):
        header = request.headers["authorization"]
        return httpx.Response(400, text=f"bad request from {header} / {KEY}")

    backend, _ = jev_backend(echo)
    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)
    assert KEY not in str(raised.value)
    assert "ts-test" not in str(raised.value)


def test_the_key_is_removed_before_the_body_is_cut_to_300_characters():
    # A key that starts just inside the cut would otherwise leave a piece of itself.
    backend, _ = jev_backend(jev_status(400, "y" * 290 + KEY))
    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)
    assert "ts-test" not in str(raised.value)
    assert str(raised.value).startswith("JEV error 400: " + "y" * 290)


def test_a_connection_failure_raises_a_decision_error_without_a_retry(sleeps):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    backend, seen = jev_backend(refuse)

    with pytest.raises(
        DecisionError, match="JEV request failed: ConnectError"
    ) as raised:
        backend.decide(STATE, QUESTIONS)

    assert KEY not in str(raised.value)
    assert len(seen) == 1 and sleeps == []


def test_a_transport_error_that_quotes_the_key_leaves_no_trace_in_the_jev_error():
    # What httpx raises for a key with a trailing newline: the whole header.
    def refuse(request):
        raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {CHAIN_KEY}'")

    http, _ = mock_http(refuse)

    with pytest.raises(DecisionError) as raised:
        JevBackend(CHAIN_KEY, http=http).decide(STATE, QUESTIONS)

    assert str(raised.value) == "JEV request failed: LocalProtocolError"
    assert_key_free(raised.value)


def test_a_401_that_echoes_the_key_and_a_masked_copy_leaves_no_trace_in_the_jev_error():
    body = f"Incorrect API key provided: {MASKED_KEY}. Sent {CHAIN_KEY}"
    http, _ = mock_http(jev_status(401, body))

    with pytest.raises(DecisionError) as raised:
        JevBackend(CHAIN_KEY, http=http).decide(STATE, QUESTIONS)

    assert str(raised.value) == (
        "JEV error 401: Incorrect API key provided: [key]. Sent [key]"
    )
    assert_key_free(raised.value)


def test_a_reply_that_is_not_json_raises():
    backend, _ = jev_backend(jev_status(200, "<html>gateway</html>"))
    with pytest.raises(DecisionError, match="not JSON"):
        backend.decide(STATE, QUESTIONS)


def refuse_the_connection(request):
    raise httpx.ConnectError("connection refused", request=request)


# A 2xx reply is billed whether or not it can be used, so none of these is dropped.
# The first group has good answers and a usage report that cannot be trusted.
@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(None, id="no usage"),
        pytest.param({"input_tokens": 0, "output_tokens": 3}, id="0 input tokens"),
        pytest.param({"output_tokens": 3}, id="no input tokens"),
        pytest.param({"input_tokens": -4}, id="a negative count"),
        pytest.param({"input_tokens": "400"}, id="a count that is not a number"),
        pytest.param("lots", id="usage that is not an object"),
    ],
)
def test_a_good_jev_reply_with_no_usable_usage_comes_back_with_an_estimate(usage):
    backend, seen = jev_backend(jev_ok(usage=usage))

    answers, booked = backend.decide(STATE, QUESTIONS)

    assert answers == ANSWERS
    assert booked == Usage(estimated_tokens(seen[0]), estimated=True)


# The second group has a usage report and answers that cannot be used.
@pytest.mark.parametrize(
    ("reply", "message"),
    [
        pytest.param(
            jev_ok(answers={}),
            "no answer for question 'machine'",
            id="no answer for a question",
        ),
        pytest.param(jev_ok(answers=[]), "no answers", id="answers that are a list"),
        pytest.param(
            jev_ok({**JEV_ANSWERS, "lockout": {"type": "noul"}}),
            "'lockout' is malformed",
            id="a noul with no value",
        ),
        pytest.param(
            jev_ok({**JEV_ANSWERS, "machine": {"type": "choice", "choice": "HP-01"}}),
            "'machine' is malformed",
            id="a choice with no probabilities",
        ),
    ],
)
def test_a_jev_reply_with_unusable_answers_raises_with_the_usage_it_reported(
    reply, message
):
    backend, _ = jev_backend(reply)

    with pytest.raises(base.UnusableReply, match=message) as raised:
        backend.decide(STATE, QUESTIONS)

    assert raised.value.usage == Usage(input_tokens=400, output_tokens=30)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


def test_a_jev_reply_with_unusable_answers_and_no_usable_usage_carries_an_estimate():
    backend, seen = jev_backend(jev_ok(answers={}, usage=None))

    with pytest.raises(base.UnusableReply, match="no answer for question") as raised:
        backend.decide(STATE, QUESTIONS)

    assert raised.value.usage == Usage(estimated_tokens(seen[0]), estimated=True)


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        pytest.param(
            jev_status(200, "<html>gateway</html>"),
            "JEV reply is not JSON",
            id="a body that is not JSON",
        ),
        pytest.param(
            lambda: httpx.Response(200, json=[1, 2]),
            "JEV reply is not a JSON object",
            id="a JSON array",
        ),
    ],
)
def test_a_2xx_jev_reply_that_is_not_a_json_object_raises_with_an_estimate(
    reply, message
):
    backend, seen = jev_backend(reply)

    with pytest.raises(base.UnusableReply, match=message) as raised:
        backend.decide(STATE, QUESTIONS)

    assert raised.value.usage == Usage(estimated_tokens(seen[0]), estimated=True)
    assert raised.value.__cause__ is None and raised.value.__context__ is None


def test_the_jev_estimate_counts_the_json_that_was_sent():
    reply = jev_ok({"q": {"type": "noul", "noul": 0.5}}, usage=None)
    backend, seen = jev_backend(reply)

    _, booked = backend.decide("s", {"q": Noul("Is it?")})

    wire = (
        f'{{"model":"{JEV}","state":"s",'
        '"questions":{"q":{"type":"noul","instructions":"Is it?"}}}'
    )
    assert seen[0].content.decode("utf-8") == wire  # the body that httpx sent
    assert booked == Usage(-(-len(wire) // 3), estimated=True)


# --- JEV behind the cache, the budget and the ledger ------------------------


def test_a_cached_jev_request_is_paid_once_and_replayed_free():
    backend, seen = jev_backend(jev_ok())
    decider = CachedDecider(backend, DecisionCache(), SETTINGS)

    first = decider.ask(STATE, QUESTIONS, "triage")
    second = decider.ask(STATE, QUESTIONS, "triage")

    assert first == second == ANSWERS
    assert len(seen) == 1  # the hit made no request
    paid, replayed = ledger_rows()
    assert (paid["cache_hit"], paid["usd"]) == (0, ledger.cost(JEV, 400, 0, 30))
    assert (replayed["cache_hit"], replayed["usd"]) == (1, 0)
    assert (replayed["input_tokens"], replayed["output_tokens"]) == (400, 30)


def test_a_spent_budget_stops_a_jev_request_before_it_is_sent():
    spend_the_budget()
    backend, seen = jev_backend(jev_ok())
    decider = CachedDecider(backend, DecisionCache(), TIGHT)

    with pytest.raises(BudgetExceeded):
        decider.ask(STATE, QUESTIONS, "triage")

    assert seen == []


def test_a_cached_jev_request_with_no_usable_usage_is_booked_as_an_estimate(caplog):
    backend, seen = jev_backend(jev_ok(usage=None))
    decider = CachedDecider(backend, DecisionCache(), SETTINGS)

    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        first = decider.ask(STATE, QUESTIONS, "triage")
        second = decider.ask(STATE, QUESTIONS, "triage")

    estimate = estimated_tokens(seen[0])
    assert first == second == ANSWERS
    assert len(seen) == 1  # the answers were stored, so the second ask made no request
    paid, replayed = ledger_rows()
    tokens = (paid["input_tokens"], paid["cached_input_tokens"], paid["output_tokens"])
    assert paid["purpose"] == "triage [usage estimated]"
    assert tokens == (estimate, 0, 0)
    assert (paid["cache_hit"], paid["usd"]) == (0, ledger.cost(JEV, estimate, 0, 0))
    assert paid["usd"] > 0
    assert (replayed["cache_hit"], replayed["usd"], replayed["input_tokens"]) == (
        1,
        0,
        estimate,
    )
    (warning,) = base_warnings(caplog)  # one, for the paid call; the replay logs none
    assert JEV in warning and "triage" in warning


def test_a_cached_jev_request_with_unusable_answers_books_the_reported_usage():
    reply = jev_ok(
        {**JEV_ANSWERS, "lockout": {"type": "noul"}},
        usage={"input_tokens": 500, "output_tokens": 5},
    )
    backend, seen = jev_backend(reply)
    decider = CachedDecider(backend, DecisionCache(), SETTINGS)

    with pytest.raises(base.UnusableReply, match="'lockout' is malformed"):
        decider.ask(STATE, QUESTIONS, "triage")

    (row,) = ledger_rows()
    assert row["purpose"] == "triage"  # reported usage is real, so no estimate mark
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        500,
        0,
        5,
    )
    assert (row["cache_hit"], row["usd"]) == (0, ledger.cost(JEV, 500, 0, 5))
    assert row["usd"] > 0
    assert decider.cache.get(DecisionCache.key("jev", JEV, STATE, QUESTIONS)) is None
    # Nothing was stored, so asking again goes out again, and is booked again.
    with pytest.raises(base.UnusableReply):
        decider.ask(STATE, QUESTIONS, "triage")
    assert len(seen) == 2 and len(ledger_rows()) == 2


def test_a_cached_jev_request_with_unusable_answers_and_no_usage_books_an_estimate():
    backend, seen = jev_backend(jev_ok(answers={}, usage=None))
    decider = CachedDecider(backend, DecisionCache(), SETTINGS)

    with pytest.raises(base.UnusableReply):
        decider.ask(STATE, QUESTIONS, "triage")

    estimate = estimated_tokens(seen[0])
    (row,) = ledger_rows()
    assert row["purpose"] == "triage [usage estimated]"
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        estimate,
        0,
        0,
    )
    assert row["usd"] == ledger.cost(JEV, estimate, 0, 0) > 0
    assert decider.cache.get(DecisionCache.key("jev", JEV, STATE, QUESTIONS)) is None


def test_the_spend_of_an_unusable_jev_reply_counts_against_the_budget():
    backend, seen = jev_backend(jev_ok(answers={}, usage={"input_tokens": 1_000_000}))
    decider = CachedDecider(backend, DecisionCache(), TIGHT)

    with pytest.raises(base.UnusableReply):
        decider.ask(STATE, QUESTIONS, "first")  # books $0.042, over the $0.01 budget
    with pytest.raises(BudgetExceeded):
        decider.ask(STATE, QUESTIONS, "again")  # so the retry is stopped before it goes

    assert len(seen) == 1


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(jev_status(400, "no good"), id="a 400"),
        pytest.param(jev_status(401, "bad key"), id="a 401"),
        pytest.param(jev_status(503, "overloaded"), id="a 503 on every attempt"),
        pytest.param(refuse_the_connection, id="a refused connection"),
    ],
)
def test_a_failed_jev_request_was_not_billed_so_nothing_is_booked(reply, sleeps):
    backend, _ = jev_backend(reply)
    decider = CachedDecider(backend, DecisionCache(), SETTINGS)

    with pytest.raises(DecisionError) as raised:
        decider.ask(STATE, QUESTIONS, "triage")

    assert not isinstance(raised.value, base.UnusableReply)
    assert ledger_rows() == []
    assert decider.cache.get(DecisionCache.key("jev", JEV, STATE, QUESTIONS)) is None


# --- the Decisions backend --------------------------------------------------

OPENAI_KEY = "sk-test-key-not-real-0123456789abcdef"

DECISIONS_QUESTIONS = {
    "machine": {
        "type": "choice",
        "name": "machine",
        "instructions": "Which machine is this about?",
        "choices": [
            {"value": "HP-01", "description": "Hydraulic press"},
            {"value": "CNC-01", "description": "Machining centre 1"},
            {"value": "unknown", "description": "No machine is named"},
        ],
    },
    "urgency": {
        "type": "score",
        "name": "urgency",
        "instructions": "How urgent is it?",
        "levels": [
            {"label": "L0", "description": "Can wait"},
            {"label": "L1", "description": "This shift"},
            {"label": "L2", "description": "Right now"},
        ],
    },
    "lockout": {
        "type": "predicate",
        "name": "lockout",
        "instructions": (
            "Does the repair need a lockout? "
            "Yes means: Someone must work inside the guarded area. "
            "No means: The machine can be inspected from outside."
        ),
    },
}
# Replies in the shape of the Decision type of the installed openai SDK.
DECISIONS_PAYLOAD = {
    "model": LUNA,
    "answers": [
        {
            "type": "choice",
            "name": "machine",
            "choice": "HP-01",
            "confidence": 0.9,
            "probabilities": [
                {"value": "CNC-01", "probability": 0.03},
                {"value": "HP-01", "probability": 0.95},
                {"value": "unknown", "probability": 0.02},
            ],
        },
        {
            "type": "score",
            "name": "urgency",
            "score": 1.7,
            "confidence": 0.55,
            "probabilities": [
                {"value": 0, "label": "L0", "probability": 0.1},
                {"value": 1, "label": "L1", "probability": 0.1},
                {"value": 2, "label": "L2", "probability": 0.8},
            ],
        },
        {"type": "predicate", "name": "lockout", "probability": 0.98},
    ],
    "usage": {
        "input_tokens": 400,
        "input_tokens_details": {"cache_write_tokens": 0, "cached_tokens": 128},
        "output_tokens": 30,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 430,
    },
}
DECISIONS_ANSWERS = {
    qid: dataclasses.replace(answer, backend="decisions")
    for qid, answer in ANSWERS.items()
}
DECISIONS_USAGE = Usage(input_tokens=400, cached_input_tokens=128, output_tokens=30)


def payload(answers=None, **usage_fields) -> dict:
    """The canned Decisions reply, with other answers or other usage fields."""
    usage = {**DECISIONS_PAYLOAD["usage"], **usage_fields}
    return {
        **DECISIONS_PAYLOAD,
        "answers": DECISIONS_PAYLOAD["answers"] if answers is None else answers,
        "usage": usage,
    }


class FakeDecisions:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeClient:
    """Stands in for openai.OpenAI. It remembers the arguments of create."""

    def __init__(self, reply=None):
        reply = Decision.model_validate(payload()) if reply is None else reply
        self.decisions = FakeDecisions(reply)

    @property
    def calls(self):
        return self.decisions.calls


def decisions_backend(client=None) -> tuple[DecisionsBackend, FakeClient]:
    client = client or FakeClient()
    return DecisionsBackend(OPENAI_KEY, client=client), client


def sdk_client(handler, api_key=OPENAI_KEY) -> openai.OpenAI:
    """The real OpenAI client on a fake transport. It does not retry, so a test sees
    every error at once."""
    transport = httpx2.MockTransport(handler)
    return openai.OpenAI(
        api_key=api_key,
        http_client=httpx2.Client(transport=transport),
        max_retries=0,
    )


def test_decisions_calls_create_with_the_model_the_input_and_the_questions():
    backend, client = decisions_backend()

    backend.decide(STATE, QUESTIONS)

    assert client.calls == [
        {
            "model": LUNA,
            "input": STATE,
            "questions": [
                DECISIONS_QUESTIONS["machine"],
                DECISIONS_QUESTIONS["urgency"],
                DECISIONS_QUESTIONS["lockout"],
            ],
        }
    ]


@pytest.mark.parametrize("question_id", ["machine", "urgency", "lockout"])
def test_decisions_sends_the_exact_question_for_each_question_type(question_id):
    backend, client = decisions_backend()

    backend.decide(STATE, {question_id: QUESTIONS[question_id]})

    assert client.calls[0]["questions"] == [DECISIONS_QUESTIONS[question_id]]


def test_a_noul_without_descriptions_keeps_its_instructions_as_they_are():
    backend, client = decisions_backend(
        FakeClient(Decision.model_validate(payload([DECISIONS_PAYLOAD["answers"][2]])))
    )
    backend.decide(STATE, {"lockout": Noul("Is a lockout needed?")})
    assert client.calls[0]["questions"] == [
        {"type": "predicate", "name": "lockout", "instructions": "Is a lockout needed?"}
    ]


def test_a_dict_state_is_one_user_message_with_one_input_json_part():
    backend, client = decisions_backend()
    state = {"text": STATE, "turns": ["one", "two"], "n": 3}

    backend.decide(state, QUESTIONS)

    assert client.calls[0]["input"] == [
        {"role": "user", "content": [{"type": "input_json", "data": state}]}
    ]


def test_decisions_uses_the_model_it_was_given():
    client = FakeClient()
    backend = DecisionsBackend(OPENAI_KEY, model="gpt-6-luna-next", client=client)
    backend.decide(STATE, QUESTIONS)
    assert (backend.name, backend.model) == ("decisions", "gpt-6-luna-next")
    assert client.calls[0]["model"] == "gpt-6-luna-next"


def test_decisions_builds_a_client_with_the_key_a_30_second_timeout_and_2_retries(
    monkeypatch,
):
    made = []

    class RecordingClient:
        def __init__(self, **kwargs):
            made.append(kwargs)

    monkeypatch.setattr(openai_decisions.openai, "OpenAI", RecordingClient)

    backend = DecisionsBackend(OPENAI_KEY)

    # The retry count is the SDK default, but it is passed so that the policy is
    # written down here and a new SDK default cannot change it unseen.
    assert made == [{"api_key": OPENAI_KEY, "timeout": 30.0, "max_retries": 2}]
    assert (backend.name, backend.model) == ("decisions", LUNA)
    assert OPENAI_KEY not in repr(backend) and OPENAI_KEY not in str(backend)


def test_decisions_answers_are_normalised_for_each_question_type():
    backend, _ = decisions_backend()

    answers, usage = backend.decide(STATE, QUESTIONS)

    assert answers == DECISIONS_ANSWERS
    assert usage == DECISIONS_USAGE


def test_a_decisions_score_is_keyed_by_level_index_not_by_label():
    backend, _ = decisions_backend()
    answers, _ = backend.decide(STATE, QUESTIONS)
    assert answers["urgency"].probabilities == {"0": 0.1, "1": 0.1, "2": 0.8}


def test_a_decisions_predicate_has_yes_and_no_probabilities_and_no_confidence():
    backend, _ = decisions_backend()
    answers, _ = backend.decide(STATE, QUESTIONS)
    assert answers["lockout"].p == 0.98
    assert answers["lockout"].probabilities == {"yes": 0.98, "no": 1 - 0.98}
    assert answers["lockout"].confidence is None


def test_a_refusal_becomes_a_refusal_answer_and_the_other_questions_still_answer():
    refused = [
        DECISIONS_PAYLOAD["answers"][0],
        {"type": "refusal", "name": "urgency"},
        DECISIONS_PAYLOAD["answers"][2],
    ]
    backend, _ = decisions_backend(
        FakeClient(Decision.model_validate(payload(refused)))
    )

    answers, _ = backend.decide(STATE, QUESTIONS)

    assert answers["urgency"] == Answer("refusal", backend="decisions")
    assert answers["machine"] == DECISIONS_ANSWERS["machine"]
    assert answers["lockout"] == DECISIONS_ANSWERS["lockout"]


def test_answers_are_matched_by_question_name_whatever_their_order():
    backwards = list(reversed(DECISIONS_PAYLOAD["answers"]))
    backend, _ = decisions_backend(
        FakeClient(Decision.model_validate(payload(backwards)))
    )
    assert backend.decide(STATE, QUESTIONS)[0] == DECISIONS_ANSWERS


def test_cached_tokens_above_the_input_count_are_booked_as_all_cached_and_logged(
    caplog,
):
    backend, _ = decisions_backend(
        FakeClient(
            Decision.model_validate(
                payload(
                    input_tokens_details={
                        "cache_write_tokens": 0,
                        "cached_tokens": 999,
                    }
                )
            )
        )
    )
    with caplog.at_level(logging.WARNING, logger="app.decide.openai_decisions"):
        _, usage = backend.decide(STATE, QUESTIONS)
    assert usage == Usage(input_tokens=400, cached_input_tokens=400, output_tokens=30)
    assert f"a {LUNA} call reported 999 cached input tokens" in caplog.text
    assert "booking 400 cached" in caplog.text


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (payload([]), "no answer for question 'machine'"),
        (
            payload([{"type": "predicate", "name": "machine", "probability": 0.5}]),
            "'machine' has type 'predicate', expected 'choice'",
        ),
        (
            payload(
                [
                    {
                        "type": "choice",
                        "name": "other",
                        "choice": "a",
                        "confidence": 1.0,
                        "probabilities": [],
                    }
                ]
            ),
            "no answer for question 'machine'",
        ),
    ],
)
def test_a_decisions_reply_with_unusable_answers_raises_with_the_usage_it_reported(
    reply, message
):
    backend, _ = decisions_backend(FakeClient(Decision.model_validate(reply)))

    with pytest.raises(base.UnusableReply, match=message) as raised:
        backend.decide(STATE, QUESTIONS)

    assert raised.value.usage == DECISIONS_USAGE
    assert raised.value.__cause__ is None and raised.value.__context__ is None


# --- Decisions through the real SDK on a mock transport ---------------------


def test_the_real_sdk_sends_the_exact_json_and_its_reply_is_normalised():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json=DECISIONS_PAYLOAD)

    backend = DecisionsBackend(OPENAI_KEY, client=sdk_client(handler))
    state = {"text": STATE}

    answers, usage = backend.decide(state, QUESTIONS)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.openai.com/v1/decisions"
    assert request.headers["authorization"] == f"Bearer {OPENAI_KEY}"
    assert json.loads(request.content) == {
        "model": LUNA,
        "input": [{"role": "user", "content": [{"type": "input_json", "data": state}]}],
        "questions": list(DECISIONS_QUESTIONS.values()),
    }
    assert answers == DECISIONS_ANSWERS and usage == DECISIONS_USAGE


def reply_with(status, **kwargs):
    return lambda request: httpx2.Response(status, **kwargs)


def recording(reply):
    """A handler for sdk_client that answers with reply and keeps each request."""
    seen = []

    def handler(request):
        seen.append(request)
        return reply(request)

    return handler, seen


DECISIONS_NO_USAGE = {k: v for k, v in DECISIONS_PAYLOAD.items() if k != "usage"}


@pytest.mark.parametrize(
    ("reply", "message", "reported"),
    [
        pytest.param(
            reply_with(
                200, text="<html>gateway</html>", headers={"content-type": "text/html"}
            ),
            "Decisions reply has no answers",
            False,
            id="a text body",
        ),
        pytest.param(
            reply_with(200, json={}),
            "Decisions reply has no answers",
            False,
            id="{}",
        ),
        pytest.param(
            reply_with(200, json=[]),
            "Decisions reply has no answers",
            False,
            id="[]",
        ),
        pytest.param(
            reply_with(200, json={**DECISIONS_PAYLOAD, "answers": None}),
            "Decisions reply has no answers",
            True,  # this reply does report its usage
            id="answers null",
        ),
        pytest.param(
            reply_with(200, json={**DECISIONS_PAYLOAD, "answers": [None, "x", 3]}),
            "Decisions reply has no answer for question 'machine'",
            True,
            id="answers that are not objects",
        ),
        pytest.param(
            reply_with(
                200,
                content=b"not json at all",
                headers={"content-type": "application/json"},
            ),
            "Decisions reply is not JSON",
            False,
            id="a JSON content type with a body that is not JSON",
        ),
    ],
)
def test_a_200_reply_of_the_wrong_shape_raises_a_decision_error_not_an_attribute_error(
    reply, message, reported
):
    # Through the real SDK: a text body comes back as a str, [] as a list, {} and
    # "answers": null as an object with no answers, and a lying content type as a
    # JSONDecodeError out of create. All of them were billed, so each error carries
    # what the reply cost: its own usage when it has one, else an estimate.
    handler, seen = recording(reply)
    backend = DecisionsBackend(OPENAI_KEY, client=sdk_client(handler))

    with pytest.raises(base.UnusableReply, match=message) as raised:
        backend.decide(STATE, QUESTIONS)

    expected = (
        DECISIONS_USAGE
        if reported
        else Usage(estimated_tokens(seen[0]), estimated=True)
    )
    assert raised.value.usage == expected
    assert raised.value.__cause__ is None and raised.value.__context__ is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(DECISIONS_NO_USAGE, id="no usage"),
        pytest.param(
            payload(
                input_tokens=0,
                input_tokens_details={"cache_write_tokens": 0, "cached_tokens": 0},
            ),
            id="0 input tokens",
        ),
        pytest.param(payload(input_tokens=0), id="0 input tokens and cached tokens"),
        pytest.param(payload(input_tokens=None), id="no input count"),
        pytest.param(payload(input_tokens_details=None), id="no token details"),
    ],
)
def test_a_good_decisions_reply_with_no_usable_usage_comes_back_with_an_estimate(body):
    handler, seen = recording(reply_with(200, json=body))
    backend = DecisionsBackend(OPENAI_KEY, client=sdk_client(handler))

    answers, booked = backend.decide(STATE, QUESTIONS)

    assert answers == DECISIONS_ANSWERS
    assert booked == Usage(estimated_tokens(seen[0]), estimated=True)


def test_a_decisions_reply_with_unusable_answers_and_no_usage_carries_an_estimate():
    body = {**DECISIONS_NO_USAGE, "answers": []}
    handler, seen = recording(reply_with(200, json=body))
    backend = DecisionsBackend(OPENAI_KEY, client=sdk_client(handler))

    with pytest.raises(base.UnusableReply, match="no answer for question") as raised:
        backend.decide(STATE, QUESTIONS)

    assert raised.value.usage == Usage(estimated_tokens(seen[0]), estimated=True)


def test_the_decisions_estimate_counts_the_json_that_was_sent():
    body = {
        **DECISIONS_NO_USAGE,
        "answers": [{"type": "predicate", "name": "q", "probability": 0.5}],
    }
    handler, seen = recording(reply_with(200, json=body))
    backend = DecisionsBackend(OPENAI_KEY, client=sdk_client(handler))

    _, booked = backend.decide("s", {"q": Noul("Is it?")})

    wire = (
        f'{{"model":"{LUNA}","input":"s",'
        '"questions":[{"type":"predicate","name":"q","instructions":"Is it?"}]}'
    )
    assert seen[0].content.decode("utf-8") == wire  # the body that the SDK sent
    assert booked == Usage(-(-len(wire) // 3), estimated=True)


def test_a_bad_request_raises_a_decision_error_with_the_status_and_no_key():
    error = {"error": {"message": "unknown field", "type": "invalid_request_error"}}
    backend = DecisionsBackend(
        OPENAI_KEY, client=sdk_client(reply_with(400, json=error))
    )

    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)

    assert str(raised.value).startswith("Decisions error 400: ")
    assert "unknown field" in str(raised.value)
    assert OPENAI_KEY not in str(raised.value)


def test_a_rejected_key_is_never_echoed_even_as_the_masked_copy_the_api_returns():
    masked = f"Incorrect API key provided: {OPENAI_KEY[:8]}{'*' * 20}{OPENAI_KEY[-4:]}."
    full = f"also saw the key {OPENAI_KEY} in the header"
    body = {"error": {"message": masked + " " + full}}
    backend = DecisionsBackend(
        OPENAI_KEY, client=sdk_client(reply_with(401, json=body))
    )

    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)

    message = str(raised.value)
    assert message.startswith("Decisions error 401: ")
    assert OPENAI_KEY not in message
    assert "sk-test" not in message and OPENAI_KEY[-4:] not in message


def test_an_error_message_holds_at_most_300_characters_of_the_body_here_too():
    backend = DecisionsBackend(
        OPENAI_KEY, client=sdk_client(reply_with(500, text="x" * 1000))
    )
    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)
    assert str(raised.value) == "Decisions error 500: " + "x" * 300


def test_a_connection_failure_raises_a_decision_error_without_the_key():
    def refuse(request):
        raise httpx2.ConnectError("connection refused", request=request)

    backend = DecisionsBackend(OPENAI_KEY, client=sdk_client(refuse))

    with pytest.raises(
        DecisionError, match="Decisions request failed: APIConnectionError"
    ) as raised:
        backend.decide(STATE, QUESTIONS)

    assert OPENAI_KEY not in str(raised.value)


def test_a_transport_error_that_quotes_the_key_leaves_no_trace_in_the_decisions_error():
    # What httpx2 raises for a key with a trailing newline: the whole header. The SDK
    # wraps it in an APIConnectionError that chains to it.
    def refuse(request):
        raise httpx2.LocalProtocolError(f"Illegal header value b'Bearer {CHAIN_KEY}'")

    backend = DecisionsBackend(CHAIN_KEY, client=sdk_client(refuse, CHAIN_KEY))

    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)

    assert str(raised.value) == "Decisions request failed: APIConnectionError"
    assert_key_free(raised.value)


def test_a_401_that_echoes_a_masked_key_leaves_no_trace_in_the_decisions_error():
    body = {"error": {"message": f"Incorrect API key provided: {MASKED_KEY}."}}
    backend = DecisionsBackend(
        CHAIN_KEY, client=sdk_client(reply_with(401, json=body), CHAIN_KEY)
    )

    with pytest.raises(DecisionError) as raised:
        backend.decide(STATE, QUESTIONS)

    assert str(raised.value).startswith("Decisions error 401: ")
    assert "Incorrect API key provided: [key]." in str(raised.value)
    assert_key_free(raised.value)


# --- Decisions behind the cache, the budget and the ledger ------------------


def test_a_cached_decisions_request_books_its_cached_tokens_and_replays_free():
    client = FakeClient()
    decider = CachedDecider(
        DecisionsBackend(OPENAI_KEY, client=client), DecisionCache(), SETTINGS
    )

    first = decider.ask(STATE, QUESTIONS, "merge")
    second = decider.ask(STATE, QUESTIONS, "merge")

    assert first == second == DECISIONS_ANSWERS
    assert len(client.calls) == 1  # the hit made no request
    paid, replayed = ledger_rows()
    assert paid["model"] == replayed["model"] == LUNA
    assert (paid["input_tokens"], paid["cached_input_tokens"]) == (400, 128)
    assert paid["usd"] == ledger.cost(LUNA, 400, 128, 30) > 0
    assert (replayed["cache_hit"], replayed["usd"]) == (1, 0)
    assert replayed["cached_input_tokens"] == 128


def test_a_spent_budget_stops_a_decisions_request_before_it_is_sent():
    spend_the_budget()
    client = FakeClient()
    decider = CachedDecider(
        DecisionsBackend(OPENAI_KEY, client=client), DecisionCache(), TIGHT
    )

    with pytest.raises(BudgetExceeded):
        decider.ask(STATE, QUESTIONS, "merge")

    assert client.calls == []


def decisions_decider(reply, settings=SETTINGS):
    """A CachedDecider over the real SDK on a mock transport, and the requests that
    the transport sees."""
    handler, seen = recording(reply)
    backend = DecisionsBackend(OPENAI_KEY, client=sdk_client(handler))
    return CachedDecider(backend, DecisionCache(), settings), seen


def decisions_stored(decider) -> bool:
    key = DecisionCache.key("decisions", LUNA, STATE, QUESTIONS)
    return decider.cache.get(key) is not None


def refuse_the_sdk_connection(request):
    raise httpx2.ConnectError("connection refused", request=request)


def test_a_cached_decisions_request_with_no_usable_usage_is_booked_as_an_estimate(
    caplog,
):
    decider, seen = decisions_decider(reply_with(200, json=DECISIONS_NO_USAGE))

    with caplog.at_level(logging.WARNING, logger="app.decide.base"):
        first = decider.ask(STATE, QUESTIONS, "merge")
        second = decider.ask(STATE, QUESTIONS, "merge")

    estimate = estimated_tokens(seen[0])
    assert first == second == DECISIONS_ANSWERS
    assert len(seen) == 1  # the answers were stored, so the second ask made no request
    paid, replayed = ledger_rows()
    tokens = (paid["input_tokens"], paid["cached_input_tokens"], paid["output_tokens"])
    assert (paid["model"], paid["purpose"]) == (LUNA, "merge [usage estimated]")
    assert tokens == (estimate, 0, 0)
    assert (paid["cache_hit"], paid["usd"]) == (0, ledger.cost(LUNA, estimate, 0, 0))
    assert paid["usd"] > 0
    assert (replayed["cache_hit"], replayed["usd"]) == (1, 0)
    (warning,) = base_warnings(caplog)  # one, for the paid call; the replay logs none
    assert LUNA in warning and "merge" in warning


def test_a_cached_decisions_request_with_unusable_answers_books_the_reported_usage():
    decider, _ = decisions_decider(reply_with(200, json=payload([])))

    with pytest.raises(base.UnusableReply, match="no answer for question 'machine'"):
        decider.ask(STATE, QUESTIONS, "merge")

    (row,) = ledger_rows()
    assert (row["model"], row["purpose"]) == (LUNA, "merge")
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        400,
        128,
        30,
    )
    assert (row["cache_hit"], row["usd"]) == (0, ledger.cost(LUNA, 400, 128, 30))
    assert row["usd"] > 0
    assert not decisions_stored(decider)


def test_a_cached_decisions_request_with_a_text_reply_books_an_estimate():
    decider, seen = decisions_decider(
        reply_with(
            200, text="<html>gateway</html>", headers={"content-type": "text/html"}
        )
    )

    with pytest.raises(base.UnusableReply, match="no answers"):
        decider.ask(STATE, QUESTIONS, "merge")

    estimate = estimated_tokens(seen[0])
    (row,) = ledger_rows()
    assert row["purpose"] == "merge [usage estimated]"
    assert row["input_tokens"] == estimate
    assert row["usd"] == ledger.cost(LUNA, estimate, 0, 0) > 0
    assert not decisions_stored(decider)


def test_the_spend_of_an_unusable_decisions_reply_counts_against_the_budget():
    body = payload(
        [],
        input_tokens=1_000_000,
        input_tokens_details={"cache_write_tokens": 0, "cached_tokens": 0},
    )
    decider, seen = decisions_decider(reply_with(200, json=body), TIGHT)

    with pytest.raises(base.UnusableReply):
        decider.ask(STATE, QUESTIONS, "first")  # books $0.10, over the $0.01 budget
    with pytest.raises(BudgetExceeded):
        decider.ask(STATE, QUESTIONS, "again")  # so the retry is stopped before it goes

    assert len(seen) == 1


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(reply_with(400, json={"error": {"message": "no"}}), id="a 400"),
        pytest.param(reply_with(500, text="broken"), id="a 500"),
        pytest.param(refuse_the_sdk_connection, id="a refused connection"),
    ],
)
def test_a_failed_decisions_request_was_not_billed_so_nothing_is_booked(reply):
    decider, _ = decisions_decider(reply)

    with pytest.raises(DecisionError) as raised:
        decider.ask(STATE, QUESTIONS, "merge")

    assert not isinstance(raised.value, base.UnusableReply)
    assert ledger_rows() == []
    assert not decisions_stored(decider)


# --- get_decider and the package exports ------------------------------------

TYPESAFE_KEY = "ts-selection-key-not-real-1"
OPENAI_SELECTION_KEY = "sk-selection-key-not-real-2"
KEY_SETS = {
    "none": {},
    "typesafe": {"typesafe_api_key": TYPESAFE_KEY},
    "openai": {"openai_api_key": OPENAI_SELECTION_KEY},
    "both": {
        "typesafe_api_key": TYPESAFE_KEY,
        "openai_api_key": OPENAI_SELECTION_KEY,
    },
}


def test_the_package_exports_the_public_names():
    from app import decide

    assert decide.Choice is Choice and decide.Score is Score and decide.Noul is Noul
    assert decide.Answer is Answer
    assert decide.NoDecisionBackend is NoDecisionBackend
    assert decide.DecisionError is DecisionError
    assert decide.BudgetExceeded is ledger.BudgetExceeded
    assert decide.Decider is base.Decider and decide.Question is base.Question
    assert callable(decide.get_decider)


@pytest.mark.parametrize(
    ("decider", "keys", "expected"),
    [
        ("jev", "none", ValueError),
        ("jev", "openai", ValueError),
        ("jev", "typesafe", "jev"),
        ("jev", "both", "jev"),
        ("decisions", "none", ValueError),
        ("decisions", "typesafe", ValueError),
        ("decisions", "openai", "decisions"),
        ("decisions", "both", "decisions"),
        ("auto", "none", "cache"),
        ("auto", "typesafe", "jev"),
        ("auto", "openai", "decisions"),
        ("auto", "both", "jev"),
    ],
)
def test_get_decider_selects_the_backend_for_every_decider_and_key_combination(
    decider, keys, expected
):
    from app.decide import get_decider

    settings = Settings(decider=decider, **KEY_SETS[keys])

    if expected is ValueError:
        with pytest.raises(ValueError) as raised:
            get_decider(settings)
        wanted = "TYPESAFE_API_KEY" if decider == "jev" else "OPENAI_API_KEY"
        assert str(raised.value) == f"DECIDER={decider} needs {wanted}"
        # The message names the setting and never holds a key.
        assert TYPESAFE_KEY not in str(raised.value)
        assert OPENAI_SELECTION_KEY not in str(raised.value)
        return

    chosen = get_decider(settings)
    assert chosen.name == expected
    if expected == "cache":
        assert isinstance(chosen, CacheOnlyDecider)
    else:
        assert isinstance(chosen, CachedDecider)  # a live backend is always cached
        backend_class = {"jev": JevBackend, "decisions": DecisionsBackend}[expected]
        assert isinstance(chosen.backend, backend_class)
        assert chosen.model == {"jev": JEV, "decisions": LUNA}[expected]


def test_backend_name_is_the_one_helper_behind_get_decider_for_every_combination():
    from app.decide import backend_name, get_decider

    for decider in ("auto", "jev", "decisions"):
        for keys, key_set in KEY_SETS.items():
            settings = Settings(decider=decider, **key_set)
            try:
                chosen = get_decider(settings)
            except ValueError as refused:
                with pytest.raises(ValueError) as named:
                    backend_name(settings)
                assert str(named.value) == str(refused)
            else:
                assert backend_name(settings) == chosen.name, (decider, keys)


def test_an_empty_key_counts_as_no_key():
    from app.decide import get_decider

    empty = {"typesafe_api_key": "", "openai_api_key": ""}
    assert isinstance(get_decider(Settings(decider="auto", **empty)), CacheOnlyDecider)
    for decider in ("jev", "decisions"):
        with pytest.raises(ValueError):
            get_decider(Settings(decider=decider, **empty))


def test_an_unknown_decider_value_is_refused():
    from app.decide import get_decider

    with pytest.raises(ValueError, match="DECIDER must be one of auto, jev, decisions"):
        get_decider(Settings(decider="gpt", **KEY_SETS["both"]))


@pytest.mark.parametrize(
    ("decider", "keys"),
    [("jev", "typesafe"), ("decisions", "openai"), ("auto", "both")],
)
def test_a_live_decider_uses_the_default_cache_and_the_settings_it_was_made_with(
    decider, keys
):
    from app.decide import get_decider

    spend_the_budget()
    tight = Settings(decider=decider, budget_usd=0.01, **KEY_SETS[keys])

    chosen = get_decider(tight)

    assert chosen.cache.path == base.CACHE_PATH
    assert chosen.settings is tight
    # The spent budget stops the request before it reaches the network, which the
    # isolated fixture would refuse anyway.
    with pytest.raises(BudgetExceeded):
        chosen.ask(STATE, QUESTIONS, "unit test")


def test_a_keyless_decider_replays_a_stored_answer_and_refuses_a_new_request():
    from app.decide import get_decider

    store("jev", JEV)  # into the default cache, which the fixture made temporary
    decider = get_decider(Settings(decider="auto"))

    assert decider.ask(STATE, QUESTIONS, "replay") == ANSWERS
    with pytest.raises(NoDecisionBackend, match="no API key and no cached answer"):
        decider.ask(STATE + " new", QUESTIONS, "replay")
