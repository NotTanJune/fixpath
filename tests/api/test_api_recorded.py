"""Tests of the read-only API for recorded ingest runs: the list and one stream.

Both work in replay mode. They read only the recorded runs and write nothing.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import recorded
from app.config import ROOT, Settings

REAL = ROOT / "data" / "recordings" / "ingest"


def _events(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (REAL / name).read_text(encoding="utf-8").splitlines()
        if line
    ]


def _client(directory) -> TestClient:
    app = FastAPI()
    recorded.mount(app, directory)
    return TestClient(app)


@pytest.fixture
def folder(tmp_path):
    rows = [
        {"number": 1, "file": "one.txt", "status": "ok", "doc_type": "handover"},
        {"number": 2, "file": "two.bin", "status": "refused", "doc_type": None},
        {"number": 3, "file": "three.txt", "status": "ok", "doc_type": "log"},
    ]
    (tmp_path / "report.json").write_text(
        json.dumps({"live": True, "totals": {}, "files": rows}), encoding="utf-8"
    )
    stream = [
        {"event": "step", "data": {"node": "prepare"}, "at": "2026-01-01T00:00:00Z"}
    ]
    (tmp_path / "1.jsonl").write_text(json.dumps(stream[0]) + "\n", encoding="utf-8")
    (tmp_path / "2.jsonl").write_text(
        json.dumps({"event": "refused", "data": {"status": 415}, "at": "x"}) + "\n",
        encoding="utf-8",
    )
    # run 3 has a row but no stream
    return tmp_path


def test_the_list_gives_each_file_with_its_report_row(folder):
    response = _client(folder).get("/api/ingest/recorded")
    assert response.status_code == 200
    rows = response.json()
    assert [row["number"] for row in rows] == [1, 2]
    assert rows[0] == {
        "number": 1,
        "file": "one.txt",
        "status": "ok",
        "doc_type": "handover",
    }
    assert rows[1]["status"] == "refused"


def test_the_list_is_empty_when_there_are_no_recordings(tmp_path):
    assert _client(tmp_path).get("/api/ingest/recorded").json() == []


def test_one_stream_comes_back_as_its_events_in_order(folder):
    response = _client(folder).get("/api/ingest/recorded/1")
    assert response.status_code == 200
    assert response.json() == {
        "number": 1,
        "file": "one.txt",
        "events": [
            {"event": "step", "data": {"node": "prepare"}, "at": "2026-01-01T00:00:00Z"}
        ],
    }


@pytest.mark.parametrize("number", ["3", "99", "0", "-1"])
def test_a_stream_that_is_not_recorded_is_a_404(folder, number):
    response = _client(folder).get(f"/api/ingest/recorded/{number}")
    assert response.status_code == 404
    assert "recorded" in response.json()["detail"]


def test_a_number_that_is_not_a_number_is_refused_and_reads_no_path(folder):
    assert _client(folder).get("/api/ingest/recorded/..%2Freport").status_code in (
        404,
        422,
    )
    assert _client(folder).get("/api/ingest/recorded/x").status_code == 422


def test_it_only_reads(folder):
    before = sorted((p.name, p.read_bytes()) for p in folder.iterdir())
    client = _client(folder)
    client.get("/api/ingest/recorded")
    client.get("/api/ingest/recorded/1")
    assert sorted((p.name, p.read_bytes()) for p in folder.iterdir()) == before
    assert client.post("/api/ingest/recorded").status_code == 405
    assert client.delete("/api/ingest/recorded/1").status_code == 405


def test_the_committed_recordings_are_served_in_replay_mode():
    from app.api.app import create_app

    # no startup step, since there is nothing to warm up
    client = TestClient(create_app(Settings(), replay_check=lambda question: False))
    assert client.get("/api/health").json()["replay_mode"] is True
    rows = client.get("/api/ingest/recorded").json()
    assert [row["number"] for row in rows] == list(range(1, 11))
    refused = client.get("/api/ingest/recorded/10").json()
    assert refused["events"] == _events("10.jsonl")
    assert refused["file"] == rows[9]["file"]
    one = client.get("/api/ingest/recorded/1").json()
    assert [e["event"] for e in one["events"]] == [
        e["event"] for e in _events("1.jsonl")
    ]
    assert one["events"][-1]["event"] == "done"
