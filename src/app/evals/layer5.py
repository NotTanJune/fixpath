"""Layer 5 of the evals: how fast and how costly the answers are.

The numbers come from the traces of the cards scored in layer 4. They cover the time of
each step and of a whole answer, the cost per answer, and the share of model steps that
were served from a cache. Only a run that mostly made live calls gives real speed and
cost. A replayed run is fast and nearly free, so the section keeps the numbers of the
last live run and marks the new run as replayed.

evaluate() builds the section. percentile() is the small helper used for the medians and
the slow tail.
"""

import datetime as dt
from collections.abc import Sequence
from typing import Any

from app.evals.layer4 import Run
from app.evals.metrics import ratio

# A run counts as live only when its share of cache hits is under this.
LIVE_BELOW = 0.5
COST_NOTE = (
    "The mean cost per answer includes the answers that do not retrieve (the non-"
    "retrieving traps stop at triage and cost almost nothing), so it is lower than the "
    "cost of an answer that does. Read the p50 next to the mean."
)


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile, using straight-line steps between ranks. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _spread(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
    }


def _answer_seconds(run: Run) -> float | None:
    trace = run.card.trace
    if not trace:
        return None
    start = min(t.started_at for t in trace)
    end = max(t.ended_at for t in trace)
    return (end - start).total_seconds()


def _live(runs: Sequence[Run], today: str) -> dict[str, Any]:
    by_node: dict[str, list[float]] = {}
    for run in runs:
        for t in run.card.trace:
            by_node.setdefault(t.name, []).append(t.seconds)
    answers = [s for s in (_answer_seconds(r) for r in runs) if s is not None]
    costs = [sum(t.cost_usd for t in r.card.trace) for r in runs]
    return {
        "measured_at": today,
        "answers": len(runs),
        "latency": {
            "answer": _spread(answers),
            "nodes": {
                name: _spread(values) for name, values in sorted(by_node.items())
            },
        },
        "cost_per_answer": {
            "mean": sum(costs) / len(costs) if costs else None,
            "p50": percentile(costs, 50),
            "p95": percentile(costs, 95),
            "total": sum(costs),
        },
        "cache_hit_share": _cache_hit_share(runs),
    }


def _cache_hit_share(runs: Sequence[Run]) -> float | None:
    flags = [t.cache_hit for r in runs for t in r.card.trace if t.cache_hit is not None]
    return ratio(sum(1 for f in flags if f), len(flags))


def evaluate(
    runs: Sequence[Run], previous: dict[str, Any] | None, today: str | None = None
) -> dict[str, Any]:
    """The layer 5 section. previous is the section from the last results file. A run
    that is mostly cached keeps the live numbers from there."""
    today = today or dt.datetime.now(dt.UTC).date().isoformat()
    flags = [t.cache_hit for r in runs for t in r.card.trace if t.cache_hit is not None]
    share = ratio(sum(1 for f in flags if f), len(flags))
    replayed = share is None or share >= LIVE_BELOW
    section: dict[str, Any] = {
        "this_run": {
            "answers": len(runs),
            "cache_hit_share": share,
            "replayed": replayed,
        },
        "cost_note": COST_NOTE,
    }
    if replayed:
        section["live"] = (previous or {}).get("live")
    else:
        section["live"] = _live(runs, today)
    return section
