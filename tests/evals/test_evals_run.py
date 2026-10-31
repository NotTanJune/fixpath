"""Tests for layers 3 to 5, the bake-off and the Langfuse push, run through the
evals entry points. Fakes stand in for the crew, the deciders and Langfuse. Nothing
here pays for a call."""

import json
from datetime import UTC, datetime

import pytest
from crew_stubs import FakeDecider, open_crew_kg, triage_answers
from test_evals_main import tiny_world

from app import ledger
from app.ask.types import AnswerCard, NodeTrace, Triage
from app.config import Settings
from app.evals import __main__ as entry
from app.evals.__main__ import run


@pytest.fixture(autouse=True)
def throwaway_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")


def write_golden(folder):
    folder.mkdir(parents=True, exist_ok=True)
    triage = {
        "id": "tri-01",
        "kind": "incident",
        "source": "incident",
        "question": "the press E217",
        "truth_incident": None,
        "hazard": False,
        "labels": {
            "machine": "HP-01",
            "alarm": "E-217",
            "root_cause": None,
            "intent": "troubleshoot",
            "safety": False,
        },
    }
    answer = {
        "id": "ans-01",
        "source": "trap",
        "trap": "out_of_scope",
        "truth_incident": None,
        "question": "can you approve my leave for Friday",
        "expect": {"status": ["out_of_scope"], "referral": True, "no_retrieval": True},
    }
    (folder / "triage.jsonl").write_text(json.dumps(triage) + "\n")
    (folder / "answers.jsonl").write_text(json.dumps(answer) + "\n")
    return folder


def stub_card(cache_hit):
    now = datetime(2026, 10, 28, tzinfo=UTC)
    trace = [
        NodeTrace(
            name="triage",
            started_at=now,
            ended_at=now,
            seconds=0.5,
            cache_hit=cache_hit,
        )
    ]
    triage = Triage(status="out_of_scope", reason="Ask the shift lead.")
    return AnswerCard(question="q", triage=triage, trace=trace)


def test_layer_3_runs_the_golden_triage_set_through_the_decider(tmp_path):
    truth, kg = tiny_world(tmp_path)
    crew_kg = tmp_path / "crew" / "kg.sqlite"
    crew_kg.parent.mkdir()
    open_crew_kg(crew_kg.parent).close()
    fake = FakeDecider(lambda purpose, state, questions: triage_answers())
    out = tmp_path / "latest.json"
    result = run(
        (3,),
        truth_dir=truth,
        kg_path=crew_kg,
        out_path=out,
        settings=Settings(),
        decider=fake,
        golden_dir=write_golden(tmp_path / "golden"),
    )
    assert fake.purposes() == ["triage"]
    assert (
        result["layer3"]["n"] == 1
        and result["layer3"]["accuracy"]["machine"]["correct"] == 1
    )
    assert result["cost"]["layer3"]["calls"] == 0
    assert json.loads(out.read_text(encoding="utf-8"))["layer3"] == json.loads(
        json.dumps(result["layer3"])
    )


def test_a_golden_run_carries_the_note_that_these_are_not_hold_out_results(tmp_path):
    truth, kg = tiny_world(tmp_path)
    crew_kg = tmp_path / "crew" / "kg.sqlite"
    crew_kg.parent.mkdir()
    open_crew_kg(crew_kg.parent).close()
    out = tmp_path / "latest.json"
    result = run(
        (3,),
        truth_dir=truth,
        kg_path=crew_kg,
        out_path=out,
        settings=Settings(),
        decider=FakeDecider(lambda purpose, state, questions: triage_answers()),
        golden_dir=write_golden(tmp_path / "golden"),
    )
    (limit,) = result["limits"]
    assert "recorded incident" in limit and "not hold-out" in limit
    assert json.loads(out.read_text(encoding="utf-8"))["limits"] == result["limits"]


def test_layers_4_and_5_share_the_crew_runs_and_layer_5_keeps_live_numbers(
    tmp_path, monkeypatch
):
    truth, kg = tiny_world(tmp_path)
    golden = write_golden(tmp_path / "golden")
    calls = {"runs": 0, "judge": 0}

    def fake_runs(items, kg_path, settings, decider, models=None):
        calls["runs"] += 1
        return [entry.layer4.Run(item, stub_card(cache_hit), []) for item in items]

    cache_hit = False
    monkeypatch.setattr(entry.layer4, "run_answers", fake_runs)
    monkeypatch.setattr(
        entry.layer4,
        "judge_run",
        lambda run, settings: calls.__setitem__("judge", calls["judge"] + 1),
    )
    out = tmp_path / "latest.json"
    live = run(
        (4, 5),
        truth_dir=truth,
        kg_path=kg,
        out_path=out,
        settings=Settings(),
        decider=FakeDecider({}),
        golden_dir=golden,
    )
    assert calls == {"runs": 1, "judge": 1}
    assert live["layer4"]["traps"][0]["trap"] == "out_of_scope"
    assert (
        live["layer5"]["live"]["latency"]["answer"]["n"] == 1
        and not live["layer5"]["this_run"]["replayed"]
    )
    cache_hit = True
    replay = run(
        (5,),
        truth_dir=truth,
        kg_path=kg,
        out_path=out,
        settings=Settings(),
        decider=FakeDecider({}),
        golden_dir=golden,
    )
    assert calls == {"runs": 2, "judge": 1}  # layer 5 alone needs no judge
    assert replay["layer5"]["live"] == live["layer5"]["live"]
    assert (
        replay["layer4"] == live["layer4"]
    )  # the layer 4 section was not run, so it is kept


class StubMetered:
    """A decider with the counters of bakeoff.Metered, answering from a script."""

    def __init__(self, name, script):
        self.name, self.model, self.asks, self.usd = name, name, 0, 0.0
        self.fake = FakeDecider(script)

    def snapshot(self):
        return self.asks, self.usd

    def ask(self, state, questions, purpose):
        self.asks += 1
        return self.fake.ask(state, questions, purpose)


def test_the_bakeoff_flag_adds_its_section_and_the_timing_flag_its_latency(
    tmp_path, monkeypatch
):
    from app.ingest.normalize import AliasTable

    truth, kg = tiny_world(tmp_path)
    crew_kg = tmp_path / "crew" / "kg.sqlite"
    crew_kg.parent.mkdir()
    open_crew_kg(crew_kg.parent).close()
    golden = write_golden(tmp_path / "golden")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"files": []}))
    monkeypatch.setattr(
        entry.replay,
        "replay_extractions",
        lambda raw_dir, settings=None: ([], AliasTable([])),
    )
    monkeypatch.setattr(entry.bakeoff, "merge_cases", lambda *args: [])
    monkeypatch.setattr(
        entry.layer4,
        "run_answers",
        lambda items, kg_path, settings, decider, models=None: [
            entry.layer4.Run(item, stub_card(True), []) for item in items
        ],
    )
    monkeypatch.setattr(
        entry.bakeoff, "triage_asks", lambda items, kg_path, decider: [({}, {})]
    )
    seen = {}

    def fake_timing(asks, make, folder, **kwargs):
        seen["asks"] = asks
        return {"measured_at": "2026-10-28", "n": 1}

    monkeypatch.setattr(entry.bakeoff, "timing_pass", fake_timing)
    deciders = {
        name: StubMetered(name, lambda purpose, state, questions: triage_answers())
        for name in ("jev", "decisions")
    }
    out = tmp_path / "latest.json"
    args = dict(
        truth_dir=crew_kg.parent,
        kg_path=crew_kg,
        out_path=out,
        settings=Settings(),
        decider=FakeDecider({}),
        golden_dir=golden,
        report_path=report,
        deciders=deciders,
    )
    # the truth folder is only read for layer 1 and the matching, so a tiny one will do
    args["truth_dir"] = truth
    first = run((1,), with_bakeoff=True, **args)
    assert first["bakeoff"]["tasks"]["triage"]["n"] == 1
    assert first["bakeoff"]["latency"] is None and "asks" not in seen
    assert first["cost"]["bakeoff"]["calls"] == 0
    timed = run((1,), with_bakeoff=True, with_timing=True, **args)
    assert timed["bakeoff"]["latency"] == {"measured_at": "2026-10-28", "n": 1}
    again = run((1,), with_bakeoff=True, **args)
    # a run that does not time keeps the last pass and says how it was measured
    assert again["bakeoff"]["latency"] == {
        "measured_at": "2026-10-28",
        "n": 1,
        "caveat": entry.bakeoff.LATENCY_CAVEAT,
    }


def test_push_needs_langfuse_keys_and_says_so_in_one_line(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(entry, "load_settings", lambda: Settings())
    monkeypatch.setattr(entry, "RESULTS_PATH", tmp_path / "latest.json")
    assert entry.main(["--push"]) == 1
    out, err = capsys.readouterr()
    assert len(err.strip().splitlines()) == 1 and "LANGFUSE" in err and out == ""


def test_push_sends_the_golden_sets_and_the_stored_scores_through_the_client(
    monkeypatch, capsys, tmp_path
):
    from test_evals_push import RESULTS, FakeLangfuse

    from app.config import Settings as S

    golden = write_golden(tmp_path / "golden")
    results = tmp_path / "latest.json"
    results.write_text(
        json.dumps(
            {
                "layer3": {
                    "items": [
                        {
                            "id": "tri-01",
                            "question": "q",
                            "status": "act",
                            "predicted": {},
                            "correct": {"machine": True},
                        }
                    ]
                },
                "layer4": RESULTS["layer4"],
            }
        )
    )
    client = FakeLangfuse()
    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: S(langfuse_public_key="p", langfuse_secret_key="s"),
    )
    monkeypatch.setattr(entry, "langfuse_client", lambda settings: client)
    monkeypatch.setattr(entry, "RESULTS_PATH", results)
    monkeypatch.setattr(entry, "GOLDEN_DIR", golden)
    assert entry.main(["--push"]) == 0
    assert [c for c, _ in client.calls].count("create_dataset") == 2
    assert client.calls[-1] == ("flush", None)
    assert "pushed" in capsys.readouterr().out


def test_a_langfuse_api_error_is_one_line_and_not_a_traceback(
    monkeypatch, capsys, tmp_path
):
    from langfuse.api.core.api_error import ApiError

    results = tmp_path / "latest.json"
    results.write_text(json.dumps({"layer3": {"items": []}, "layer4": {"items": []}}))

    class Refusing:
        def create_dataset(self, **kwargs):
            raise ApiError(status_code=401, body={"message": "Invalid credentials"})

    monkeypatch.setattr(
        entry,
        "load_settings",
        lambda: Settings(langfuse_public_key="p", langfuse_secret_key="s"),
    )
    monkeypatch.setattr(entry, "langfuse_client", lambda settings: Refusing())
    monkeypatch.setattr(entry, "RESULTS_PATH", results)
    monkeypatch.setattr(entry, "GOLDEN_DIR", write_golden(tmp_path / "golden"))
    assert entry.main(["--push"]) == 1
    out, err = capsys.readouterr()
    assert len(err.strip().splitlines()) == 1 and "401" in err and "Langfuse" in err
    assert "Traceback" not in err and out == ""


def test_main_runs_the_layers_and_the_flags_it_is_given(monkeypatch, capsys):
    seen = {}

    def fake_run(layers, **kwargs):
        seen.update(layers=layers, **kwargs)
        return {
            "cost": {
                f"layer{n}": {"calls": 0, "cache_hits": 0, "paid_calls": 0, "usd": 0.0}
                for n in layers
            }
        }

    monkeypatch.setattr(entry, "run", fake_run)
    assert entry.main(["--layers", "1,2,3,4,5", "--bakeoff", "--time"]) == 0
    assert (
        seen["layers"] == (1, 2, 3, 4, 5)
        and seen["with_bakeoff"] is True
        and seen["with_timing"] is True
    )
    seen.clear()
    assert entry.main([]) == 0
    assert (
        seen["layers"] == (1, 2)
        and seen["with_bakeoff"] is False
        and seen["with_timing"] is False
    )
