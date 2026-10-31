"""Tests for the evals command line: choosing layers, writing the results file and
reporting the cost of a run."""

import json
import os

import pytest
from eval_stubs import edge, node, write_kg, write_truth

from app import ledger
from app.decide.base import NoDecisionBackend
from app.evals import __main__ as entry
from app.evals.__main__ import cost_summary, parse_layers, run
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey, NoCacheInstalled


@pytest.fixture(autouse=True)
def throwaway_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")


def test_layers_are_a_comma_separated_list_of_the_numbers_this_task_has():
    assert parse_layers("1,2") == (1, 2)
    assert parse_layers("2") == (2,)
    assert parse_layers("2,1,2") == (1, 2)
    assert parse_layers("5,3,4,1,2") == (1, 2, 3, 4, 5)
    for bad in ("6", "1,x", "", "0"):
        with pytest.raises(ValueError):
            parse_layers(bad)


def test_the_cost_of_a_run_is_summed_from_its_ledger_rows():
    rows = [
        {
            "purpose": "eval merge Part",
            "usd": 0.00003,
            "cache_hit": 0,
            "input_tokens": 100,
            "output_tokens": 0,
        },
        {
            "purpose": "eval merge Part",
            "usd": 0.0,
            "cache_hit": 1,
            "input_tokens": 90,
            "output_tokens": 0,
        },
        {
            "purpose": "extract note",
            "usd": 0.0,
            "cache_hit": 1,
            "input_tokens": 500,
            "output_tokens": 40,
        },
    ]
    result = cost_summary(rows)
    assert (result["calls"], result["cache_hits"], result["paid_calls"]) == (3, 2, 1)
    assert result["usd"] == pytest.approx(0.00003)
    assert result["input_tokens"] == 690
    assert result["by_purpose"]["eval merge Part"] == {
        "calls": 2,
        "cache_hits": 1,
        "usd": pytest.approx(0.00003),
    }


def tiny_world(tmp_path):
    nodes = [node("HP-01", "Asset"), node("E-1", "AlarmCode", severity="alarm")]
    truth = write_truth(
        tmp_path / "truth", nodes, [edge("HP-01", "RAISED", "E-1", count=2)]
    )
    kg = write_kg(
        tmp_path / "kg.sqlite", nodes, [edge("HP-01", "RAISED", "E-1", count=2)]
    )
    return truth, kg


def test_layer_1_alone_writes_sorted_indented_json_with_the_cost(tmp_path):
    truth, kg = tiny_world(tmp_path)
    out = tmp_path / "results" / "latest.json"
    result = run((1,), truth_dir=truth, kg_path=kg, out_path=out)
    text = out.read_text(encoding="utf-8")
    assert json.loads(text) == json.loads(json.dumps(result))
    assert (
        text == json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert set(result) == {"layer1", "cost"}
    assert result["layer1"]["nodes"]["Asset"]["f1"] == 1.0
    assert result["cost"]["layer1"]["calls"] == 0


def test_a_run_keeps_the_sections_of_layers_it_did_not_run(tmp_path):
    truth, kg = tiny_world(tmp_path)
    out = tmp_path / "latest.json"
    out.write_text(json.dumps({"layer3": {"kept": True}, "layer1": {"old": True}}))
    result = run((1,), truth_dir=truth, kg_path=kg, out_path=out)
    assert result["layer3"] == {"kept": True}
    assert "old" not in result["layer1"]


def test_each_layer_keeps_its_own_cost_so_a_rerun_of_one_layer_leaves_no_stale_total(
    tmp_path,
):
    truth, kg = tiny_world(tmp_path)
    out = tmp_path / "latest.json"
    spent = {"calls": 26, "paid_calls": 14, "usd": 0.000312}
    out.write_text(json.dumps({"layer2": {"kept": True}, "cost": {"layer2": spent}}))
    result = run((1,), truth_dir=truth, kg_path=kg, out_path=out)
    # layer 2 keeps the cost of the run that made it and layer 1 gets its own
    assert result["layer2"] == {"kept": True}
    assert result["cost"]["layer2"] == spent
    assert (
        result["cost"]["layer1"]["usd"] == 0 and result["cost"]["layer1"]["calls"] == 0
    )
    assert set(result["cost"]) == {"layer1", "layer2"}
    assert "cost" not in result["layer1"]


def test_a_whole_file_cost_total_of_an_earlier_version_is_dropped(tmp_path):
    truth, kg = tiny_world(tmp_path)
    out = tmp_path / "latest.json"
    out.write_text(
        json.dumps({"cost": {"calls": 26, "paid_calls": 14, "usd": 0.000312}})
    )
    result = run((1,), truth_dir=truth, kg_path=kg, out_path=out)
    assert set(result["cost"]) == {"layer1"}


def test_main_exits_with_one_line_on_a_bad_layer(capsys):
    assert entry.main(["--layers", "9"]) == 1
    assert "layers" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "words"),
    [
        (MissingApiKey(), "OPENAI_API_KEY"),
        (NoDecisionBackend("no cached decision"), "decision"),
        (BudgetExceeded("the budget is spent"), "budget"),
        (NoCacheInstalled(), "No LLM cache is installed"),
    ],
)
def test_a_known_failure_prints_one_line_to_stderr_and_exits_with_1(
    monkeypatch, capsys, error, words
):
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(entry, "run", refuse)
    assert entry.main([]) == 1
    out, err = capsys.readouterr()
    assert len(err.strip().splitlines()) == 1 and words in err
    assert "Traceback" not in err and out == ""


def test_a_run_keeps_the_crews_block_it_did_not_write(tmp_path):
    truth, kg = tiny_world(tmp_path)
    out = tmp_path / "latest.json"
    block = {"ingestion": {"steps": []}, "ledger": {"x": {"usd": 1}}}
    out.write_text(json.dumps({"crews": block}))
    result = run((1,), truth_dir=truth, kg_path=kg, out_path=out)
    assert result["crews"] == block
    assert json.loads(out.read_text())["crews"] == block


def test_crews_writes_the_block_with_no_settings_and_no_key(monkeypatch, capsys):
    seen = {}

    def write_block(**paths):
        seen.update(paths)
        return {"ingestion": {"steps": [1, 2]}, "troubleshooting": {"steps": [3]}}

    monkeypatch.setattr(entry.crews, "write_block", write_block)
    monkeypatch.setattr(
        entry, "load_settings", lambda: pytest.fail("--crews must not load settings")
    )
    assert entry.main(["--crews"]) == 0
    assert seen["results"] == entry.RESULTS_PATH
    assert seen["timing"] == entry.crews_time.TIMING_PATH
    assert "3 steps" in capsys.readouterr().out


def test_crews_prints_one_line_when_it_cannot_build_the_block(monkeypatch, capsys):
    def refuse(**paths):
        raise ValueError("the timing file is not here")

    monkeypatch.setattr(entry.crews, "write_block", refuse)
    assert entry.main(["--crews"]) == 1
    assert "timing file" in capsys.readouterr().err


def test_time_crews_sets_the_cap_for_the_process_before_it_loads_the_settings(
    monkeypatch, capsys
):
    monkeypatch.setenv("BUDGET_USD", "40")
    ledger.record("gpt-6.1-sol", "x", 1_000_000, 0, 0, cache_hit=False)
    spent = ledger.spent_usd()
    seen = {}

    def load():
        seen["budget"] = os.environ["BUDGET_USD"]
        return "settings"

    def fake_run(settings, spent):
        seen.update(settings=settings, spent=spent)
        raise entry.BudgetExceeded("model spend has reached the budget")

    monkeypatch.setattr(entry, "load_settings", load)
    monkeypatch.setattr(entry.crews_time, "run_timing", fake_run)
    assert entry.main(["--time-crews"]) == 1
    assert float(seen["budget"]) == pytest.approx(spent + 0.10)
    assert seen["spent"] == spent and seen["settings"] == "settings"
    assert "cap" in capsys.readouterr().err


def test_time_crews_names_a_cap_that_is_not_enforced(monkeypatch, capsys):
    monkeypatch.setenv("BUDGET_USD", "40")

    def refuse(settings, spent):
        raise entry.crews_time.CapNotEnforced("the budget is not the cap")

    monkeypatch.setattr(entry, "load_settings", lambda: "settings")
    monkeypatch.setattr(entry.crews_time, "run_timing", refuse)
    assert entry.main(["--time-crews"]) == 1
    assert "not started" in capsys.readouterr().err
