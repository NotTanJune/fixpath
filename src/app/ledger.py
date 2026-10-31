"""The spend ledger and its hard budget stop.

Every model call is recorded in its own file, apart from the graph, so rebuilding the
graph never resets the spend. `record` writes a call, `spent_usd` totals the spend and
`check_budget` raises `BudgetExceeded` when the budget is used up. Prices are in
dollars per million tokens.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from app.config import (
    CACHE_DIR,
    DECISIONS_MODEL,
    JEV_MODEL,
    SOL_MODEL,
    TERRA_MODEL,
    Settings,
)

LEDGER_PATH = CACHE_DIR / "ledger.sqlite"

# Prices per model: input, cached input and output. The Decisions model and JEV bill
# no output, so their output price is zero.
PRICES: dict[str, tuple[float, float, float]] = {
    SOL_MODEL: (2.00, 0.10, 10.00),
    TERRA_MODEL: (1.00, 0.05, 5.00),
    DECISIONS_MODEL: (0.10, 0.01, 0.00),
    JEV_MODEL: (0.042, 0.042, 0.00),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
    id INTEGER PRIMARY KEY,
    ts TEXT,
    model TEXT,
    purpose TEXT,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    usd REAL,
    cache_hit INTEGER
)
"""


class BudgetExceeded(RuntimeError):
    """Raised when the spend has reached the budget, so no further call may start."""


def _connect() -> sqlite3.Connection:
    """Open the ledger and create it if it is missing."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER_PATH, timeout=30)
    conn.execute(_SCHEMA)
    return conn


def cost(
    model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int
) -> float:
    """Work out the dollar cost of one call.

    Raise an error for an unpriced model, a negative count, or more cached tokens than
    input tokens, since any of these would understate the spend."""
    input_price, cached_price, output_price = PRICES[model]
    if min(input_tokens, cached_input_tokens, output_tokens) < 0:
        raise ValueError(
            "token counts cannot be negative; got "
            f"input {input_tokens}, cached {cached_input_tokens}, "
            f"output {output_tokens}"
        )
    if cached_input_tokens > input_tokens:
        raise ValueError(
            f"cached input tokens ({cached_input_tokens}) are part of the input "
            f"tokens ({input_tokens}), so they cannot exceed them"
        )
    fresh_tokens = input_tokens - cached_input_tokens
    return (
        fresh_tokens * input_price
        + cached_input_tokens * cached_price
        + output_tokens * output_price
    ) / 1_000_000


def record(
    model: str,
    purpose: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    cache_hit: bool,
) -> float:
    """Write one call to the ledger and return its cost.

    A call replayed from the saved answers costs nothing, though its token counts are
    kept. Bad input is rejected before anything is written."""
    usd = cost(model, input_tokens, cached_input_tokens, output_tokens)
    if cache_hit:
        usd = 0.0
    row = (
        datetime.now(UTC).isoformat(timespec="seconds"),
        model,
        purpose,
        input_tokens,
        cached_input_tokens,
        output_tokens,
        usd,
        int(cache_hit),
    )
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO calls(ts, model, purpose, input_tokens, cached_input_tokens,"
            " output_tokens, usd, cache_hit) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    return usd


def spent_usd() -> float:
    """Return the total dollars recorded so far."""
    with closing(_connect()) as conn:
        (total,) = conn.execute("SELECT COALESCE(SUM(usd), 0.0) FROM calls").fetchone()
    return total


def check_budget(settings: Settings) -> None:
    """Raise BudgetExceeded once the spend reaches the budget."""
    spent = spent_usd()
    if spent >= settings.budget_usd:
        raise BudgetExceeded(
            f"model spend of ${spent:.4f} has reached the "
            f"${settings.budget_usd:.2f} budget"
        )
