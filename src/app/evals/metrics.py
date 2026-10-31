"""Shared scoring math for the eval layers.

It holds the plain ratio and precision, recall and F1 calculations. For decisions that
come with a confidence, it also holds the counts at a cut-off, a sweep over cut-offs, a
table that checks whether confidence matches how often a decision is right, and a
suggested cut-off. A ratio with nothing behind it is None, never a made-up number.

prf() gives precision, recall and F1. reliability() checks confidence against reality.
recommend() suggests a cut-off. summarize() builds the report for one kind of question.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

GATE_SWEEP = tuple(round(0.5 + 0.05 * step, 2) for step in range(10))
MIN_PRECISION = 0.98
BINS = 10


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    """Counts with their precision, recall and F1. A ratio is None when it has nothing
    to divide by."""
    denominator = 2 * tp + fp + fn
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, denominator),
    }


@dataclass(frozen=True)
class Scored:
    """A labeled pair: p is the confidence the decider gave, or None for no decision,
    and label says whether the pair really is a match. refused marks a pair that rules
    would block at any cut-off."""

    p: float | None
    label: bool
    refused: bool = False


def confusion(items: list[Scored], gate: float) -> tuple[int, int, int]:
    """The counts (tp, fp, fn) when a confidence at or above the cut-off merges. No
    decision and a refused pair do not merge."""
    tp = fp = fn = 0
    for item in items:
        merged = item.p is not None and item.p >= gate and not item.refused
        if merged and item.label:
            tp += 1
        elif merged:
            fp += 1
        elif item.label:
            fn += 1
    return tp, fp, fn


def metrics_at(items: list[Scored], gate: float) -> dict[str, Any]:
    return {"gate": gate, **prf(*confusion(items, gate))}


def gate_sweep(items: list[Scored]) -> list[dict[str, Any]]:
    return [metrics_at(items, gate) for gate in GATE_SWEEP]


def reliability(items: list[Scored]) -> dict[str, Any]:
    """Ten equal bins of confidence. Each has its count, mean confidence and the share of
    true pairs. The expected calibration error is the average gap between the two,
    weighted by count. Pairs with no confidence are left out."""
    members: list[list[Scored]] = [[] for _ in range(BINS)]
    for item in items:
        if item.p is not None:
            members[min(int(item.p * BINS + 1e-9), BINS - 1)].append(item)
    total = sum(len(m) for m in members)
    bins = []
    error = 0.0
    for number, found in enumerate(members):
        mean_p = sum(i.p for i in found) / len(found) if found else None
        observed = sum(i.label for i in found) / len(found) if found else None
        if found:
            error += len(found) / total * abs(observed - mean_p)
        bins.append(
            {
                "low": round(number / BINS, 1),
                "high": round((number + 1) / BINS, 1),
                "count": len(found),
                "mean_p": mean_p,
                "observed": observed,
            }
        )
    return {"bins": bins, "ece": error if total else None}


def sample_sizes(items: list[Scored]) -> dict[str, int]:
    """How many true and how many false pairs a result rests on."""
    true = sum(1 for i in items if i.label)
    return {"true": true, "false": len(items) - true}


def max_false_p(items: list[Scored]) -> float | None:
    """The highest confidence among false pairs that a cut-off could hold back. A refused
    pair does not count. None when there is none."""
    found = [i.p for i in items if not i.label and not i.refused and i.p is not None]
    return max(found) if found else None


def min_true_p(items: list[Scored], gate: float = 0.0) -> float | None:
    """The lowest confidence among true pairs that reach the cut-off, or None."""
    found = [i.p for i in items if i.label and i.p is not None and i.p >= gate]
    return min(found) if found else None


def recommend(
    items: list[Scored], min_precision: float = MIN_PRECISION
) -> dict[str, Any]:
    """The suggested cut-off, with the counts behind it. Among the cut-offs with enough
    precision, it takes the highest one that keeps the best recall. If no false pair
    scores above it, the data does not fix it, so the result shows the gap between the
    false and true pairs. The gate is None, with a reason, when no cut-off qualifies."""
    sample = sample_sizes(items)
    qualifying = [
        row
        for row in gate_sweep(items)
        if row["precision"] is not None and row["precision"] >= min_precision
    ]
    if not qualifying:
        return {
            "gate": None,
            "reason": f"no gate from {GATE_SWEEP[0]} to {GATE_SWEEP[-1]} reaches "
            f"precision {min_precision}",
            "sample": sample,
        }
    best = max(
        row["recall"] if row["recall"] is not None else -1.0 for row in qualifying
    )
    row = max(
        (
            r
            for r in qualifying
            if (r["recall"] if r["recall"] is not None else -1.0) == best
        ),
        key=lambda r: r["gate"],
    )
    highest_false = max_false_p(items)
    unbound = highest_false is None or highest_false < row["gate"]
    result = {**row, "unbound": unbound, "sample": sample}
    if unbound:
        result["gap"] = {
            "max_false_p": highest_false,
            "min_true_p": min_true_p(items, row["gate"]),
        }
    return result


def summarize(
    items: list[Scored],
    *,
    gate: float,
    band: Callable[[float | None], str],
    unlabeled: int,
    band_names: tuple[str, ...] = ("merge", "queue", "none", "undecided"),
) -> dict[str, Any]:
    """The report for one kind of question: metrics at the current cut-off, how true and
    false pairs spread across the bands, the sweep, the confidence check, and the
    suggested cut-off."""
    bands: dict[str, dict[str, int]] = {}
    for item in items:
        counts = bands.setdefault(band(item.p), {"true": 0, "false": 0})
        counts["true" if item.label else "false"] += 1
    for name in band_names:
        bands.setdefault(name, {"true": 0, "false": 0})
    true = sum(1 for i in items if i.label)
    return {
        "gate": gate,
        "pairs": {
            "labeled": len(items),
            "true": true,
            "false": len(items) - true,
            "unlabeled": unlabeled,
        },
        "sample": sample_sizes(items),
        "max_false_p": max_false_p(items),
        "min_true_p": min_true_p(items),
        "at_gate": metrics_at(items, gate),
        "bands": bands,
        "sweep": gate_sweep(items),
        "reliability": reliability(items),
        "recommendation": recommend(items),
    }
