"""Sends the golden question sets and the per-question scores to Langfuse.

Run it with python -m app.evals --push. It creates or updates one dataset for each
golden set and one item for each question. Every item also gets a trace with its
question and outcome, and the checks of that item become scores on the trace: yes or no
scores for the code checks, numbers for the judge's scores. A check that does not apply
is not sent. The ids are fixed by the run name, dataset, item and score, so pushing
again with the same run name updates the data and never adds copies.

push() does the work and takes the Langfuse client as an argument, so tests can pass a
fake one.
"""

import hashlib
from collections.abc import Sequence
from typing import Any

from app.brand import load_brand

SETS = (
    (
        "triage",
        "layer3",
        "triage",
        "Technician questions for triage: machine, alarm, cause.",
    ),
    (
        "answers",
        "layer4",
        "answer",
        "Technician questions for the answer card, with traps.",
    ),
)


def dataset_name(kind: str) -> str:
    return f"{load_brand().slug}-golden-{kind}"


def score_id(run_name: str, dataset: str, item_id: str, score: str) -> str:
    """A score id that depends only on where the score belongs, so pushing the same run
    again replaces the score."""
    text = ":".join((run_name, dataset, item_id, score))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _scores(prefix: str, row: dict[str, Any]) -> list[tuple[str, float, str]]:
    """(name, value, data type) for every check that was made on a result row."""
    checks = row.get("correct") if "correct" in row else row.get("checks", {})
    scores = [
        (f"{prefix}.{name}", 1.0 if flag else 0.0, "BOOLEAN")
        for name, flag in checks.items()
        if flag is not None
    ]
    if "ok" in row:
        scores.append((f"{prefix}.ok", 1.0 if row["ok"] else 0.0, "BOOLEAN"))
    for name, value in (row.get("judge") or {}).items():
        scores.append((f"judge.{name}", float(value), "NUMERIC"))
    return scores


def push(
    results: dict[str, Any],
    triage_items: Sequence[dict[str, Any]],
    answer_items: Sequence[dict[str, Any]],
    client: Any,
    *,
    run_name: str,
) -> dict[str, int]:
    """Push both sets and flush the client. Returns how many datasets, items and scores
    were sent. Raises an error if the results lack the layer 3 or layer 4 section."""
    for _kind, key, _prefix, _text in SETS:
        if key not in results:
            raise ValueError(
                f"the results have no {key} section; run `python -m app.evals` "
                "for it first"
            )
    golden = {"triage": triage_items, "answers": answer_items}
    summary = {"datasets": 0, "items": 0, "scores": 0}
    for kind, key, prefix, description in SETS:
        name = dataset_name(kind)
        client.create_dataset(name=name, description=description)
        summary["datasets"] += 1
        rows = {row["id"]: row for row in results[key]["items"]}
        for item in golden[kind]:
            row = rows.get(item["id"])
            if row is None:
                continue
            trace_id = client.create_trace_id(seed=f"{run_name}:{name}:{item['id']}")
            span = client.start_observation(
                name=f"eval {kind}",
                trace_context={"trace_id": trace_id},
                input=item["question"],
                output={"status": row.get("status"), "predicted": row.get("predicted")},
                metadata={"run": run_name, "item": item["id"]},
            )
            span.end()
            client.create_dataset_item(
                dataset_name=name,
                id=f"{name}:{item['id']}",
                input={"question": item["question"]},
                expected_output=item.get("labels") or item.get("expect"),
                metadata={
                    k: item.get(k) for k in ("kind", "source", "trap") if k in item
                },
                source_trace_id=trace_id,
            )
            summary["items"] += 1
            for score, value, data_type in _scores(prefix, row):
                client.create_score(
                    name=score,
                    score_id=score_id(run_name, name, item["id"], score),
                    value=value,
                    trace_id=trace_id,
                    data_type=data_type,
                    comment=item["question"],
                )
                summary["scores"] += 1
    client.flush()
    return summary
