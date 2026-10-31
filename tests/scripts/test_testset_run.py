"""Tests for scripts/testset_run.py. The dry run makes no paid call. The live run
refuses to start without a spending cap. The report and the saved streams have the
form the web replay needs. The live run itself is not started here, only its checks."""

import hashlib
import importlib.util
import json
import sys

import pytest
from api_stubs import build_api_data
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app import ledger, llm_cache
from app.config import DATA_DIR, ROOT, Settings
from app.decide import base as decide_base
from app.ingest.normalize import document_id

SCRIPT = ROOT / "scripts" / "testset_run.py"
TESTSET = ROOT / "data" / "testset"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("testset_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["testset_run"] = module
    spec.loader.exec_module(module)
    return module


def sha(path):
    """Hash a file, or return None when it does not exist (the ledger is local only)."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


# --- the guards of --live -----------------------------------------------------------


def never_load(*args, **kwargs):
    raise AssertionError("the settings (and so .env) must not be loaded")


def test_live_refuses_to_start_without_budget_usd(run, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BUDGET_USD", raising=False)
    monkeypatch.setattr(run, "load_settings", never_load)
    assert run.main(["--live", "--out", str(tmp_path / "out")]) == 2
    assert "BUDGET_USD" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("value", ["", "abc", "nan", "-1", "0"])
def test_live_refuses_a_budget_that_is_not_a_positive_number(
    run, monkeypatch, tmp_path, value
):
    monkeypatch.setenv("BUDGET_USD", value)
    monkeypatch.setattr(run, "load_settings", never_load)
    assert run.main(["--live", "--out", str(tmp_path / "out")]) == 2


def test_live_refuses_more_than_30_cents_above_the_ledger_total(
    run, monkeypatch, tmp_path, capsys
):
    spent = run.ledger_total(DATA_DIR)
    monkeypatch.setenv("BUDGET_USD", f"{spent + 0.31:.4f}")
    monkeypatch.setattr(run, "load_settings", never_load)
    assert run.main(["--live", "--out", str(tmp_path / "out")]) == 2
    assert "0.30" in capsys.readouterr().err


def test_live_refuses_a_budget_below_what_is_already_spent(run, monkeypatch, tmp_path):
    spent = 1.0
    monkeypatch.setattr(run, "ledger_total", lambda data_dir: spent)
    monkeypatch.setenv("BUDGET_USD", f"{max(spent - 0.5, 0.01):.4f}")
    monkeypatch.setattr(run, "load_settings", never_load)
    assert run.main(["--live", "--out", str(tmp_path / "out")]) == 2


def test_live_with_a_good_budget_still_needs_the_openai_key(
    run, monkeypatch, tmp_path, capsys
):
    spent = run.ledger_total(DATA_DIR)
    monkeypatch.setenv("BUDGET_USD", f"{spent + 0.30:.6f}")
    monkeypatch.setattr(run, "load_settings", lambda: Settings(budget_usd=spent + 0.30))
    called = []
    monkeypatch.setattr(run, "run_testset", lambda *a, **k: called.append(1))
    assert run.main(["--live", "--out", str(tmp_path / "out")]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err and not called


def test_live_with_a_good_budget_and_a_key_runs_with_those_settings(
    run, monkeypatch, tmp_path
):
    spent = run.ledger_total(DATA_DIR)
    budget = spent + 0.30
    monkeypatch.setenv("BUDGET_USD", f"{budget:.6f}")
    keyed = Settings(openai_api_key="sk-test-not-real", budget_usd=budget)
    monkeypatch.setattr(run, "load_settings", lambda: keyed)
    seen = {}

    def fake_run(testset_dir, data_dir, out, *, live, settings):
        seen.update(live=live, settings=settings, out=out)
        return {"files": [], "totals": {"usd": 0, "ok": 0, "files": 0}}

    monkeypatch.setattr(run, "run_testset", fake_run)
    assert run.main(["--live", "--out", str(tmp_path / "out")]) == 0
    assert seen == {"live": True, "settings": keyed, "out": tmp_path / "out"}


# --- the stream fixtures --------------------------------------------------------------


def test_a_stream_is_normalized_to_one_second_a_step(run):
    events = [
        {"event": "step", "data": {"node": "prepare"}},
        {"event": "extracted", "data": {"doc_path": "x"}},
        {
            "event": "done",
            "data": {
                "report": {
                    "run": {"ran_at": "2026-10-29T10:11:12+00:00", "seconds": 7.77}
                }
            },
        },
    ]
    kept = json.loads(json.dumps(events))
    lines = run.normalized(events)
    assert events == kept  # the input is not changed
    assert [line["at"] for line in lines] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:01Z",
        "2026-01-01T00:00:02Z",
    ]
    assert [line["event"] for line in lines] == ["step", "extracted", "done"]
    assert lines[1]["data"] == {"doc_path": "x"}
    assert lines[2]["data"]["report"]["run"] == {
        "ran_at": "2026-01-01T00:00:02Z",
        "seconds": 2.0,
    }


def test_a_refusal_is_recorded_as_one_refused_event(run):
    lines = run.normalized(run.refused_stream(415, "no"))
    assert lines == [
        {
            "event": "refused",
            "data": {"status": 415, "detail": "no"},
            "at": "2026-01-01T00:00:00Z",
        }
    ]


def test_the_status_of_a_file_comes_from_its_events_and_http_status(run):
    ok = [{"event": "done", "data": {}}]
    keys = [{"event": "error", "data": {"message": "This file needs keys: x"}}]
    budget = [
        {"event": "error", "data": {"message": "The model budget is used up: x."}}
    ]
    other = [{"event": "error", "data": {"message": "Something went wrong"}}]
    assert run.status_of(200, ok) == "ok"
    assert run.status_of(200, keys) == "needs_keys"
    assert run.status_of(200, budget) == "budget"
    assert run.status_of(200, other) == "failed"
    assert run.status_of(415, []) == "refused"
    assert run.status_of(200, []) == "failed"


def test_the_expected_ids_found_are_those_with_evidence_from_the_file(run, tmp_path):
    kg = build_api_data(tmp_path)
    found = run.found_ids(kg, "logs/hp.log", {"HP-01", "E-217", "NOPE-1"})
    assert found <= {"HP-01", "E-217"}
    assert "NOPE-1" not in found
    assert run.found_ids(kg, "logs/not-loaded.log", {"HP-01"}) == set()


# --- the dry run ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def dry(run, tmp_path_factory):
    out = tmp_path_factory.mktemp("dry")
    committed = [
        DATA_DIR / "kg.sqlite",
        DATA_DIR / "cache" / "llm_cache.sqlite",
        DATA_DIR / "cache" / "decisions.sqlite",
        DATA_DIR / "cache" / "ledger.sqlite",
        DATA_DIR / "pipeline" / "latest.json",
        DATA_DIR / "map" / "fact_map.json",
        DATA_DIR / "map" / "vectors.npy",
    ]
    before = {p: sha(p) for p in committed}
    paths = (llm_cache.CACHE_PATH, ledger.LEDGER_PATH, decide_base.CACHE_PATH)
    report = run.run_testset(TESTSET, DATA_DIR, out, live=False, settings=Settings())
    after = (llm_cache.CACHE_PATH, ledger.LEDGER_PATH, decide_base.CACHE_PATH)
    return out, report, before, (paths, after), committed


def test_the_dry_run_leaves_the_committed_data_and_the_module_paths_alone(dry):
    out, report, before, paths, committed = dry
    assert {p: sha(p) for p in committed} == before
    assert paths[0] == paths[1]


def test_the_dry_run_pays_for_nothing(dry):
    out, report, *_ = dry
    assert all(row["usd"] == 0 for row in report["files"])
    assert report["totals"]["usd"] == 0 and report["live"] is False


def test_the_report_has_one_row_per_ingest_file_with_the_planned_fields(dry):
    out, report, *_ = dry
    saved = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert saved == json.loads(json.dumps(report))
    names = sorted(p.name for p in (TESTSET / "ingest").iterdir())
    assert [row["file"] for row in report["files"]] == names
    for number, row in enumerate(report["files"], start=1):
        assert set(row) == {
            "number",
            "file",
            "status",
            "http",
            "detail",
            "doc_type",
            "converted_from",
            "chunks",
            "facts",
            "matched",
            "new",
            "found",
            "missed",
            "seconds",
            "usd",
        }
        assert row["number"] == number
        assert isinstance(row["found"], list) and isinstance(row["missed"], list)
        assert isinstance(row["seconds"], float) and row["usd"] == 0


def test_the_binary_is_refused_and_every_other_file_stops_at_its_first_miss(dry):
    out, report, *_ = dry
    rows = {row["file"]: row for row in report["files"]}
    binary = rows["10_unknown_firmware.bin"]
    assert (binary["status"], binary["http"]) == ("refused", 415)
    assert (
        binary["detail"]
        == "This file is not text, a PDF or a workbook, so it cannot be read."
    )
    for name, row in rows.items():
        if name != "10_unknown_firmware.bin":
            assert row["status"] in ("needs_keys", "ok"), (name, row)
            assert row["http"] == 200


def test_the_expected_ids_of_a_file_that_did_not_load_are_all_missed(dry):
    out, report, *_ = dry
    expected = json.loads((TESTSET / "expected.json").read_text(encoding="utf-8"))
    for row in report["files"]:
        if row["status"] != "ok":
            assert (
                row["found"] == [] and row["missed"] == expected[row["file"]]["expect"]
            )


def test_the_converted_and_native_files_are_told_apart(dry):
    out, report, *_ = dry
    came = {
        row["file"].rsplit(".", 1)[1]: row["converted_from"] for row in report["files"]
    }
    assert came["txt"] is None and came["csv"] is None and came["pdf"] is None
    assert came["docx"] == ".docx" and came["md"] == ".md" and came["html"] == ".html"


def test_each_file_has_a_normalized_stream(dry):
    out, report, *_ = dry
    for row in report["files"]:
        lines = [
            json.loads(line)
            for line in (out / f"{row['number']}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert lines and all(set(line) == {"event", "data", "at"} for line in lines)
        assert [line["at"] for line in lines] == [
            f"2026-01-01T00:00:{i:02d}Z" for i in range(len(lines))
        ]
    first = (out / "10.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(first[0])["event"] == "refused"


def test_the_dry_run_is_repeatable_apart_from_the_clock(run, tmp_path):
    again = run.run_testset(
        TESTSET, DATA_DIR, tmp_path, live=False, settings=Settings()
    )
    first = [{k: v for k, v in r.items() if k != "seconds"} for r in again["files"]]
    second = run.run_testset(
        TESTSET, DATA_DIR, tmp_path / "b", live=False, settings=Settings()
    )
    assert first == [
        {k: v for k, v in r.items() if k != "seconds"} for r in second["files"]
    ]
    assert (tmp_path / "1.jsonl").read_bytes() == (
        tmp_path / "b" / "1.jsonl"
    ).read_bytes()


def test_found_ids_only_look_at_the_evidence_of_the_document(run, tmp_path):
    kg = build_api_data(tmp_path)
    assert document_id("logs/hp.log").startswith("DOC-")
    assert run.found_ids(kg, "logs/hp.log", set()) == set()


def test_the_spend_of_a_live_run_is_carried_into_the_real_ledger(run, tmp_path):
    """A live run books its calls in the scratch ledger; the rows it booked are added to
    the real one afterwards, so the spend the app shows stays true."""
    import sqlite3
    from contextlib import closing

    real = tmp_path / "ledger.sqlite"
    with closing(sqlite3.connect(real)) as conn, conn:
        conn.executescript(ledger._SCHEMA)
        conn.execute(
            "INSERT INTO calls(ts, model, purpose, input_tokens, cached_input_tokens,"
            " output_tokens, usd, cache_hit) VALUES ('t0', 'm', 'old', 1, 0, 1, 0.5, 0)"
        )
    booked = [
        {
            "id": 7,
            "ts": "t1",
            "model": "gpt-6.1-sol",
            "purpose": "extract note",
            "input_tokens": 10,
            "cached_input_tokens": 0,
            "output_tokens": 5,
            "usd": 0.003,
            "cache_hit": 0,
        },
        {
            "id": 8,
            "ts": "t2",
            "model": "jev-1.14.0",
            "purpose": "merge Part",
            "input_tokens": 3,
            "cached_input_tokens": 0,
            "output_tokens": 1,
            "usd": 0.00003,
            "cache_hit": 0,
        },
    ]
    assert run.carry_spend(booked, real) == 2
    with closing(sqlite3.connect(real)) as conn:
        rows = conn.execute("SELECT purpose, usd FROM calls ORDER BY id").fetchall()
    assert rows == [("old", 0.5), ("extract note", 0.003), ("merge Part", 0.00003)]
    assert run.carry_spend([], real) == 0
