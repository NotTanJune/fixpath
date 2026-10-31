"""Tests of the script that saves the read-only endpoints and the recorded question
transcripts as files.
"""

import importlib.util
import json

import pytest
from api_stubs import QUESTION, make_app, scripted_crew
from fastapi.testclient import TestClient
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app import ledger, llm_cache
from app.api import suggestions
from app.config import ROOT

SCRIPT = ROOT / "scripts" / "api_fixtures.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("api_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_writes_one_json_file_per_get_endpoint_and_one_per_source(
    script, tmp_path, monkeypatch
):
    app = make_app(tmp_path / "data_root", replay_check=lambda question: False)
    (tmp_path / "data_root" / "evals" / "results").mkdir(parents=True)
    (tmp_path / "data_root" / "evals" / "results" / "latest.json").write_text(
        '{"layer1": 1}'
    )
    out = tmp_path / "out"
    written = script.write_fixtures(TestClient(app), out)
    names = sorted(str(path.relative_to(out)) for path in written)
    assert names[:7] == [
        "activity.json",
        "evals_latest.json",
        "graph.json",
        "health.json",
        "ontology.json",
        "pipeline_latest.json",
        "sources.json",
    ]
    assert "suggestions.json" in names
    assert (
        "sources/DOC-aaaaaaaa.json" in names
        and len([n for n in names if n.startswith("sources/")]) == 6
    )
    assert json.loads((out / "health.json").read_text())["replay_mode"] is True
    assert set(json.loads((out / "activity.json").read_text())) == {
        "breakdowns",
        "alarms",
    }
    assert (
        json.loads((out / "sources" / "DOC-dddddddd.json").read_text())["kind"]
        == "text"
    )
    assert not list(out.glob("ask_*.jsonl"))  # nothing can be replayed here


def test_it_writes_the_fact_map_when_there_is_one_and_skips_it_otherwise(
    script, tmp_path
):
    out = tmp_path / "out"
    script.write_fixtures(TestClient(make_app(tmp_path / "d")), out)
    assert not (out / "map.json").exists()
    path = tmp_path / "d" / "data" / "map" / "fact_map.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"points": []}')
    script.write_fixtures(TestClient(make_app(tmp_path / "d")), out)
    assert json.loads((out / "map.json").read_text()) == {"points": []}


def test_it_skips_the_evals_file_when_there_are_no_results(script, tmp_path):
    out = tmp_path / "out"
    script.write_fixtures(TestClient(make_app(tmp_path / "d")), out)
    assert not (out / "evals_latest.json").exists()


def test_it_records_a_jsonl_transcript_for_each_replayable_question(
    script, tmp_path, monkeypatch
):
    scripted_crew(monkeypatch)
    app = make_app(tmp_path / "d", replay_check=lambda question: question == QUESTION)
    out = tmp_path / "out"
    script.write_fixtures(TestClient(app), out)
    assert [p.name for p in sorted(out.glob("ask_*.jsonl"))] == ["ask_1.jsonl"]
    lines = [
        json.loads(line) for line in (out / "ask_1.jsonl").read_text().splitlines()
    ]
    assert [line["event"] for line in lines][-3:] == ["answer", "graph_path", "done"]
    assert lines[0]["event"] == "node_start" and "node" in lines[0]["data"]
    assert json.loads((out / "suggestions.json").read_text())[0] == {
        "question": suggestions.QUESTIONS[0],
        "replayable": True,
    }


# --- the same bytes every time ---


def write_transcript(script, tmp_path, monkeypatch, name):
    scripted_crew(monkeypatch)
    app = make_app(tmp_path / name, replay_check=lambda question: question == QUESTION)
    out = tmp_path / f"out_{name}"
    script.write_fixtures(TestClient(app), out)
    return (out / "ask_1.jsonl").read_bytes()


def test_two_runs_write_the_same_transcript_bytes(script, tmp_path, monkeypatch):
    first = write_transcript(script, tmp_path, monkeypatch, "one")
    llm_cache.CACHE_PATH.unlink()  # the second run starts from the same caches
    ledger.LEDGER_PATH.unlink()
    second = write_transcript(script, tmp_path, monkeypatch, "two")
    assert first == second


def test_the_clocks_of_a_transcript_are_made_up_and_agree_with_each_other(
    script, tmp_path, monkeypatch
):
    write_transcript(script, tmp_path, monkeypatch, "clock")
    lines = (tmp_path / "out_clock" / "ask_1.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    starts = [e["data"]["at"] for e in events if e["event"] == "node_start"]
    assert all(at.startswith("2026-01-01T00:00:") for at in starts)
    ends = {e["data"]["node"]: e["data"] for e in events if e["event"] == "node_end"}
    assert all(d["started_at"] < d["ended_at"] for d in ends.values())
    assert all(d["seconds"] > 0 for d in ends.values())
    card = next(e["data"] for e in events if e["event"] == "answer")
    for entry in card["trace"]:
        keys = ("started_at", "ended_at", "seconds")
        timing = {k: ends[entry["name"]][k] for k in keys}
        assert {k: entry[k] for k in timing} == timing
    traced = [entry["started_at"] for entry in card["trace"]]
    assert traced == sorted(traced)


def event(kind, **data):
    return {"event": kind, "data": data}


def test_the_parallel_branches_are_written_graph_agent_first(script):
    times = dict(started_at="x", ended_at="y", seconds=1.5)
    graph = [
        event("node_start", node="graph_agent", at="x"),
        event("node_end", node="graph_agent", **times),
        event("tool_call", name="alarm_causes"),
    ]
    manual = [
        event("node_start", node="manual_agent", at="x"),
        event("node_end", node="manual_agent", **times),
    ]
    before = [event("node_start", node="triage", at="x")]
    after = [event("node_start", node="synthesize", at="x")]
    interleaved = before + [manual[0], graph[0], manual[1], graph[1], graph[2]] + after
    sequential = before + graph + manual + after
    assert script.normalized(interleaved) == script.normalized(sequential)
    shown = script.normalized(interleaved)
    names = [(e["event"], e["data"].get("node")) for e in shown]
    assert names == [
        ("node_start", "triage"),
        ("node_start", "graph_agent"),
        ("node_end", "graph_agent"),
        ("tool_call", None),
        ("node_start", "manual_agent"),
        ("node_end", "manual_agent"),
        ("node_start", "synthesize"),
    ]
