"""Serve the recorded ingest runs, so the app can show an ingest without keys.

The saved runs live in `data/recordings/ingest`. `mount` adds two read-only endpoints to the
app: one lists the recorded files and one returns the events of a single run. Nothing
here writes anything.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from app.config import ROOT

RECORDED_DIR = ROOT / "data" / "recordings" / "ingest"
NOT_RECORDED = "This ingest was not recorded."
UNREADABLE = "This recording could not be read."


def _rows(directory: Path) -> list[dict[str, Any]]:
    """List the recorded files that have a saved stream, in order."""
    try:
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    files = report.get("files") if isinstance(report, dict) else None
    rows = [
        row
        for row in files or []
        if isinstance(row, dict)
        and isinstance(row.get("number"), int)
        and (directory / f"{row['number']}.jsonl").is_file()
    ]
    return sorted(rows, key=lambda row: row["number"])


def mount(app: FastAPI, directory: Path = RECORDED_DIR) -> None:
    """Add the endpoints that list and play recorded runs."""
    directory = Path(directory)

    @app.get("/api/ingest/recorded")
    def recorded_list() -> list[dict[str, Any]]:
        return _rows(directory)

    @app.get("/api/ingest/recorded/{number}")
    def recorded_run(number: int) -> dict[str, Any]:
        row = next((r for r in _rows(directory) if r["number"] == number), None)
        if row is None:
            raise HTTPException(404, NOT_RECORDED)
        try:
            text = (directory / f"{number}.jsonl").read_text(encoding="utf-8")
            events = [json.loads(line) for line in text.splitlines() if line.strip()]
        except (OSError, ValueError):
            raise HTTPException(500, UNREADABLE) from None
        return {"number": number, "file": row.get("file"), "events": events}
