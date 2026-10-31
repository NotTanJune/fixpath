"""Tests for the ingest command line. A recorder replaces the real graph."""

import pytest

from app.config import KG_PATH, RAW_DIR, Settings
from app.decide.base import NoDecisionBackend
from app.ingest import __main__ as cli
from app.ingest.router import UnknownFile
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey, NoCacheInstalled
from llm_stubs import isolated  # noqa: F401

REPORT = {
    "files": [{"doc_path": "a"}, {"doc_path": "b"}],
    "run": {"mode": "full", "seconds": 1.5},
    "totals": {
        "nodes": {"Asset": 5, "Incident": 61},
        "edges": {"RAISED": 3},
        "review": {"conflict": 2},
        "flags": {"error": 1, "warning": 18, "by_rule": {}},
        "cost": {"calls": 4, "cache_hits": 4, "paid_calls": 0, "usd": 0.0},
    },
}


@pytest.fixture
def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(
        cli.graph,
        "run_full",
        lambda *a, **k: (calls.append(("full", a)), {"report": REPORT})[1],
    )
    monkeypatch.setattr(
        cli.graph,
        "run_file",
        lambda *a, **k: (calls.append(("file", a)), {"report": REPORT})[1],
    )
    return calls


def test_the_summary_lists_counts_flags_and_the_cost():
    assert cli.summary(REPORT) == [
        "mode: full, 2 files, 1.5 s",
        "nodes: Asset 5, Incident 61",
        "edges: RAISED 3",
        "review items: conflict 2",
        "flags: 1 errors, 18 warnings",
        "model calls: 4 (4 from the cache, 0 paid), cost $0.0000",
    ]


def test_no_arguments_runs_the_full_ingestion(recorded, capsys):
    assert cli.main([]) == 0
    assert recorded == [("full", (RAW_DIR, KG_PATH, Settings()))]
    assert "nodes: Asset 5, Incident 61" in capsys.readouterr().out


def test_file_runs_one_file_into_the_existing_graph(recorded, capsys, tmp_path):
    target = tmp_path / "note.txt"
    assert cli.main(["--file", str(target)]) == 0
    assert recorded == [("file", (target, KG_PATH, Settings()))]
    assert "wall time" in capsys.readouterr().out


def test_a_missing_database_is_reported_and_exits_with_1(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())

    def refuse(*args, **kwargs):
        raise FileNotFoundError("there is no graph database; run a full ingest first")

    monkeypatch.setattr(cli.graph, "run_file", refuse)
    assert cli.main(["--file", "x.txt"]) == 1
    assert "full ingest" in capsys.readouterr().err


# --- one-line errors --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "mode", "words"),
    [
        (
            MissingApiKey(
                "No cached answer for this request and OPENAI_API_KEY is not set."
            ),
            "full",
            "OPENAI_API_KEY",
        ),
        (
            MissingApiKey(
                "No cached answer for this request and OPENAI_API_KEY is not set."
            ),
            "file",
            "OPENAI_API_KEY",
        ),
        (NoDecisionBackend("no cached decision and no backend"), "full", "decision"),
        (BudgetExceeded("the budget is spent"), "full", "budget"),
        (NoCacheInstalled(), "full", "No LLM cache is installed"),
        (NoCacheInstalled(), "file", "enable_cache"),
        (FileNotFoundError("x.txt is not a file"), "file", "x.txt"),
        (
            UnknownFile("cannot tell what kind of file photo.png is"),
            "file",
            "photo.png",
        ),
    ],
)
def test_a_known_failure_prints_one_line_to_stderr_and_exits_with_1(
    monkeypatch, capsys, error, mode, words
):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(cli.graph, "run_full", refuse)
    monkeypatch.setattr(cli.graph, "run_file", refuse)
    argv = [] if mode == "full" else ["--file", "x.txt"]
    assert cli.main(argv) == 1
    out, err = capsys.readouterr()
    lines = err.strip().splitlines()
    assert len(lines) == 1
    assert words in lines[0]
    assert "Traceback" not in err and out == ""


def test_the_missing_key_line_says_what_to_do(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())
    monkeypatch.setattr(
        cli.graph, "run_full", lambda *a, **k: (_ for _ in ()).throw(MissingApiKey("x"))
    )
    assert cli.main([]) == 1
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY is not set" in err and ".env" in err and "run again" in err


def test_an_unlisted_exception_keeps_its_traceback(monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())

    def explode(*args, **kwargs):
        raise RuntimeError("a real bug")

    monkeypatch.setattr(cli.graph, "run_full", explode)
    with pytest.raises(RuntimeError, match="a real bug"):
        cli.main([])


def test_another_value_error_keeps_its_traceback(monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: Settings())

    def explode(*args, **kwargs):
        raise ValueError("a real bug")

    monkeypatch.setattr(cli.graph, "run_full", explode)
    with pytest.raises(ValueError, match="a real bug"):
        cli.main([])
