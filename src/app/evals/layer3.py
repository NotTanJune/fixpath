"""Layer 3 of the evals: how well the triage step works on its own.

Each question in the golden triage set goes through the triage step only, not the rest
of the crew. The picks are then compared with the labels: which machine, which alarm,
which root cause, what the user wants, and whether it is safety-critical. The section
also reports how often triage chose to act, confirm, ask for clarification or decline,
how well it catches safety-critical cases, and whether its confidence matches how often
it is right. It holds no timing or cost, so a replay gives the same section as a live run.

run_triage() runs the questions. score_item() checks one result against its labels.
evaluate() builds the whole section.
"""

from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from app.ask import tools
from app.ask.triage import triage
from app.ask.types import Triage
from app.decide import Decider
from app.evals.match import Matching
from app.evals.metrics import Scored, ratio, reliability

FIELDS = ("machine", "alarm", "root_cause", "intent", "safety")
STATUSES = ("act", "confirm", "clarify", "out_of_scope")
SAFETY_THRESHOLD = 0.5
NO_ALARM = "none"
# The confidence that goes with each field in the calibration tables.
CALIBRATED = ("machine", "root_cause")


def run_triage(
    items: Sequence[dict[str, Any]], kg_path: Path, decider: Decider
) -> list[Triage]:
    """Triage every item's question with no history, in order."""
    with closing(tools.connect_ro(kg_path)) as conn:
        return [triage(item["question"], None, conn, decider) for item in items]


def predicted(result: Triage, matching: Matching) -> dict[str, Any]:
    """The five picks of a triage in the terms of the labels: true ids, the word none
    for no alarm, and the safety flag as a yes or no."""
    mode = result.failure_mode_id
    return {
        "machine": result.asset_id,
        "alarm": result.alarm_code or NO_ALARM,
        "root_cause": (matching.truth_id(mode) or mode) if mode else None,
        "intent": result.intent,
        "safety": (result.safety_critical or 0.0) >= SAFETY_THRESHOLD,
        "safety_p": result.safety_critical,
    }


def score_item(
    item: dict[str, Any], result: Triage, matching: Matching
) -> dict[str, Any]:
    """The picks of one triage and whether each is right. A field with no label is
    marked None and is not scored."""
    labels = item["labels"]
    picks = predicted(result, matching)
    correct: dict[str, bool | None] = {
        name: picks[name] == labels[name] for name in FIELDS
    }
    if labels["root_cause"] is None:
        correct["root_cause"] = None
    if labels["safety"] is None:
        correct["safety"] = None
    expected = item.get("expect_status")
    return {
        "id": item["id"],
        "kind": item.get("kind"),
        "question": item["question"],
        "status": result.status,
        "status_ok": None if not expected else result.status in expected,
        "predicted": picks,
        "correct": correct,
        "confidence": dict(result.confidence),
    }


def _accuracy(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    table = {}
    for name in FIELDS:
        scored = [
            row["correct"][name] for row in rows if row["correct"][name] is not None
        ]
        right = sum(scored)
        table[name] = {
            "correct": right,
            "n": len(scored),
            "accuracy": ratio(right, len(scored)),
        }
    return table


def _all_fields(rows: list[dict[str, Any]]) -> dict[str, Any]:
    whole = [all(v for v in row["correct"].values() if v is not None) for row in rows]
    return {
        "correct": sum(whole),
        "n": len(whole),
        "accuracy": ratio(sum(whole), len(whole)),
    }


def _reliability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tables = {}
    for name in CALIBRATED:
        scored = [
            Scored(row["confidence"].get(name), row["correct"][name])
            for row in rows
            if row["correct"][name] is not None
        ]
        tables[name] = reliability(scored)
    return tables


def _safety(
    items: Sequence[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Recall over the safety-critical items and the false positive rate over the rest.
    Items with no safety label are left out."""
    tp = fn = fp = tn = unscored = 0
    for item, row in zip(items, rows, strict=True):
        label = item["labels"]["safety"]
        flagged = row["predicted"]["safety"]
        if label is None:
            unscored += 1
        elif label:
            tp, fn = tp + flagged, fn + (not flagged)
        else:
            fp, tn = fp + flagged, tn + (not flagged)
    return {
        "scored": tp + fn + fp + tn,
        "unscored": unscored,
        "positives": tp + fn,
        "negatives": fp + tn,
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "recall": ratio(tp, tp + fn),
        "false_positive_rate": ratio(fp, fp + tn),
    }


def evaluate(
    items: Sequence[dict[str, Any]], results: Sequence[Triage], matching: Matching
) -> dict[str, Any]:
    """The layer 3 section for the items and their triage results, in the same order."""
    rows = [score_item(i, r, matching) for i, r in zip(items, results, strict=True)]
    acted = [row for row in rows if row["status"] == "act"]
    counts = {
        name: sum(1 for row in rows if row["status"] == name) for name in STATUSES
    }
    return {
        "n": len(rows),
        "accuracy": _accuracy(rows),
        "all_fields": _all_fields(rows),
        "on_act": {
            "n": len(acted),
            "accuracy": _accuracy(acted),
            "all_fields": _all_fields(acted),
        },
        "status": {
            "count": counts,
            "share": {name: ratio(count, len(rows)) for name, count in counts.items()},
        },
        "safety": _safety(items, rows),
        "reliability": _reliability(rows),
        "gates": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "expect": item["expect_status"],
                "status": row["status"],
                "ok": row["status_ok"],
            }
            for item, row in zip(items, rows, strict=True)
            if item.get("expect_status")
        ],
        "items": rows,
    }
