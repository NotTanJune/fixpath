"""Run the test set through the real API and record what happens.

Each file in the test set is uploaded to the app in order, against scratch copies of
the data, so no committed file changes. The script writes one event stream for each
file and a report of how each file went. The default dry run has no keys and pays
nothing; it only replays saved answers. The `--live` run pays for new answers, within
a budget that must be set first.

`run_testset` does the whole run. `scratch_copy` makes the temporary copies of the data.
`carry_spend` adds the cost of a live run to the real ledger.
"""

import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from langchain_core.globals import get_llm_cache, set_llm_cache

from app import ledger, llm_cache
from app.api import uploads
from app.api.app import create_app
from app.config import DATA_DIR, ROOT, Settings, load_settings
from app.decide import base as decide_base
from app.ingest import router
from app.ingest.normalize import document_id
from app.ingest.report import ledger_mark, ledger_rows_since

TESTSET_DIR = ROOT / "data" / "testset"
FIXTURES_DIR = ROOT / "data" / "recordings" / "ingest"
HEADROOM_USD = 0.30
CLOCK_START = datetime(2026, 1, 1, tzinfo=UTC)
BINARY_TYPE = "application/octet-stream"


# --- the scratch copies -------------------------------------------------------------


def _copy_database(source: Path, target: Path) -> None:
    """Copy a SQLite file safely. Do nothing if the file is missing."""
    if Path(source).is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(f"{Path(source).resolve().as_uri()}?mode=ro", uri=True)
        ) as old:
            with closing(sqlite3.connect(target)) as new:
                old.backup(new)


@dataclass(frozen=True)
class Scratch:
    root: Path

    @property
    def kg(self) -> Path:
        return self.root / "kg.sqlite"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def map(self) -> Path:
        return self.root / "map" / "fact_map.json"


@contextmanager
def scratch_copy(data_dir: Path) -> Iterator[Scratch]:
    """Copy the data to a temporary folder and point the caches and ledger at the copies.

    The original paths come back when the block ends."""
    data_dir = Path(data_dir)
    saved = (
        llm_cache.CACHE_PATH,
        ledger.LEDGER_PATH,
        decide_base.CACHE_PATH,
        get_llm_cache(),
    )
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder) / "data"
        root.mkdir()
        _copy_database(data_dir / "kg.sqlite", root / "kg.sqlite")
        for name in ("raw", "map", "pipeline"):
            if (data_dir / name).is_dir():
                shutil.copytree(data_dir / name, root / name)
        cache = root / "cache"
        for name in ("llm_cache.sqlite", "decisions.sqlite", "ledger.sqlite"):
            _copy_database(data_dir / "cache" / name, cache / name)
        llm_cache.CACHE_PATH = cache / "llm_cache.sqlite"
        decide_base.CACHE_PATH = cache / "decisions.sqlite"
        ledger.LEDGER_PATH = cache / "ledger.sqlite"
        try:
            yield Scratch(root)
        finally:
            llm_cache.CACHE_PATH, ledger.LEDGER_PATH, decide_base.CACHE_PATH = saved[:3]
            set_llm_cache(saved[3])


def ledger_total(data_dir: Path) -> float:
    """Read the total dollars in the ledger. Return 0 if there is none."""
    path = Path(data_dir) / "cache" / "ledger.sqlite"
    if not path.is_file():
        return 0.0
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    ) as conn:
        try:
            (total,) = conn.execute(
                "SELECT COALESCE(SUM(usd), 0.0) FROM calls"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0.0
    return float(total)


# --- reading a response ---------------------------------------------------------------


def parse_events(body: str) -> list[dict[str, Any]]:
    """Split an event-stream reply into its events."""
    events = []
    for block in body.split("\n\n"):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data.append(line[len("data: ") :])
        if name:
            events.append(
                {"event": name, "data": json.loads("\n".join(data)) if data else None}
            )
    return events


def refused_stream(status: int, detail: str) -> list[dict[str, Any]]:
    """Record a refused upload as a single event."""
    return [{"event": "refused", "data": {"status": status, "detail": detail}}]


def _stamp(step: int) -> str:
    return (CLOCK_START + timedelta(seconds=step)).isoformat().replace("+00:00", "Z")


def normalized(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give the events a made-up clock, one second a step, so runs look the same."""
    lines = []
    for step, item in enumerate(json.loads(json.dumps(events))):
        if item["event"] == "done":
            run = item["data"].get("report", {}).get("run")
            if isinstance(run, dict):
                if "ran_at" in run:
                    run["ran_at"] = _stamp(step)
                if "seconds" in run:
                    run["seconds"] = float(step)
        lines.append({"event": item["event"], "data": item["data"], "at": _stamp(step)})
    return lines


def status_of(http: int, events: list[dict[str, Any]]) -> str:
    """Say how a file went: ok, refused, needs_keys, budget or failed."""
    if http != 200:
        return "refused"
    names = [item["event"] for item in events]
    if "error" in names:
        message = next(i["data"]["message"] for i in events if i["event"] == "error")
        if "needs keys" in message:
            return "needs_keys"
        if "budget" in message:
            return "budget"
        return "failed"
    return "ok" if "done" in names else "failed"


def found_ids(kg_path: Path, doc_path: str, expect: set[str]) -> set[str]:
    """Return the expected facts that the stored graph backs with this document."""
    with closing(
        sqlite3.connect(f"{Path(kg_path).resolve().as_uri()}?mode=ro", uri=True)
    ) as conn:
        rows = conn.execute(
            "SELECT fact_kind, fact_id FROM evidence WHERE doc_id = ?",
            (document_id(doc_path),),
        ).fetchall()
    seen: set[str] = set()
    for kind, fact_id in rows:
        if kind == "node":
            seen.add(fact_id)
        else:
            parts = fact_id.split("|")
            seen.update(parts[0:1] + parts[2:3])
    return set(expect) & seen


def routed_type(name: str, data: bytes) -> str | None:
    """Say which document type the upload gives a file, or None if it refuses it."""
    with tempfile.TemporaryDirectory() as folder:
        try:
            saved = uploads.save_upload(name, data, Path(folder))
        except uploads.Refused:
            return None
        return router.FOLDER_TYPES.get(saved.path.parent.name)


# --- the run --------------------------------------------------------------------------


def _event(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next((i["data"] for i in events if i["event"] == name), {}) or {}


def _row(number, path, expect, response, seconds, rows, kg) -> tuple[dict, list[dict]]:
    http = response.status_code
    if http == 200:
        events = parse_events(response.text)
        detail = None
    else:
        detail = response.json().get("detail", response.text)
        events = refused_stream(http, detail)
    status = status_of(http, events)
    if status != "ok" and http == 200:
        detail = _event(events, "error").get("message")
    extracted = _event(events, "extracted")
    matched = _event(events, "matched")
    facts = _event(events, "facts")
    doc_type = extracted.get("doc_type") or routed_type(path.name, path.read_bytes())
    found: set[str] = set()
    if status == "ok" and extracted.get("doc_path"):
        found = found_ids(kg, extracted["doc_path"], set(expect))
    row = {
        "number": number,
        "file": path.name,
        "status": status,
        "http": http,
        "detail": detail,
        "doc_type": doc_type,
        "converted_from": _event(events, "converted").get("from"),
        "chunks": len(_event(events, "chunked").get("chunks", [])),
        "facts": len(facts.get("nodes", [])) + len(facts.get("edges", [])),
        "matched": len(matched.get("merged", [])),
        "new": len(matched.get("new", [])),
        "found": sorted(found),
        "missed": sorted(set(expect) - found),
        "seconds": round(seconds, 2),
        "usd": round(sum(r["usd"] for r in rows), 6),
    }
    return row, events


_CARRIED = (
    "ts",
    "model",
    "purpose",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "usd",
    "cache_hit",
)


def carry_spend(rows: list[dict[str, Any]], ledger_path: Path) -> int:
    """Add the costs a live run booked in its scratch ledger to the real ledger.

    This keeps the spend shown in the app correct. Return the number of rows added."""
    if not rows:
        return 0
    with closing(sqlite3.connect(ledger_path, timeout=30)) as conn, conn:
        conn.executescript(ledger._SCHEMA)
        conn.executemany(
            f"INSERT INTO calls({', '.join(_CARRIED)})"
            f" VALUES ({', '.join('?' for _ in _CARRIED)})",
            [tuple(row[name] for name in _CARRIED) for row in rows],
        )
    return len(rows)


def run_testset(
    testset_dir: Path, data_dir: Path, out: Path, *, live: bool, settings: Settings
) -> dict[str, Any]:
    """Upload every test file to a scratch copy of the data and write the results."""
    testset_dir, out = Path(testset_dir), Path(out)
    expected = json.loads((testset_dir / "expected.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with scratch_copy(data_dir) as scratch:
        app = create_app(
            settings,
            scratch.kg,
            raw_dir=scratch.raw,
            map_path=scratch.map,
            replay_check=lambda question: False,
            ingest_enabled=True,
        )
        client = TestClient(app)
        before = ledger.spent_usd()
        start = ledger_mark()
        for number, name in enumerate(sorted(expected), start=1):
            path = testset_dir / "ingest" / name
            mark = ledger_mark()
            started = time.monotonic()
            response = client.post(
                "/api/ingest", files={"file": (name, path.read_bytes(), BINARY_TYPE)}
            )
            seconds = time.monotonic() - started
            row, events = _row(
                number,
                path,
                expected[name]["expect"],
                response,
                seconds,
                ledger_rows_since(mark),
                scratch.kg,
            )
            rows.append(row)
            lines = normalized(events)
            (out / f"{number}.jsonl").write_text(
                "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
                encoding="utf-8",
            )
        spent = ledger.spent_usd() - before
        booked = ledger_rows_since(start)
    # The scratch ledger is thrown away. A live run paid for real, so its costs go to
    # the real ledger.
    carried = carry_spend(booked, ledger.LEDGER_PATH) if live else 0
    report = {
        "live": live,
        "totals": {
            "files": len(rows),
            "ok": sum(r["status"] == "ok" for r in rows),
            "usd": round(spent, 6),
            "ledger_rows_carried": carried,
        },
        "files": rows,
    }
    (out / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


# --- the command line -----------------------------------------------------------------


def _refuse(message: str) -> int:
    print(f"refused to start: {message}", file=sys.stderr)
    return 2


def _live_budget(data_dir: Path) -> float | str:
    """Return the budget as a number, or the reason it cannot be used."""
    text = os.environ.get("BUDGET_USD", "").strip()
    if not text:
        return "--live needs BUDGET_USD in the environment (the ledger total plus 0.30)"
    try:
        budget = float(text)
    except ValueError:
        budget = math.nan
    if not (math.isfinite(budget) and budget > 0):
        return f"BUDGET_USD must be a finite number above 0; got {text!r}"
    spent = ledger_total(data_dir)
    if budget <= spent:
        return f"BUDGET_USD {budget:.4f} is not above the ledger total {spent:.4f}"
    if budget - spent > HEADROOM_USD + 1e-6:
        return (
            f"BUDGET_USD {budget:.4f} is more than {HEADROOM_USD:.2f} above the ledger "
            f"total {spent:.4f}"
        )
    return budget


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the test set through the API")
    parser.add_argument(
        "--live", action="store_true", help="paid run, capped by BUDGET_USD"
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.live:
        budget = _live_budget(DATA_DIR)
        if isinstance(budget, str):
            return _refuse(budget)
        settings = load_settings()
        if not settings.has_openai:
            return _refuse("--live needs OPENAI_API_KEY in .env or the environment")
        out = args.out or FIXTURES_DIR
    else:
        settings = Settings()
        out = args.out or Path(tempfile.mkdtemp(prefix="testset_dry_"))
    report = run_testset(TESTSET_DIR, DATA_DIR, out, live=args.live, settings=settings)
    print(f"{'live' if args.live else 'dry'} run, output in {out}")
    for row in report["files"]:
        print(
            f"{row['number']:>2} {row['file']}: {row['status']}"
            f" (http {row['http']}, {row['doc_type']}, chunks {row['chunks']},"
            f" facts {row['facts']}, matched {row['matched']}, new {row['new']},"
            f" found {len(row['found'])}, missed {len(row['missed'])},"
            f" {row['seconds']}s, ${row['usd']})"
        )
        if row["detail"]:
            print(f"   {row['detail']}")
    print(
        f"total ${report['totals']['usd']}, ok {report['totals']['ok']} of {report['totals']['files']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
