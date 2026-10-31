"""Smoke tests for the evals on the committed data, with no keys.

Layer 1 scores the committed graph. Layer 2 replays the extractors and decisions from
the committed caches, so every question it asks must already be cached. The tests work
on copies of the caches and a temporary ledger, so nothing in data/ is written."""

import json
import shutil

import pytest
from langchain_core.globals import set_llm_cache

from app import config, ledger, llm, llm_cache
from app.config import RAW_DIR, ROOT, Settings
from app.decide import NoDecisionBackend
from app.decide import base as decide_base
from app.decide.base import REPLAY_ORDER, CacheOnlyDecider, DecisionCache
from app.evals import layer1, layer2, layer4, replay
from app.evals.__main__ import run
from app.evals.truth import load_graph, load_truth
from app.ingest import questions
from app.ingest.report import default_report_path
from app.llm import MissingApiKey, NoCacheInstalled

ADVICE = (
    "A question is not in the committed decision cache. Run "
    "`uv run python -m app.evals --layers 1,2` with keys, then commit data/cache/ "
    "and data/evals/results/latest.json."
)


@pytest.fixture
def keyless(tmp_path, monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "TYPESAFE_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    caches = tmp_path / "caches"
    caches.mkdir()
    for source in (llm_cache.CACHE_PATH, decide_base.CACHE_PATH):
        shutil.copy(source, caches / source.name)
    monkeypatch.setattr(llm_cache, "CACHE_PATH", caches / "llm_cache.sqlite")
    monkeypatch.setattr(decide_base, "CACHE_PATH", caches / "decisions.sqlite")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")
    monkeypatch.setattr(llm, "load_settings", lambda: Settings())
    yield caches
    set_llm_cache(None)


def test_layer_1_on_the_committed_graph_recovers_every_asset_and_alarm_code():
    result = layer1.evaluate(load_truth(), load_graph())
    assert result["nodes"]["Asset"]["f1"] == 1.0
    assert result["nodes"]["AlarmCode"]["f1"] == 1.0
    assert result["nodes"]["Procedure"]["f1"] == 1.0
    assert set(result) == {"nodes", "edges", "properties", "documents", "matching"}


def test_layer_2_over_the_committed_decision_cache_completes_with_no_cache_miss(
    keyless,
):
    truth, graph = load_truth(), load_graph()
    report = json.loads(default_report_path(config.KG_PATH).read_text(encoding="utf-8"))
    extractions, aliases = replay.replay_extractions(RAW_DIR)
    decider = CacheOnlyDecider(DecisionCache())
    try:
        result = layer2.evaluate(truth, graph, report, extractions, aliases, decider)
    except NoDecisionBackend:
        pytest.fail(ADVICE)
    assert set(result) == {"gates", "same_entity", "same_incident", "root_cause"}
    entity = result["same_entity"]
    assert entity["sources"]["resolver_asked"] > 0 and entity["sources"]["siblings"] > 0
    assert entity["pairs"]["true"] > 0 and entity["pairs"]["false"] > 0
    assert result["same_incident"]["pairs"]["labeled"] > 0
    assert result["root_cause"]["pairs"]["true"] > 0
    assert all(n["p"] is not None for n in entity["negatives"])
    rows = ledger_rows()
    assert rows and all(row["cache_hit"] == 1 for row in rows)


def ledger_rows():
    from app.ingest.report import ledger_rows_since

    return ledger_rows_since(0)


def test_the_entry_point_writes_both_layers_and_a_free_cost_from_the_caches(
    keyless, tmp_path
):
    out = tmp_path / "results" / "latest.json"
    try:
        result = run((1, 2), out_path=out, settings=Settings())
    except NoDecisionBackend:
        pytest.fail(ADVICE)
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(json.dumps(result))
    assert {"layer1", "layer2", "cost"} <= set(result)
    for layer in ("layer1", "layer2"):
        assert (
            result["cost"][layer]["paid_calls"] == 0
            and result["cost"][layer]["usd"] == 0
        )


DRIFT = (
    "The eval's state building has drifted from the resolver's: the states it rebuilds "
    "for questions the resolver really asked are not in the committed decision cache. "
    "Fix app.evals.layer2.entity_state_for or app.evals.replay.aliases_at; running with "
    "keys would only pay for answers to the wrong questions. Missing: "
)


def missing_states() -> list[str]:
    """The questions the eval rebuilds for every logged true pair and every wrong
    candidate the resolver asked about. The committed decision cache must hold an
    answer for each one."""
    truth, graph = load_truth(), load_graph()
    report = json.loads(default_report_path(config.KG_PATH).read_text(encoding="utf-8"))
    extractions, aliases = replay.replay_extractions(RAW_DIR)
    merges = layer2.merge_log(report)
    first = replay.first_mentions(extractions)
    pairs, _rules, _none = layer2.entity_pairs(truth, merges)
    logged = {
        (p.type, p.mention, p.doc_path, p.candidate)
        for p in pairs
        if p.label is not None
    }
    asked = replay.resolver_questions(
        extractions, aliases, CacheOnlyDecider(DecisionCache())
    )
    wanted = [
        (p.index, p.type, p.mention, p.doc_path, p.candidate, "logged true pair")
        for p in pairs
        if p.label is True
    ]
    wanted += [
        (n.index, n.type, n.mention, n.doc_path, n.candidate, "resolver-asked negative")
        for n in layer2.resolver_negatives(truth, asked, merges, logged)
    ]
    assert len(wanted) > 0 and any(w[-1] == "logged true pair" for w in wanted)
    cache = DecisionCache()
    absent = []
    for index, type_, mention, doc_path, candidate, what in wanted:
        state = layer2.entity_state_for(
            graph,
            merges,
            first,
            index=index,
            type=type_,
            mention=mention,
            doc_path=doc_path,
            candidate=candidate,
        )
        keys = [
            DecisionCache.key(
                backend, model, state, {"same_entity": questions.SAME_ENTITY}
            )
            for backend, model in REPLAY_ORDER
        ]
        if not any(cache.get(key) for key in keys):
            absent.append(f"{what}: {mention!r} against {candidate}")
    return absent


def test_the_eval_rebuilds_the_states_the_resolver_sent_for_every_true_pair_and_asked_negative(
    keyless,
):
    assert missing_states() == [], DRIFT


def test_the_fidelity_check_fails_when_the_state_building_drifts(keyless, monkeypatch):
    # the final aliases, with none of the later merges taken out, are not what the
    # resolver saw at the time
    monkeypatch.setattr(
        layer2,
        "aliases_at",
        lambda graph, candidate, merges, index: list(
            graph.nodes[candidate].props.get("aliases", [])
        ),
    )
    assert missing_states() != []


RESULTS = ROOT / "data" / "evals" / "results" / "latest.json"
STALE = (
    "The committed results no longer match a replay from the committed caches. Run "
    "`make evals` with keys, then commit data/cache/ and data/evals/results/latest.json."
)


def section_text(results, key):
    return json.dumps(results[key], indent=2, sort_keys=True, ensure_ascii=False)


def test_layers_3_to_5_and_the_bakeoff_replay_from_the_caches_with_no_paid_call(
    keyless, tmp_path
):
    out = tmp_path / "latest.json"
    # a replay keeps the live numbers of layer 5 and the latency
    shutil.copy(RESULTS, out)
    committed = json.loads(RESULTS.read_text(encoding="utf-8"))
    try:
        replayed = run((3, 4, 5), out_path=out, settings=Settings(), with_bakeoff=True)
    except (NoDecisionBackend, MissingApiKey, NoCacheInstalled) as error:
        pytest.fail(f"{STALE} ({type(error).__name__}: {error})")
    rows = ledger_rows()
    assert rows, "the replay booked no calls"
    assert all(row["cache_hit"] == 1 for row in rows), "a replay made a paid call"
    for key in ("layer3", "layer4", "bakeoff"):
        same = section_text(replayed, key) == section_text(committed, key)
        assert same, f"{key}: {STALE}"
    # the speed numbers belong to the last live run, which a replay carries forward
    assert replayed["layer5"]["live"] == committed["layer5"]["live"]
    assert replayed["layer5"]["this_run"]["replayed"] is True


def test_every_item_whose_truth_fix_needs_lockout_has_the_safety_block_on_its_card():
    section = json.loads(RESULTS.read_text(encoding="utf-8"))["layer4"]
    assert section["lockout"]["n"] > 0
    assert layer4.lockout_misses(section) == [], "a card lacks the lockout warning"
    assert section["lockout"]["recall"] == 1.0


def test_the_five_traps_pass_on_the_committed_results():
    section = json.loads(RESULTS.read_text(encoding="utf-8"))["layer4"]
    failed = [t["trap"] for t in section["traps"] if not t["ok"]]
    assert failed == [] and section["traps_passed"] == {"passed": 5, "n": 5}
