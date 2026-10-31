"""Write the saved API responses that the web interface is built against.

Run it with `uv run python scripts/api_fixtures.py`. It starts the app on the committed
data without paying for anything or changing any committed file, saves the answer of
each GET endpoint as a JSON file, and saves the reply to each suggested question as a
line-by-line transcript. Two runs write the same bytes, because the clock values are
replaced by made-up ones and the two parallel steps are always written in the same order.

`write_fixtures` writes the files. `normalized` makes a transcript repeatable.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import suggestions
from app.api.app import create_app
from app.config import KG_PATH, ROOT, Settings

OUT = ROOT / "data" / "recordings" / "api"
CLOCK_START = datetime(2026, 1, 1, tzinfo=UTC)
PARALLEL = {"graph_agent": 0, "manual_agent": 1}  # the order they are written in
GET_ENDPOINTS = {
    "/api/health": "health.json",
    "/api/ontology": "ontology.json",
    "/api/graph": "graph.json",
    "/api/activity": "activity.json",
    "/api/pipeline/latest": "pipeline_latest.json",
    "/api/evals/latest": "evals_latest.json",
    "/api/map": "map.json",
}


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _get(client: TestClient, url: str):
    """Return the JSON of a GET, or None if the file does not exist yet."""
    response = client.get(url)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _branch(item: dict) -> int | None:
    """Say which parallel branch an event belongs to, or None if neither."""
    if item["event"] == "tool_call":  # these come from the graph agent
        return PARALLEL["graph_agent"]
    if item["event"] in ("node_start", "node_end"):
        return PARALLEL.get(item["data"]["node"])
    return None


def _stamp(step: int) -> str:
    return (CLOCK_START + timedelta(seconds=step)).isoformat().replace("+00:00", "Z")


def normalized(events: list[dict]) -> list[dict]:
    """Put the parallel branches in a fixed order and swap in made-up times."""
    events = [json.loads(json.dumps(item)) for item in events]
    slots = [i for i, item in enumerate(events) if _branch(item) is not None]
    for slot, item in zip(slots, sorted((events[i] for i in slots), key=_branch)):
        events[slot] = item
    starts: dict[str, int] = {}
    times: dict[str, dict] = {}
    for step, item in enumerate(events):
        data = item["data"]
        if item["event"] == "node_start":
            starts[data["node"]] = step
            data["at"] = _stamp(step)
        elif item["event"] == "node_end":
            began = starts[data["node"]]
            data["started_at"], data["ended_at"] = _stamp(began), _stamp(step)
            data["seconds"] = float(step - began)
            keys = ("started_at", "ended_at", "seconds")
            times[data["node"]] = {k: data[k] for k in keys}
    for item in events:
        if item["event"] == "answer":
            for entry in item["data"]["trace"]:
                entry.update(times[entry["name"]])
            item["data"]["trace"].sort(key=lambda entry: entry["started_at"])
    return events


def _transcript(client: TestClient, question: str) -> list[dict]:
    """Ask one question and return its events in a repeatable form."""
    response = client.post("/api/ask", json={"question": question, "history": []})
    response.raise_for_status()
    events = []
    for block in response.text.split("\n\n"):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data.append(line[len("data: ") :])
        if name:
            events.append({"event": name, "data": json.loads("\n".join(data))})
    return normalized(events)


def write_fixtures(client: TestClient, out: Path = OUT) -> list[Path]:
    """Write every fixture file under out and return their paths."""
    written = []
    for url, name in GET_ENDPOINTS.items():
        data = _get(client, url)
        if data is not None:
            written.append(_write(out / name, data))
    sources = _get(client, "/api/sources")
    written.append(_write(out / "sources.json", sources))
    for source in sources:
        written.append(
            _write(
                out / "sources" / f"{source['id']}.json",
                _get(client, f"/api/sources/{source['id']}"),
            )
        )
    found = _get(client, "/api/suggestions")
    written.append(_write(out / "suggestions.json", found))
    for number, suggestion in enumerate(found, start=1):
        if suggestion["replayable"]:
            path = out / f"ask_{number}.jsonl"
            events = _transcript(client, suggestion["question"])
            path.write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                encoding="utf-8",
            )
            written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="write the API fixtures")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    with suggestions.scratch_state():
        with TestClient(create_app(Settings(), KG_PATH)) as client:
            written = write_fixtures(client, args.out)
    for path in sorted(written):
        print(f"{path.relative_to(args.out)}  {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
