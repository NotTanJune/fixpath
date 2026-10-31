"""Tests for the bake-off, which puts the same questions to two backends (JEV and OpenAI
Decisions) and compares accuracy, confidence, cost and speed. No test pays for a call.
Both backends are fakes."""

import pytest
from crew_stubs import choice, noul, open_crew_kg, triage_answers
from eval_stubs import edge, node, write_kg, write_truth

from app import config, ledger
from app.config import Settings
from app.decide import Choice, Noul
from app.decide.base import (
    Answer,
    CachedDecider,
    DecisionCache,
    NoDecisionBackend,
    Usage,
)
from app.evals import bakeoff, layer4
from app.evals.match import Matching
from app.evals.truth import load_graph, load_truth
from app.ingest import questions

JEV, LUNA = config.JEV_MODEL, config.DECISIONS_MODEL
STATE = {"x": 1}
QS = {"same_entity": questions.SAME_ENTITY}


@pytest.fixture(autouse=True)
def throwaway_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")


class FakeBackend:
    """A backend whose answers come from a function of the state and the questions."""

    def __init__(self, name, model, script, usage=Usage(1000, 0, 0)):
        self.name, self.model, self.script, self.usage = name, model, script, usage
        self.requests = 0

    def decide(self, state, questions_):
        self.requests += 1
        return self.script(state, questions_), self.usage


def yes(p):
    return lambda state, qs: {qid: noul(p) for qid in qs}


# --- the deciders ---------------------------------------------------------------------


def test_a_replay_decider_serves_only_the_answers_stored_under_its_own_backend(
    tmp_path,
):
    cache = DecisionCache(tmp_path / "d.sqlite")
    answers = {"same_entity": noul(0.9)}
    cache.put(DecisionCache.key("jev", JEV, STATE, QS), answers, Usage(100))
    assert (
        bakeoff.ReplayDecider(cache, "jev").ask(STATE, QS, "bakeoff")["same_entity"].p
        == 0.9
    )
    with pytest.raises(NoDecisionBackend, match="decisions"):
        bakeoff.ReplayDecider(cache, "decisions").ask(STATE, QS, "bakeoff")


def test_a_replay_books_a_free_ledger_line(tmp_path):
    cache = DecisionCache(tmp_path / "d.sqlite")
    cache.put(
        DecisionCache.key("jev", JEV, STATE, QS), {"same_entity": noul(0.9)}, Usage(100)
    )
    bakeoff.ReplayDecider(cache, "jev").ask(STATE, QS, "bakeoff x")
    (row,) = [r for r in _ledger()]
    assert row["purpose"] == "bakeoff x" and row["cache_hit"] == 1 and row["usd"] == 0


def _ledger():
    from app.ingest.report import ledger_rows_since

    return ledger_rows_since(0)


def test_the_metered_decider_counts_asks_and_prices_them_from_the_stored_usage(
    tmp_path,
):
    cache = DecisionCache(tmp_path / "d.sqlite")
    backend = FakeBackend("decisions", LUNA, yes(0.8), Usage(1_000_000, 0, 0))
    metered = bakeoff.Metered(CachedDecider(backend, cache, Settings()), cache)
    metered.ask(STATE, QS, "bakeoff")
    metered.ask(STATE, QS, "bakeoff")  # the second is answered from the cache
    assert backend.requests == 1
    assert metered.asks == 2
    # the stored usage prices a replayed ask as it priced the paid one: $0.10 per
    # million
    assert metered.usd == pytest.approx(0.20)
    assert (metered.name, metered.model) == ("decisions", LUNA)


def test_deciders_without_keys_replay_and_with_keys_go_through_the_cache(tmp_path):
    cache = DecisionCache(tmp_path / "d.sqlite")
    keyless = bakeoff.make_deciders(Settings(), cache)
    assert set(keyless) == {"jev", "decisions"}
    assert all(isinstance(m.inner, bakeoff.ReplayDecider) for m in keyless.values())
    keyed = bakeoff.make_deciders(
        Settings(openai_api_key="a", typesafe_api_key="b"), cache
    )
    assert all(isinstance(m.inner, CachedDecider) for m in keyed.values())
    assert [m.model for m in keyed.values()] == [JEV, LUNA]


# --- the cases ------------------------------------------------------------------------


def entity_ask(text, candidate, doc="docs/a.csv", type_="Part"):
    state = {
        "mention": {"type": type_, "text": text, "quote": "q", "doc_path": doc},
        "candidate": {
            "id": candidate,
            "label": candidate,
            "description": "",
            "aliases": [],
            "evidence": [],
        },
    }
    return layer4.Ask("merge Part", state, QS, {"same_entity": noul(0.9)})


@pytest.fixture
def small(tmp_path):
    nodes = [
        node("P-1", "Part", aliases=["PEE 1"]),
        node("P-2", "Part", aliases=["PEE 2"]),
        node("HP-01", "Asset"),
        node(
            "I-1",
            "Incident",
            date="2026-04-14",
            symptom_text="oil leak",
            downtime_min=30,
        ),
        node(
            "I-2", "Incident", date="2026-05-01", symptom_text="noise", downtime_min=60
        ),
        node("FM-A", "FailureMode"),
        node("FM-B", "FailureMode"),
    ]
    edges = [
        edge("I-1", "OCCURRED_ON", "HP-01"),
        edge("I-2", "OCCURRED_ON", "HP-01"),
        edge("I-1", "ROOT_CAUSE", "FM-A"),
        edge("I-2", "ROOT_CAUSE", "FM-B"),
    ]
    mentions = {
        "raw/docs/a.csv": {"parts": ["P-1"], "incidents": ["I-1"]},
        "raw/docs/b.csv": {"incidents": ["I-1"]},
        "raw/docs/c.csv": {"incidents": ["I-2"]},
    }
    truth = load_truth(write_truth(tmp_path / "truth", nodes, edges, mentions))
    graph_nodes = [
        node("P-1", "Part"),
        node("HP-01", "Asset"),
        node(
            "INC-1",
            "Incident",
            date="2026-04-14",
            symptom_text="oil leak",
            resolution_text="seal",
            downtime_min=30,
            technician="Ann",
            shift="A",
        ),
        node("FM-G1", "FailureMode"),
        node("FM-G2", "FailureMode"),
    ]
    graph = load_graph(
        write_kg(
            tmp_path / "kg.sqlite", graph_nodes, [edge("INC-1", "OCCURRED_ON", "HP-01")]
        )
    )
    matching = Matching(
        ids={
            "INC-1": "I-1",
            "FM-G1": "FM-A",
            "FM-G2": "FM-B",
            "P-1": "P-1",
            "HP-01": "HP-01",
        }
    )
    return truth, graph, matching


def test_entity_cases_are_the_asks_the_truth_can_label(small):
    truth, _graph, _matching = small
    asks = [
        entity_ask("PEE 1", "P-1"),  # the mention names this part
        entity_ask("PEE 1", "P-2"),  # and not this one
        entity_ask("PEE 1", "P-1"),  # asked twice: one case
        entity_ask("mystery", "P-2", doc="docs/none.csv"),  # no label
        entity_ask("HP-01", "HP-01", type_="Asset"),  # an asset named by its id
    ]
    cases = bakeoff.entity_cases(truth, asks)
    assert [(c.task, c.meta["candidate"], c.label) for c in cases] == [
        ("same_entity", "P-1", True),
        ("same_entity", "P-2", False),
        ("same_entity", "HP-01", True),
    ]
    assert cases[0].state == asks[0].state and cases[0].qid == "same_entity"


def test_sibling_cases_ask_the_other_parts_of_the_family_that_the_resolver_did_not(
    small,
):
    truth, graph, _m = small
    graph = graph  # P-1 only, so there is no sibling in this graph
    merges = [
        {
            "type": "Part",
            "doc_path": "docs/a.csv",
            "mention": "PEE 1",
            "candidate": "P-1",
            "decision": "merge",
            "p": 0.9,
        }
    ]
    assert bakeoff.sibling_cases(truth, graph, merges, {}, set()) == []


def incident_ask(p=0.95):
    return layer4.Ask(
        "merge Incident",
        {"record_a": {"date": "2026-04-14"}, "record_b": {"date": "2026-04-14"}},
        {"same_incident": questions.SAME_INCIDENT},
        {"same_incident": noul(p)},
    )


def test_incident_cases_pair_each_ask_with_its_logged_decision_and_label_it(small):
    from app.evals.labels import Record

    truth, _g, _m = small
    records = {
        ("docs/a.csv", "k1"): Record(
            "docs/a.csv", "k1", "2026-04-14", "HP-01", None, 30
        ),
        ("docs/b.csv", "k1"): Record(
            "docs/b.csv", "k1", "2026-04-14", "HP-01", None, 30
        ),
        ("docs/c.csv", "k1"): Record(
            "docs/c.csv", "k1", "2026-05-01", "HP-01", None, 60
        ),
    }
    merges = [
        {
            "type": "Part",
            "doc_path": "docs/a.csv",
            "mention": "x",
            "candidate": "P-1",
            "decision": "merge",
            "p": 1,
        },
        {
            "type": "Incident",
            "doc_path": "docs/a.csv",
            "mention": "k1",
            "candidate": "docs/b.csv::k1",
            "decision": "merge",
            "p": 0.95,
        },
        {
            "type": "Incident",
            "doc_path": "docs/a.csv",
            "mention": "k1",
            "candidate": "docs/c.csv::k1",
            "decision": "new",
            "p": 0.1,
        },
        {
            "type": "Incident",
            "doc_path": "docs/a.csv",
            "mention": "k1",
            "candidate": None,
            "decision": "rule",
            "p": 1,
        },
    ]
    cases = bakeoff.incident_cases(
        truth, [incident_ask(), incident_ask(0.1)], merges, records
    )
    assert [(c.task, c.label) for c in cases] == [
        ("same_incident", True),
        ("same_incident", False),
    ]


def test_incident_cases_fail_loudly_when_a_decision_is_not_the_one_for_its_ask(small):
    truth, _g, _m = small
    merges = [
        {
            "type": "Incident",
            "doc_path": "d",
            "mention": "k",
            "candidate": "e::k",
            "decision": "new",
            "p": 0.1,
        }
    ]
    with pytest.raises(ValueError, match="not in the same order"):
        bakeoff.incident_cases(truth, [incident_ask(0.95)], merges, {})


def test_incident_cases_fail_loudly_when_the_asks_and_the_log_disagree(small):
    truth, _g, _m = small
    merges = [
        {
            "type": "Incident",
            "doc_path": "d",
            "mention": "k",
            "candidate": "e::k",
            "decision": "new",
            "p": 0.1,
        }
    ]
    with pytest.raises(ValueError, match="1 logged"):
        bakeoff.incident_cases(truth, [incident_ask(), incident_ask()], merges, {})


def root_cause_ask(date="2026-04-14", symptom="oil leak"):
    options = {"FM-G1": "one", "FM-G2": "two", "unknown": "none"}
    incident = {
        "machine": "HP-01",
        "date": date,
        "symptom": symptom,
        "action": "seal",
        "downtime_min": 30,
        "technician": "Ann",
        "shift": "A",
    }
    return layer4.Ask(
        "link root cause",
        {"incident": incident, "alarms": []},
        {
            "root_cause": questions.root_cause_question(
                {k: v for k, v in options.items() if k != "unknown"}
            )
        },
        {"root_cause": choice("FM-G1", 0.9)},
    )


def test_root_cause_cases_find_the_incident_and_accept_the_graph_ids_of_the_truth_cause(
    small,
):
    truth, graph, matching = small
    cases = bakeoff.root_cause_cases(
        truth, graph, matching, [root_cause_ask(), root_cause_ask(date="1999-01-01")]
    )
    (case,) = cases  # the second incident is not in the graph
    assert case.task == "root_cause" and case.label == frozenset({"FM-G1"})
    assert case.meta["incident"] == "INC-1"


# --- scoring --------------------------------------------------------------------------


def results(*pairs):
    return [
        (
            bakeoff.Case("t", STATE, QS, "same_entity", label, {}),
            noul(p) if p is not None else None,
        )
        for label, p in pairs
    ]


def test_binary_accuracy_reads_a_probability_at_the_gate_and_no_answer_as_no_merge():
    scored = bakeoff.score_binary(
        results((True, 0.9), (False, 0.8), (False, None), (True, None)), gate=0.7
    )
    # right: the first, and the missing answer on a false pair
    assert scored["n"] == 4 and scored["accuracy"] == 0.5 and scored["undecided"] == 2
    assert scored["ece"] is not None


def test_binary_ece_is_the_gap_between_the_probability_and_the_rate_of_true_pairs():
    scored = bakeoff.score_binary(
        results((True, 0.9), (True, 0.9), (False, 0.9), (True, 0.9)), gate=0.7
    )
    assert scored["ece"] == pytest.approx(abs(0.75 - 0.9))
    assert bakeoff.score_binary([], gate=0.7)["accuracy"] is None


def test_root_cause_is_right_when_the_chosen_option_is_an_accepted_id():
    cases = [
        (
            bakeoff.Case(
                "root_cause", STATE, QS, "root_cause", frozenset({"FM-G1"}), {}
            ),
            choice("FM-G1", 0.8),
        ),
        (
            bakeoff.Case(
                "root_cause", STATE, QS, "root_cause", frozenset({"FM-G1"}), {}
            ),
            choice("unknown", 0.9),
        ),
        (
            bakeoff.Case(
                "root_cause", STATE, QS, "root_cause", frozenset({"FM-G1"}), {}
            ),
            None,
        ),
    ]
    scored = bakeoff.score_root_cause(cases)
    assert scored["accuracy"] == pytest.approx(1 / 3) and scored["undecided"] == 1
    # the confidence is that of the chosen option: 0.8 for a right pick, 0.9 for a wrong
    # one
    assert scored["ece"] == pytest.approx((abs(1 - 0.8) + abs(0 - 0.9)) / 2)


def verdict_answer(choice_key, p, rest=None):
    probabilities = {choice_key: p, **(rest or {})}
    return Answer(
        "choice",
        choice=choice_key,
        probabilities=probabilities,
        confidence=p,
        backend="fake",
    )


def test_verify_is_right_when_keep_or_drop_matches_the_judge():
    rows = [
        (
            True,
            verdict_answer("supported", 0.9, {"partly": 0.05}),
        ),  # kept, judge agrees
        (False, verdict_answer("supported", 0.8)),  # kept, judge says no
        (
            False,
            verdict_answer("not_supported", 0.9, {"supported": 0.1}),
        ),  # dropped, right
        # partly 0.4 plus supported 0.3 reaches the keep gate: kept, right
        (True, verdict_answer("partly", 0.4, {"supported": 0.3})),
        # partly 0.4 plus supported 0.05 is below the keep gate: dropped, wrong
        (True, verdict_answer("partly", 0.4, {"supported": 0.05})),
        # no single choice reaches 0.5, but supported plus partly is 0.89: kept, right
        (True, verdict_answer("supported", 0.47, {"partly": 0.42})),
        (False, None),  # no answer: dropped, right
    ]
    scored = bakeoff.score_verify(rows)
    assert scored["n"] == 7 and scored["accuracy"] == pytest.approx(5 / 7)
    assert scored["undecided"] == 1


def test_verify_flags_use_the_support_mass_at_any_gate():
    rows = [(True, verdict_answer("supported", 0.47, {"partly": 0.42}))]
    assert bakeoff.verify_flags(rows) == [True]
    assert bakeoff.verify_flags(rows, 0.9) == [False]


# --- the table ------------------------------------------------------------------------


def backends(tmp_path, jev_script, luna_script):
    cache = DecisionCache(tmp_path / "d.sqlite")
    jev = FakeBackend("jev", JEV, jev_script)
    luna = FakeBackend("decisions", LUNA, luna_script, Usage(2000, 0, 0))
    deciders = {
        "jev": bakeoff.Metered(CachedDecider(jev, cache, Settings()), cache),
        "decisions": bakeoff.Metered(CachedDecider(luna, cache, Settings()), cache),
    }
    return deciders, (jev, luna)


def entity_case(label, n=0):
    state = {"n": n}
    return bakeoff.Case("same_entity", state, QS, "same_entity", label, {})


def test_the_table_scores_every_task_for_both_backends_and_names_the_leader(tmp_path):
    deciders, _ = backends(tmp_path, yes(0.9), yes(0.2))
    cases = [entity_case(True, 0), entity_case(True, 1), entity_case(False, 2)]
    kg = open_crew_kg(tmp_path)
    kg.close()
    table = bakeoff.evaluate(
        cases, [], [], [], tmp_path / "kg.sqlite", Matching(), deciders, previous=None
    )
    entity = table["tasks"]["same_entity"]
    assert entity["n"] == 3
    assert entity["jev"]["accuracy"] == pytest.approx(2 / 3)
    assert entity["decisions"]["accuracy"] == pytest.approx(1 / 3)
    # two cases only JEV gets right and one only Decisions gets right: not a lead
    assert entity["leader"] == "tie" and entity["verdict"] == "no clear difference"
    assert entity["sign_test"] == {"jev_only": 2, "decisions_only": 1, "n": 3, "p": 1.0}
    # a million tokens a request would be priced; here 1,000 tokens at $0.042 and 2,000
    # at $0.10
    assert entity["jev"]["asks"] == 3
    assert entity["jev"]["cost_per_1000_usd"] == pytest.approx(
        1000 * 0.042 * 1000 / 1_000_000
    )
    assert entity["decisions"]["cost_per_1000_usd"] == pytest.approx(
        1000 * 0.10 * 2000 / 1_000_000
    )
    assert table["tuned_on"] == "jev" and "tuned" in table["bias"]
    assert table["tasks"]["same_incident"]["n"] == 0
    assert table["summary"]["tasks_led"]["jev"] == 0
    assert table["summary"]["better_by_accuracy"] == "no clear difference"


def test_the_triage_task_runs_the_golden_questions_through_each_backend(tmp_path):
    deciders, (jev, luna) = backends(
        tmp_path,
        lambda state, qs: triage_answers(),
        lambda state, qs: triage_answers(machine=choice("CNC-01", 0.9)),
    )
    kg = open_crew_kg(tmp_path)
    kg.close()
    items = [
        {
            "id": "t1",
            "kind": "incident",
            "source": "incident",
            "question": "the press stopped with E217",
            "labels": {
                "machine": "HP-01",
                "alarm": "E-217",
                "root_cause": None,
                "intent": "troubleshoot",
                "safety": False,
            },
        }
    ]
    table = bakeoff.evaluate(
        [], [], [], items, tmp_path / "kg.sqlite", Matching(), deciders, previous=None
    )
    triage = table["tasks"]["triage"]
    assert triage["n"] == 1
    assert triage["jev"]["accuracy"] > triage["decisions"]["accuracy"]
    assert jev.requests == 1 and luna.requests == 1


def verify_run(label=True):
    questions_ = {
        "C1": Choice(
            "Do the quotes support it?",
            {"supported": "y", "partly": "p", "not_supported": "n"},
        )
    }
    state = {"claims": [{"id": "C1", "text": "t", "quotes": ["q"]}]}
    ask = layer4.Ask("verify", state, questions_, {})
    run = layer4.Run({"id": "a"}, None, [ask])
    return run, layer4.Judged({}, {"C1": label})


def test_the_verify_task_asks_each_backend_for_the_claims_and_scores_them_by_the_judge(
    tmp_path,
):
    def jev_says(state, qs):
        return {"C1": verdict_answer("supported", 0.9)}

    def luna_says(state, qs):
        return {"C1": verdict_answer("not_supported", 0.9)}

    deciders, _ = backends(tmp_path, jev_says, luna_says)
    run, judged = verify_run(True)
    table = bakeoff.evaluate(
        [],
        [run],
        [judged],
        [],
        tmp_path / "kg.sqlite",
        Matching(),
        deciders,
        previous=None,
    )
    verify = table["tasks"]["verify"]
    assert (
        verify["n"],
        verify["jev"]["accuracy"],
        verify["decisions"]["accuracy"],
    ) == (1, 1.0, 0.0)
    assert verify["jev"]["asks"] == 1


def test_a_run_without_a_judgement_adds_no_verify_claims(tmp_path):
    deciders, (jev, _luna) = backends(tmp_path, yes(0.9), yes(0.9))
    run, _judged = verify_run()
    table = bakeoff.evaluate(
        [],
        [run],
        [None],
        [],
        tmp_path / "kg.sqlite",
        Matching(),
        deciders,
        previous=None,
    )
    assert table["tasks"]["verify"]["n"] == 0 and jev.requests == 0


def test_the_table_is_the_same_when_it_is_made_again_from_the_cache(tmp_path):
    deciders, (jev, luna) = backends(tmp_path, yes(0.9), yes(0.2))
    cases = [entity_case(True, 0), entity_case(False, 1)]
    kg = open_crew_kg(tmp_path)
    kg.close()
    first = bakeoff.evaluate(
        cases, [], [], [], tmp_path / "kg.sqlite", Matching(), deciders, previous=None
    )
    requests = jev.requests + luna.requests
    second = bakeoff.evaluate(
        cases, [], [], [], tmp_path / "kg.sqlite", Matching(), deciders, previous=None
    )
    assert first == second
    assert jev.requests + luna.requests == requests  # no new request


def test_the_latency_of_the_last_timing_pass_is_kept_by_a_run_that_does_not_time(
    tmp_path,
):
    deciders, _ = backends(tmp_path, yes(0.9), yes(0.2))
    kept = {"measured_at": "2026-10-28", "n": 30, "jev": {"p50": 0.4}}
    table = bakeoff.evaluate(
        [],
        [],
        [],
        [],
        tmp_path / "kg.sqlite",
        Matching(),
        deciders,
        previous={"latency": kept},
    )
    assert table["latency"] == {**kept, "caveat": bakeoff.LATENCY_CAVEAT}
    assert (
        bakeoff.evaluate(
            [], [], [], [], tmp_path / "kg.sqlite", Matching(), deciders, previous=None
        )["latency"]
        is None
    )


# --- the sign test, the best gate and the verdict -------------------------------------


def test_the_exact_sign_test_is_two_sided_on_the_cases_the_backends_split():
    assert bakeoff.sign_test(0, 0) == 1.0
    assert bakeoff.sign_test(1, 0) == 1.0
    assert bakeoff.sign_test(2, 1) == 1.0
    assert bakeoff.sign_test(7, 0) == pytest.approx(2 / 128)
    assert bakeoff.sign_test(0, 7) == pytest.approx(2 / 128)
    assert bakeoff.sign_test(6, 0) == pytest.approx(2 / 64)
    assert bakeoff.sign_test(5, 0) == pytest.approx(2 / 32)
    assert bakeoff.sign_test(8, 2) == pytest.approx(2 * 56 / 1024)


def entity_cases_with(labels):
    return [entity_case(label, n) for n, label in enumerate(labels)]


def test_a_task_is_led_only_when_the_sign_test_is_below_five_percent(tmp_path):
    # JEV is right on all eight cases, Decisions on none of the eight true pairs
    deciders, _ = backends(tmp_path, yes(0.95), yes(0.1))
    cases = entity_cases_with([True] * 8)
    table = bakeoff.evaluate(
        cases, [], [], [], tmp_path / "kg.sqlite", Matching(), deciders, previous=None
    )
    entity = table["tasks"]["same_entity"]
    assert entity["sign_test"]["p"] == pytest.approx(2 / 256)
    assert entity["leader"] == "jev" and entity["verdict"] == "jev"
    assert table["summary"]["tasks_led"]["jev"] == 1
    assert table["summary"]["better_by_accuracy"] == "jev"


def test_a_lead_that_the_sign_test_cannot_confirm_is_no_clear_difference(tmp_path):
    # five cases only JEV gets right: p = 2/32, above 0.05
    deciders, _ = backends(tmp_path, yes(0.95), yes(0.1))
    table = bakeoff.evaluate(
        entity_cases_with([True] * 5),
        [],
        [],
        [],
        tmp_path / "kg.sqlite",
        Matching(),
        deciders,
        previous=None,
    )
    entity = table["tasks"]["same_entity"]
    assert entity["jev"]["accuracy"] == 1.0 and entity["decisions"]["accuracy"] == 0.0
    assert entity["leader"] == "tie" and entity["verdict"] == "no clear difference"


def test_a_task_where_both_backends_score_everything_is_saturated(tmp_path):
    deciders, _ = backends(tmp_path, yes(0.95), yes(0.95))
    table = bakeoff.evaluate(
        entity_cases_with([True] * 4),
        [],
        [],
        [],
        tmp_path / "kg.sqlite",
        Matching(),
        deciders,
        previous=None,
    )
    entity = table["tasks"]["same_entity"]
    assert entity["saturated"] is True and entity["verdict"] == "saturated"
    assert entity["leader"] == "tie"
    assert table["tasks"]["same_incident"]["saturated"] is False  # no cases at all


def test_each_backend_gets_its_own_best_gate_from_the_sweep_and_it_is_in_sample(
    tmp_path,
):
    # the pairs are wrong to merge, Decisions says 0.75: right only at gates above that
    deciders, _ = backends(tmp_path, yes(0.95), yes(0.75))
    cases = entity_cases_with([False] * 4)
    table = bakeoff.evaluate(
        cases, [], [], [], tmp_path / "kg.sqlite", Matching(), deciders, previous=None
    )
    entity = table["tasks"]["same_entity"]
    # the shared gate is 0.7: both merge every false pair
    assert entity["jev"]["accuracy"] == 0.0 and entity["decisions"]["accuracy"] == 0.0
    jev, luna = entity["jev"]["best_gate"], entity["decisions"]["best_gate"]
    # JEV says 0.95, so no swept gate helps: the best gate is the one nearest the
    # shared gate. Decisions is right from 0.8 up, and 0.8 is the nearest of those.
    assert (jev["gate"], jev["accuracy"]) == (0.7, 0.0)
    assert (luna["gate"], luna["accuracy"]) == (0.8, 1.0)
    assert luna["correct"] == 4 and luna["in_sample"] is True
    assert table["tasks"]["triage"]["jev"]["best_gate"] is None


def test_a_lead_at_the_shared_gate_that_vanishes_at_the_best_gates_is_not_a_lead(
    tmp_path,
):
    # JEV is right on 8 of 8 false pairs at the shared gate 0.7 only because it says
    # 0.6; Decisions says 0.75 and is wrong at the shared gate, but right from gate
    # 0.8 up.
    deciders, _ = backends(tmp_path, yes(0.6), yes(0.75))
    table = bakeoff.evaluate(
        entity_cases_with([False] * 8),
        [],
        [],
        [],
        tmp_path / "kg.sqlite",
        Matching(),
        deciders,
        previous=None,
    )
    entity = table["tasks"]["same_entity"]
    assert entity["sign_test"]["p"] < 0.05 and entity["sign_test"]["jev_only"] == 8
    assert entity["sign_test_best_gate"]["p"] == 1.0
    assert entity["leader"] == "tie" and entity["verdict"] == "no clear difference"


def test_the_bias_note_names_the_caveats_and_the_latency_says_how_it_was_measured(
    tmp_path,
):
    deciders, _ = backends(tmp_path, yes(0.9), yes(0.2))
    kept = {"measured_at": "2026-10-28", "n": 30, "jev": {"p50": 0.4}}
    table = bakeoff.evaluate(
        [],
        [],
        [],
        [],
        tmp_path / "kg.sqlite",
        Matching(),
        deciders,
        previous={"latency": kept},
    )
    bias = table["bias"]
    for phrase in ("tuned on JEV", "same distribution", "one LLM judge"):
        assert phrase in bias
    assert "best gate" in bias and "in sample" in bias
    assert table["latency"]["jev"] == kept["jev"]
    caveat = table["latency"]["caveat"]
    assert "one run" in caveat and "30 triage asks" in caveat
    assert "not interleaved" in caveat


def test_the_timing_pass_carries_the_same_caveat(tmp_path):
    section = bakeoff.timing_pass(
        [({}, {"q": Noul("?")})],
        lambda backend, cache: type(
            "D", (), {"name": backend, "model": backend, "ask": lambda *a: {}}
        )(),
        tmp_path,
        today="2026-10-28",
    )
    assert section["caveat"] == bakeoff.LATENCY_CAVEAT


# --- the timing pass ------------------------------------------------------------------


def test_the_timing_pass_makes_fresh_calls_and_reports_p50_and_p95(
    tmp_path,
):
    ticks = iter(float(t) for t in range(1000))
    # each call to the clock moves it one second
    clock = lambda: next(ticks)  # noqa: E731
    calls = []

    def make(backend, cache):
        class Decider:
            name, model = backend, backend

            def ask(self, state, qs, purpose):
                calls.append((backend, state, cache.path))
                return {}

        return Decider()

    asks = [({"n": n}, {"q": Noul("?")}) for n in range(30)]
    section = bakeoff.timing_pass(asks, make, tmp_path, clock=clock, today="2026-10-28")
    assert section["n"] == 30 and section["measured_at"] == "2026-10-28"
    assert section["jev"] == {"p50": 1.0, "p95": 1.0, "mean": 1.0, "n": 30}
    assert len(calls) == 60 and {c[2].parent for c in calls} == {tmp_path}
    # each backend has a cache of its own, so no call can be answered by the other's
    assert len({c[2] for c in calls}) == 2


def test_the_timing_pass_stops_on_a_backend_that_fails_instead_of_timing_the_error(
    tmp_path,
):
    def make(backend, cache):
        class Decider:
            name = model = backend

            def ask(self, state, qs, purpose):
                raise NoDecisionBackend("down")

        return Decider()

    with pytest.raises(NoDecisionBackend):
        bakeoff.timing_pass(
            [({}, {"q": Noul("?")})], make, tmp_path, today="2026-10-28"
        )


def test_the_timing_asks_are_the_triage_asks_of_the_first_golden_items(tmp_path):
    from crew_stubs import FakeDecider

    open_crew_kg(tmp_path).close()
    fake = FakeDecider(lambda purpose, state, qs: triage_answers())
    items = [{"id": f"t{n}", "question": f"the press E217 {n}"} for n in range(5)]
    asks = bakeoff.triage_asks(items, tmp_path / "kg.sqlite", fake, count=3)
    assert len(asks) == 3 and fake.purposes() == ["triage"] * 3
    state, qs = asks[1]
    assert state["question"].endswith("E217 1") and "machine" in qs


def test_a_live_decider_needs_the_key_of_its_backend(tmp_path):
    cache = DecisionCache(tmp_path / "d.sqlite")
    with pytest.raises(NoDecisionBackend, match="jev"):
        bakeoff.live_decider("jev", cache, Settings(openai_api_key="a"))
    assert (
        bakeoff.live_decider("decisions", cache, Settings(openai_api_key="a")).model
        == LUNA
    )
    assert (
        bakeoff.live_decider("jev", cache, Settings(typesafe_api_key="b")).model == JEV
    )
