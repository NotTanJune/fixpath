"""Predict which parts are due to fail soon, from the dated breakdowns in the graph.

No model is involved. Only gradual causes such as wear, drift, blockage and
contamination are predicted, since a one-off fault does not repeat on a schedule.
Breakdowns of the same machine and cause within a week of each other count as one
episode, because a repair that did not hold is the same failure. The usual gap between
episodes, added to the last one, gives the next due date. Each prediction lists the
breakdowns it rests on, with quotes from their source files.

upkeep builds the whole answer for the API: the predictions and example service terms.
"""

import datetime as dt
import itertools
import json
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

EPISODE_DAYS = 7  # repeats within this many days are one episode
SOON_DAYS = 14  # due within this many days counts as due soon
MIN_EPISODES = 3  # fewer episodes than this give no prediction
GRADUAL = ("wear", "drift", "blockage", "contamination")
ORDER = {"overdue": 0, "due_soon": 1, "later": 2, "not_enough_history": 3}

# Example contract terms, made up for the demo. They are not in the graph.
TERMS = {
    "CNC-01": (8, 0.97, "Spindle and coolant system"),
    "CNC-02": (8, 0.97, "Spindle and coolant system"),
    "HP-01": (4, 0.98, "Hydraulic cylinder, seals and valves"),
    "LT-01": (24, 0.95, "Sensors and test fixtures"),
    "RW-01": (8, 0.96, "Gripper and end effector"),
}

_BREAKDOWNS = """
SELECT i.id, i.props, m.id, m.label, c.id, c.label
FROM nodes AS i
JOIN edges AS o ON o.src = i.id AND o.rel = 'OCCURRED_ON'
JOIN nodes AS m ON m.id = o.dst
JOIN edges AS r ON r.src = i.id AND r.rel = 'ROOT_CAUSE'
JOIN nodes AS c ON c.id = r.dst
WHERE i.type = 'Incident'
"""

_CITATIONS = """
SELECT d.path, e.page, e.chunk_id, e.quote
FROM evidence AS e JOIN documents AS d ON d.id = e.doc_id
WHERE e.fact_kind = 'node' AND e.fact_id = ?
ORDER BY e.confidence DESC, e.id
LIMIT 3
"""


def is_gradual(cause_label: str) -> bool:
    """True when the cause wears in over time rather than happening at random."""
    label = cause_label.lower()
    return any(word in label for word in GRADUAL)


def episodes(dates: list[dt.date]) -> list[dt.date]:
    """The start date of each episode. A date within a week of the previous one joins it."""
    starts: list[dt.date] = []
    last: dt.date | None = None
    for day in sorted(dates):
        if last is None or (day - last).days > EPISODE_DAYS:
            starts.append(day)
        last = day
    return starts


def predict(starts: list[dt.date], today: dt.date) -> dict[str, Any]:
    """The usual gap, the due date and the status for one machine and cause."""
    if len(starts) < MIN_EPISODES:
        return {"status": "not_enough_history", "gap_days": None, "due": None}
    gaps = [(b - a).days for a, b in itertools.pairwise(starts)]
    gap = round(statistics.median(gaps))
    due = starts[-1] + dt.timedelta(days=gap)
    if due < today:
        status = "overdue"
    elif (due - today).days <= SOON_DAYS:
        status = "due_soon"
    else:
        status = "later"
    return {"status": status, "gap_days": gap, "due": due.isoformat()}


def _breakdowns(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every breakdown with its machine, cause, date, downtime and source quotes."""
    out = []
    for inc, props, machine, machine_label, cause, cause_label in conn.execute(
        _BREAKDOWNS
    ):
        data = json.loads(props)
        citations = [
            {"doc_path": path, "page": page, "chunk_id": chunk, "quote": quote}
            for path, page, chunk, quote in conn.execute(_CITATIONS, (inc,))
        ]
        out.append(
            {
                "id": inc,
                "date": dt.date.fromisoformat(data["date"]),
                "downtime_min": data.get("downtime_min"),
                "machine": machine,
                "machine_label": machine_label,
                "cause": cause,
                "cause_label": cause_label,
                "citations": citations,
            }
        )
    return out


def _item(group: list[dict[str, Any]], today: dt.date) -> dict[str, Any]:
    """One prediction for one machine and cause."""
    group.sort(key=lambda r: (r["date"], r["id"]))
    starts = episodes([r["date"] for r in group])
    first = group[0]
    return {
        "machine": first["machine"],
        "machine_label": first["machine_label"],
        "cause": first["cause"],
        "cause_label": first["cause_label"],
        "episodes": len(starts),
        "last": starts[-1].isoformat(),
        **predict(starts, today),
        "breakdowns": [
            {
                "id": r["id"],
                "date": r["date"].isoformat(),
                "downtime_min": r["downtime_min"],
                "citations": r["citations"],
            }
            for r in group
        ],
    }


def _terms(rows: list[dict[str, Any]], today: dt.date) -> list[dict[str, Any]]:
    """The example terms per machine, with the uptime the recorded downtime gives."""
    first = min(r["date"] for r in rows)
    minutes = ((today - first).days + 1) * 24 * 60
    terms = []
    for machine, (hours, target, covers) in sorted(TERMS.items()):
        down = sum(r["downtime_min"] or 0 for r in rows if r["machine"] == machine)
        terms.append(
            {
                "machine": machine,
                "response_hours": hours,
                "uptime_target": target,
                "covers": covers,
                "measured_uptime": round(1 - down / minutes, 4),
                "downtime_min": down,
            }
        )
    return terms


def upkeep(kg_path: Path) -> dict[str, Any]:
    """The predictions and the example service terms, as the API returns them.

    Today is the date of the latest breakdown, so the answer is the same every run."""
    uri = f"{Path(kg_path).resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = _breakdowns(conn)
    if not rows:
        return {"as_of": None, "items": [], "terms": []}
    today = max(r["date"] for r in rows)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if is_gradual(r["cause_label"]):
            groups[(r["machine"], r["cause"])].append(r)
    items = [_item(group, today) for group in groups.values()]
    items.sort(
        key=lambda i: (ORDER[i["status"]], i["due"] or "", i["machine"], i["cause"])
    )
    return {"as_of": today.isoformat(), "items": items, "terms": _terms(rows, today)}
