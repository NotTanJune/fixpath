"""Tests for the spend ledger: model prices, the record of what each call cost, and
the budget limit that stops further calls.

Every test uses a temporary ledger file and never touches data/cache.
"""

import datetime as dt
import sqlite3
from contextlib import closing

import pytest

from app import config, ledger
from app.config import Settings

DEFAULT_LEDGER_PATH = ledger.LEDGER_PATH  # read before any fixture changes it

SOL, TERRA = config.SOL_MODEL, config.TERRA_MODEL
LUNA, JEV = config.DECISIONS_MODEL, config.JEV_MODEL
MILLION = 1_000_000


@pytest.fixture(autouse=True)
def temporary_ledger(tmp_path, monkeypatch):
    path = tmp_path / "cache" / "ledger.sqlite"
    monkeypatch.setattr(ledger, "LEDGER_PATH", path)
    return path


def rows(path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM calls ORDER BY id").fetchall()


def settings(budget: float) -> Settings:
    return Settings(budget_usd=budget)


# --- where the ledger lives -------------------------------------------------


def test_the_ledger_has_its_own_file_apart_from_the_graph():
    # A separate file is what lets a graph rebuild leave the spend alone.
    assert DEFAULT_LEDGER_PATH == config.CACHE_DIR / "ledger.sqlite"
    assert DEFAULT_LEDGER_PATH != config.KG_PATH


# --- the prices -------------------------------------------------------------


def test_prices_are_per_million_tokens_as_input_cached_input_output():
    assert ledger.PRICES == {
        SOL: (2.00, 0.10, 10.00),
        TERRA: (1.00, 0.05, 5.00),
        LUNA: (0.10, 0.01, 0.00),
        JEV: (0.042, 0.042, 0.00),
    }


@pytest.mark.parametrize(
    ("model", "input_tokens", "cached", "output_tokens", "expected"),
    [
        (SOL, MILLION, 0, 0, 2.00),
        (SOL, MILLION, MILLION, 0, 0.10),
        (SOL, 0, 0, MILLION, 10.00),
        (TERRA, MILLION, 0, 0, 1.00),
        (TERRA, MILLION, MILLION, 0, 0.05),
        (TERRA, 0, 0, MILLION, 5.00),
        (LUNA, MILLION, 0, 0, 0.10),
        (LUNA, MILLION, MILLION, 0, 0.01),
        (LUNA, 0, 0, MILLION, 0.00),
        (JEV, MILLION, 0, 0, 0.042),
        (JEV, MILLION, MILLION, 0, 0.042),
        (JEV, 0, 0, MILLION, 0.00),
    ],
)
def test_record_prices_each_model(model, input_tokens, cached, output_tokens, expected):
    usd = ledger.record(model, "test", input_tokens, cached, output_tokens, False)
    assert usd == pytest.approx(expected)
    assert ledger.spent_usd() == pytest.approx(expected)


def test_cached_tokens_are_part_of_the_input_and_billed_at_the_cached_rate():
    # 1,000,000 input tokens of which 400,000 were cached:
    # 600,000 at $2.00 + 400,000 at $0.10 + 500,000 output at $10.00 per million.
    usd = ledger.record(SOL, "test", MILLION, 400_000, 500_000, False)
    assert usd == pytest.approx(1.2 + 0.04 + 5.0)


def test_a_cache_hit_costs_nothing_and_keeps_the_token_counts():
    usd = ledger.record(SOL, "replay", MILLION, 400_000, 500_000, True)
    assert usd == 0
    assert ledger.spent_usd() == 0
    (row,) = rows(ledger.LEDGER_PATH)
    assert row["usd"] == 0
    assert row["cache_hit"] == 1
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (
        MILLION,
        400_000,
        500_000,
    )


def test_an_unknown_model_raises_key_error_and_writes_nothing():
    with pytest.raises(KeyError):
        ledger.record("gpt-0-nope", "test", 1, 0, 1, False)
    with pytest.raises(KeyError):
        ledger.record("gpt-0-nope", "test", 1, 0, 1, True)  # even a hit names a model
    assert ledger.spent_usd() == 0


# --- counts that cannot be right ---------------------------------------------


@pytest.mark.parametrize(
    "counts",
    [(-1, 0, 0), (100, -1, 0), (100, 0, -1)],
    ids=["input", "cached input", "output"],
)
def test_a_negative_token_count_is_rejected_and_writes_nothing(counts):
    with pytest.raises(ValueError, match="negative"):
        ledger.cost(SOL, *counts)
    with pytest.raises(ValueError, match="negative"):
        ledger.record(SOL, "test", *counts, False)
    assert ledger.spent_usd() == 0


def test_more_cached_tokens_than_input_tokens_is_rejected_and_writes_nothing():
    # Cached tokens are part of the input. 20 cached out of 10 would give a negative
    # number of fresh tokens and lower the recorded spend.
    with pytest.raises(ValueError, match="cached"):
        ledger.cost(SOL, 10, 20, 0)
    with pytest.raises(ValueError, match="cached"):
        ledger.record(SOL, "test", 10, 20, 0, False)
    with pytest.raises(ValueError, match="cached"):
        ledger.record(SOL, "replay", 10, 20, 0, True)  # a replay is checked too
    assert ledger.spent_usd() == 0


def test_a_prompt_that_is_entirely_cached_is_fine():
    assert ledger.cost(SOL, 10, 10, 0) == pytest.approx(10 * 0.10 / MILLION)


# --- the table --------------------------------------------------------------


def test_calls_table_has_exactly_the_specified_columns():
    ledger.record(SOL, "test", 1, 0, 1, False)
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        columns = [
            (name, kind, pk)
            for _, name, kind, _, _, pk in conn.execute("PRAGMA table_info(calls)")
        ]
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
    assert tables == ["calls"]
    assert columns == [
        ("id", "INTEGER", 1),
        ("ts", "TEXT", 0),
        ("model", "TEXT", 0),
        ("purpose", "TEXT", 0),
        ("input_tokens", "INTEGER", 0),
        ("cached_input_tokens", "INTEGER", 0),
        ("output_tokens", "INTEGER", 0),
        ("usd", "REAL", 0),
        ("cache_hit", "INTEGER", 0),
    ]


def test_record_writes_one_row_per_call():
    before = dt.datetime.now(dt.UTC).replace(microsecond=0)
    ledger.record(SOL, "extract alarms", 1200, 200, 300, False)
    ledger.record(TERRA, "rank causes", 50, 0, 10, True)
    first, second = rows(ledger.LEDGER_PATH)

    assert (first["model"], first["purpose"]) == (SOL, "extract alarms")
    assert (first["input_tokens"], first["cached_input_tokens"]) == (1200, 200)
    assert (first["output_tokens"], first["cache_hit"]) == (300, 0)
    assert first["usd"] == pytest.approx((1000 * 2.0 + 200 * 0.1 + 300 * 10.0) / 1e6)
    assert (second["model"], second["purpose"], second["cache_hit"]) == (
        TERRA,
        "rank causes",
        1,
    )
    stamp = dt.datetime.fromisoformat(first["ts"])
    assert stamp.tzinfo is not None
    assert stamp >= before


def test_the_ledger_creates_its_folder_on_first_use(temporary_ledger):
    assert not temporary_ledger.parent.exists()
    assert ledger.spent_usd() == 0
    assert temporary_ledger.exists()


def test_the_ledger_persists_between_connections():
    ledger.record(SOL, "one", 0, 0, 100_000, False)
    ledger.record(SOL, "two", 0, 0, 100_000, False)
    assert ledger.spent_usd() == pytest.approx(2.0)
    assert len(rows(ledger.LEDGER_PATH)) == 2


# --- the budget stop --------------------------------------------------------


def test_spent_usd_is_zero_for_a_new_ledger():
    assert ledger.spent_usd() == 0.0


def test_spent_usd_adds_real_calls_and_ignores_hits():
    ledger.record(SOL, "a", 0, 0, 100_000, False)  # $1.00
    ledger.record(TERRA, "b", 0, 0, 100_000, False)  # $0.50
    ledger.record(SOL, "c", 0, 0, 100_000, True)  # replayed, free
    assert ledger.spent_usd() == pytest.approx(1.5)


def test_check_budget_passes_below_the_budget():
    ledger.record(SOL, "a", 0, 0, 400_000, False)  # $4.00
    ledger.check_budget(settings(5.0))  # does not raise


def test_check_budget_raises_once_the_spend_reaches_the_budget():
    ledger.record(SOL, "a", 0, 0, 400_000, False)  # $4.00
    ledger.check_budget(settings(5.0))
    ledger.record(SOL, "b", 0, 0, 100_000, False)  # exactly $5.00
    with pytest.raises(ledger.BudgetExceeded) as raised:
        ledger.check_budget(settings(5.0))
    assert "5.00" in str(raised.value)


def test_check_budget_reads_the_limit_from_the_settings_it_is_given():
    ledger.record(SOL, "a", 0, 0, 100_000, False)  # $1.00
    ledger.check_budget(settings(40.0))
    with pytest.raises(ledger.BudgetExceeded):
        ledger.check_budget(settings(0.5))


def test_cache_hits_never_push_the_spend_to_the_budget():
    for _ in range(50):
        ledger.record(SOL, "replay", MILLION, 0, MILLION, True)
    ledger.check_budget(settings(0.01))


def test_budget_exceeded_is_an_exception_that_callers_can_catch_by_name():
    assert issubclass(ledger.BudgetExceeded, Exception)
