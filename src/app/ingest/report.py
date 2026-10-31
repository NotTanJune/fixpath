"""Builds the report that shows what happened to each file in a pipeline run.

build_report gathers the results for each file and the totals for the whole run,
including cost. write_report saves it as a JSON file that the Pipeline view reads.
A full run writes latest.json. A run on one file writes last_file.json instead, so
the full report is kept.
"""

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

from app import ledger
from app.ingest.types import Extraction, Resolution
from app.ingest.validate import Validated, edge_subject

LATEST = Path("pipeline") / "latest.json"
LAST_FILE = Path("pipeline") / "last_file.json"


def default_report_path(kg_path: Path, mode: str = "full") -> Path:
    """Return the default report path, next to the database."""
    return Path(kg_path).parent / (LAST_FILE if mode == "file" else LATEST)


# --- the ledger -------------------------------------------------------------------


def ledger_mark() -> int:
    """Return the id of the last cost ledger row, or 0 if there are none.

    Take it before a run, then pass it to ledger_rows_since afterwards.
    """
    if not ledger.LEDGER_PATH.exists():
        return 0
    with closing(sqlite3.connect(ledger.LEDGER_PATH, timeout=30)) as conn:
        try:
            (last,) = conn.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()
        except sqlite3.OperationalError:  # the file exists but has no table yet
            return 0
    return last


def ledger_rows_since(mark: int) -> list[dict[str, Any]]:
    """Return the cost ledger rows added after the mark, oldest first."""
    if not ledger.LEDGER_PATH.exists():
        return []
    with closing(sqlite3.connect(ledger.LEDGER_PATH, timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute(
                "SELECT * FROM calls WHERE id > ? ORDER BY id", (mark,)
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(row) for row in found]


# --- the report --------------------------------------------------------------------


def _counts(items) -> dict[str, int]:
    return dict(sorted(Counter(items).items()))


def _cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_purpose: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cache_hits": 0, "usd": 0.0}
    )
    for row in rows:
        entry = by_purpose[row["purpose"]]
        entry["calls"] += 1
        entry["cache_hits"] += int(row["cache_hit"])
        entry["usd"] += row["usd"]
    hits = sum(int(row["cache_hit"]) for row in rows)
    return {
        "calls": len(rows),
        "cache_hits": hits,
        "paid_calls": len(rows) - hits,
        "usd": round(sum(row["usd"] for row in rows), 6),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "by_purpose": {
            purpose: {**entry, "usd": round(entry["usd"], 6)}
            for purpose, entry in sorted(by_purpose.items())
        },
    }


def _docs_by_subject(resolution: Resolution) -> dict[str, set[str]]:
    """Return the source files behind each node and each edge."""
    docs: dict[str, set[str]] = defaultdict(set)
    for node in resolution.nodes:
        docs[node.id] |= {e.doc_path for e in node.evidence} | set(node.sources)
    for edge in resolution.edges:
        docs[edge_subject(edge)] |= {e.doc_path for e in edge.evidence}
    return docs


def build_report(
    extractions: list[Extraction],
    resolution: Resolution,
    validated: Validated,
    ledger_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the report of one run, with a section per file and the run totals."""
    docs = _docs_by_subject(resolution)
    flags = [flag.model_dump(mode="json") for flag in validated.flags]
    files = []
    for extraction in extractions:
        path = extraction.doc_path
        files.append(
            {
                "doc_path": path,
                "doc_type": extraction.doc_type,
                "extractor": extraction.extractor,
                "candidates": {
                    "nodes": _counts(n.ref.type for n in extraction.nodes),
                    "edges": _counts(e.rel for e in extraction.edges),
                    "chunks": len(extraction.chunks),
                },
                "notes": list(extraction.notes),
                "merges": [
                    m.model_dump(mode="json")
                    for m in resolution.merges
                    if m.doc_path == path
                ],
                "review": [
                    r.model_dump(mode="json")
                    for r in resolution.review
                    if path in r.doc_paths
                ],
                "flags": [f for f in flags if path in docs.get(f["subject"], ())],
            }
        )
    listed = {extraction.doc_path for extraction in extractions}
    return {
        "files": files,
        "totals": {
            "nodes": _counts(n.type for n in validated.nodes),
            "edges": _counts(e.rel for e in validated.edges),
            "review": _counts(r.kind for r in resolution.review),
            "merges": _counts(m.decision for m in resolution.merges),
            "flags": {
                "error": sum(f["severity"] == "error" for f in flags),
                "warning": sum(f["severity"] == "warning" for f in flags),
                "by_rule": _counts(f["rule"] for f in flags),
            },
            "cost": _cost(ledger_rows),
        },
        "unassigned_flags": [
            f for f in flags if not (docs.get(f["subject"], set()) & listed)
        ],
    }


def write_report(report: dict[str, Any], path: Path) -> Path:
    """Write the report as readable JSON, creating its folder if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
