"""Tests of the ask endpoint, which streams the crew's work to the client as events.
The crew and its models are scripted, and the data is the small test graph.
"""

import json
import threading

import pytest
from api_stubs import NO_KEYS, QUESTION, make_app, parse_events, scripted_crew
from ask_stubs import FakeEmbedder, plan_model
from crew_stubs import FakeDecider, RaisingModel, choice, sol_model, triage_answers
from fastapi.sse import ServerSentEvent
from fastapi.testclient import TestClient
from llm_stubs import SETTINGS, TERRA, isolated  # noqa: F401  (an autouse fixture)
from llm_stubs import rows as ledger_rows

from app import ledger
from app.api import stream
from app.api.stream import CACHE_MISS
from app import llm
from app.ask import crew
from app.ask.crew import Models
from app.config import Settings
from app.decide import NoDecisionBackend
from app.decide.base import CacheOnlyDecider
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey


@pytest.fixture
def client(tmp_path):
    return TestClient(make_app(tmp_path))


def ask(client, question=QUESTION, history=None):
    response = client.post(
        "/api/ask", json={"question": question, "history": history or []}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_events(response.text)


def names(events):
    return [name for name, _ in events]


def test_on_act_the_events_come_in_the_order_of_the_brief(client, monkeypatch):
    scripted_crew(monkeypatch)
    events = ask(client)
    seen = names(events)
    assert seen[:2] == ["node_start", "node_end"]
    assert [data["node"] for name, data in events[:2]] == ["triage", "triage"]
    assert seen[-3:] == ["answer", "graph_path", "done"]
    assert seen.count("answer") == seen.count("done") == 1 and "clarify" not in seen
    # each step starts once, ends once, and starts before it ends
    for node in (
        "triage",
        "graph_agent",
        "manual_agent",
        "synthesize",
        "verify",
        "assemble",
    ):
        at = [
            i
            for i, (n, d) in enumerate(events)
            if n.startswith("node_") and d["node"] == node
        ]
        assert [events[i][0] for i in at] == ["node_start", "node_end"], node
    ends = {d["node"]: i for i, (n, d) in enumerate(events) if n == "node_end"}
    starts = {d["node"]: i for i, (n, d) in enumerate(events) if n == "node_start"}
    assert ends["triage"] < min(starts["graph_agent"], starts["manual_agent"])
    assert max(ends["graph_agent"], ends["manual_agent"]) < starts["synthesize"]
    assert ends["synthesize"] < starts["verify"] < ends["verify"] < starts["assemble"]
    assert ends["assemble"] == seen.index("answer") - 1


def test_a_node_end_has_the_time_backend_cache_flag_and_cost(client, monkeypatch):
    scripted_crew(monkeypatch)
    ends = {d["node"]: d for n, d in ask(client) if n == "node_end"}
    verify = ends["verify"]
    assert set(verify) >= {
        "node",
        "started_at",
        "ended_at",
        "seconds",
        "backend",
        "cache_hit",
        "cost_usd",
        "notes",
    }
    assert verify["started_at"].endswith("Z") or "+" in verify["started_at"]
    assert ends["assemble"]["backend"] is None  # no model call


def test_the_triage_node_end_carries_the_decisions_and_confidences(client, monkeypatch):
    scripted_crew(monkeypatch)
    events = ask(client)
    end = next(d for n, d in events if n == "node_end" and d["node"] == "triage")
    assert end["status"] == "act"
    assert end["decisions"] == {
        "machine": "HP-01",
        "alarm": "E-217",
        "root_cause": "FM-SEAL-WEAR",
        "intent": "troubleshoot",
        "safety_critical": 0.1,
    }
    assert end["confidence"]["machine"] == 0.97 and end["confidence"]["alarm"] == 0.95
    other = next(d for n, d in events if n == "node_end" and d["node"] == "verify")
    assert "decisions" not in other


def test_each_graph_tool_call_is_an_event_after_the_graph_agent_ends(
    client, monkeypatch
):
    scripted_crew(monkeypatch)
    events = ask(client)
    calls = [(i, d) for i, (n, d) in enumerate(events) if n == "tool_call"]
    assert [d["name"] for _, d in calls][0] == "alarm_causes"
    assert {"name", "args", "filled_by", "result_count"} <= set(calls[0][1])
    ended = next(
        i
        for i, (n, d) in enumerate(events)
        if n == "node_end" and d["node"] == "graph_agent"
    )
    started = next(
        i
        for i, (n, d) in enumerate(events)
        if n == "node_start" and d["node"] == "synthesize"
    )
    assert all(ended < i < started for i, _ in calls)


def test_the_answer_is_the_card_and_graph_path_its_steps(client, monkeypatch):
    scripted_crew(monkeypatch)
    events = dict(
        (n, d) for n, d in ask(client) if n in ("answer", "graph_path", "done")
    )
    card = events["answer"]
    assert card["question"] == QUESTION and card["triage"]["status"] == "act"
    assert card["causes"][0]["failure_mode_id"] == "FM-SEAL-WEAR"
    assert card["fix"]["procedure_id"] == "PROC-HP-04" and card["safety"]["required"]
    assert [t["name"] for t in card["trace"]][-1] == "assemble"
    assert events["graph_path"] == {"path": card["path"]} and card["path"]
    assert {"src", "rel", "dst"} == set(card["path"][0])


def test_done_has_the_cost_and_the_cache_hits_of_the_run(client, monkeypatch):
    scripted_crew(monkeypatch)
    done = next(d for n, d in ask(client) if n == "done")
    assert set(done) == {"cost_usd", "calls", "cache_hits"}
    assert done["calls"] == 4 and done["cache_hits"] == 0 and done["cost_usd"] > 0
    assert done["cost_usd"] == pytest.approx(ledger.spent_usd())


def test_a_clarify_sends_the_chips_and_the_reason_then_a_short_card(
    client, monkeypatch
):
    answers = triage_answers(
        machine=choice("unknown", 0.9, {"RW-01": 0.06, "CNC-01": 0.02, "HP-01": 0.01})
    )
    scripted_crew(monkeypatch, triage=answers)
    events = ask(client, "Line 4 robot is down")
    assert names(events) == [
        "node_start",
        "node_end",
        "clarify",
        "node_start",
        "node_end",
        "answer",
        "graph_path",
        "done",
    ]
    clarify = events[2][1]
    assert [c["value"] for c in clarify["chips"]] == ["RW-01", "CNC-01", "HP-01"]
    assert clarify["reason"]
    assert events[1][1]["status"] == "clarify"
    answer = events[5][1]
    assert answer["causes"] == [] and answer["triage"]["status"] == "clarify"
    assert events[6] == ("graph_path", {"path": []})


def test_out_of_scope_stops_after_triage_without_a_clarify(client, monkeypatch):
    scripted_crew(
        monkeypatch, triage=triage_answers(intent=choice("out_of_scope", 0.95))
    )
    events = ask(client, "Who is on the night shift roster?")
    assert "clarify" not in names(events)
    assert names(events)[-3:] == ["answer", "graph_path", "done"]
    assert events[-3][1]["triage"]["status"] == "out_of_scope"
    assert events[1][1]["status"] == "out_of_scope"


def test_the_history_reaches_the_crew_as_role_prefixed_turns(client, monkeypatch):
    runs = scripted_crew(monkeypatch)
    ask(
        client,
        QUESTION,
        [
            {"role": "technician", "text": "it is the press"},
            {"role": "assistant", "text": "which alarm?"},
        ],
    )
    assert runs == [
        (QUESTION, ["technician: it is the press", "assistant: which alarm?"])
    ]


def test_the_done_totals_count_the_crew_purposes_and_not_an_ingest_running_beside_it(
    client, monkeypatch
):
    runs = scripted_crew(monkeypatch)
    real = crew.stream_ask

    def with_an_ingest_beside(question, history=None, **kwargs):
        ledger.record(TERRA, "extract note", 1000, 0, 100, False)  # paid for by an ingest
        return real(question, history, **kwargs)

    monkeypatch.setattr(crew, "stream_ask", with_an_ingest_beside)
    done = next(d for n, d in ask(client) if n == "done")
    assert runs and done["calls"] == 4  # the same four calls as without the ingest
    rows = [r for r in ledger_rows() if r["purpose"] != "extract note"]
    assert done["cost_usd"] == pytest.approx(sum(r["usd"] for r in rows))


def test_the_client_sees_no_exception_text_in_a_node_end_or_in_the_card_trace(
    client, monkeypatch
):
    broken = Models(
        graph=RaisingModel(
            model=TERRA,
            callbacks=[llm.CostTracker(SETTINGS)],
            error=RuntimeError("secret internals sk-123"),
        ),
        manual=plan_model(["seal kit"]),
        synth=sol_model("Seals wear first.", []),
        embedder=FakeEmbedder(),
    )
    scripted_crew(monkeypatch, models=broken)
    events = ask(client)
    graph_end = next(
        d for n, d in events if n == "node_end" and d["node"] == "graph_agent"
    )
    assert graph_end["notes"] == ["this step failed"]
    card = next(d for n, d in events if n == "answer")
    traced = next(t for t in card["trace"] if t["name"] == "graph_agent")
    assert traced["notes"] == ["this step failed"]
    everything = json.dumps(events)
    assert "RuntimeError" not in everything and "sk-123" not in everything
    assert "internals" not in everything


def test_a_note_that_is_not_an_exception_is_kept():
    notes = ['dropped a claim that cites no source: "x"', "the agent was still asking"]
    assert stream.public_notes(notes) == notes
    assert stream.public_notes(
        [
            "filled by code after the graph agent failed: the read failed with "
            "OperationalError: database is locked",
            "ValueError: bad",
        ]
    ) == ["this step failed", "this step failed"]


# --- failures ---


def only_error(events):
    assert names(events)[-1] == "error" and "done" not in names(events)
    assert names(events).count("error") == 1
    return events[-1][1]["message"]


@pytest.mark.parametrize(
    "error", [NoDecisionBackend("nothing cached"), MissingApiKey("no key")]
)
def test_a_cache_miss_with_no_keys_is_an_error_event_and_the_stream_ends(
    client, monkeypatch, error
):
    def script(purpose, state, questions):
        return error

    scripted_crew(monkeypatch, decider=FakeDecider(script))
    message = only_error(ask(client, "something not cached"))
    assert (
        message
        == CACHE_MISS
        == (
            "This question is not in the replay cache; ask one of the suggested questions or add keys"
        )
    )


def test_the_budget_error_names_the_budget(client, monkeypatch):
    def script(purpose, state, questions):
        return BudgetExceeded("model spend of $1.2000 has reached the $1.00 budget")

    scripted_crew(monkeypatch, decider=FakeDecider(script))
    message = only_error(ask(client))
    assert "budget" in message and "$1.00" in message


def test_a_missing_database_is_a_plain_error_event(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    scripted_crew(monkeypatch)
    (tmp_path / "data" / "kg.sqlite").unlink()
    assert "no graph database" in only_error(ask(client))


def test_an_unexpected_failure_says_so_plainly_and_leaks_nothing(client, monkeypatch):
    def script(purpose, state, questions):
        return RuntimeError("secret internals sk-123")

    scripted_crew(monkeypatch, decider=FakeDecider(script))
    message = only_error(ask(client))
    assert "sk-123" not in message and "internals" not in message and message


def test_a_question_over_500_characters_or_empty_is_a_422(client):
    assert client.post("/api/ask", json={"question": "x" * 501}).status_code == 422
    assert client.post("/api/ask", json={"question": ""}).status_code == 422
    assert client.post("/api/ask", json={}).status_code == 422


def test_a_question_of_exactly_500_characters_is_accepted(client, monkeypatch):
    scripted_crew(monkeypatch)
    assert names(ask(client, "x" * 500))[-1] in ("done", "error")


# --- one question at a time ---


def fake_chunks(held, line):
    def fake(question, history=None, **kwargs):
        yield {
            "type": "custom",
            "ns": (),
            "data": {"event": "node_start", "node": "triage", "at": "t"},
        }
        held.append(len(line._places))

    return fake


def test_a_worker_holds_its_turn_from_the_first_event_to_the_last(monkeypatch):
    line, held = stream.Line(3), []
    monkeypatch.setattr(crew, "stream_ask", fake_chunks(held, line))
    place = line.join()
    job = stream.launch(
        line, place, lambda: stream.ask_events("q", [], None, NO_KEYS), "failed"
    )
    items = []
    while (item := job.out.get(timeout=5)) is not stream.DONE:
        items.append(item)
    assert isinstance(items[0], ServerSentEvent) and items[0].event == "node_start"
    assert held == [1]  # it held a place in the line while the crew ran
    assert not line._places  # and the place is freed once the run is done


def test_a_worker_closes_the_events_calls_after_and_leaves_when_stopped(monkeypatch):
    line, closed, after = stream.Line(3), [], []

    def events():
        try:
            for number in range(100):
                yield stream.event("tick", number)
        finally:
            closed.append(True)

    job = stream.launch(line, line.join(), events, "failed", lambda: after.append(True))
    job.stop.set()
    while job.out.get(timeout=5) is not stream.DONE:
        pass
    assert closed == [True] and after == [True] and not line._places


def test_a_run_that_is_stopped_while_it_waits_never_starts_but_still_cleans_up():
    line, made, after = stream.Line(3), [], []
    first = line.join()  # holds the turn
    job = stream.launch(
        line,
        line.join(),
        lambda: made.append(True) or iter(()),
        "failed",
        lambda: after.append(True),
    )
    assert job.out.get(timeout=5).event == "queued"
    job.stop.set()
    while job.out.get(timeout=5) is not stream.DONE:
        pass
    assert made == [] and after == [True]
    line.leave(first)
    assert not line._places


def test_an_error_outside_the_events_is_one_error_event_and_the_turn_is_released():
    line = stream.Line(3)

    def broken():
        raise RuntimeError("internals")
        yield  # pragma: no cover

    job = stream.launch(line, line.join(), broken, "plain failure")
    items = []
    while (item := job.out.get(timeout=5)) is not stream.DONE:
        items.append(item)
    assert [(i.event, i.data) for i in items] == [
        ("error", {"message": "plain failure"})
    ]
    assert not line._places


def test_the_line_runs_in_the_order_of_joining_and_holds_three_waiting():
    line = stream.Line(3)
    places = [line.join() for _ in range(4)]  # one runs, three wait
    assert all(places) and line.join() is None
    stop, seen = threading.Event(), []
    done = []

    def wait(index):
        done.append((index, line.wait_turn(places[index], stop, seen.append)))

    threads = [threading.Thread(target=wait, args=(i,)) for i in (3, 2, 1)]
    for thread in threads:
        thread.start()
    assert line.wait_turn(places[0], stop, seen.append) is True  # the head runs at once
    line.leave(places[0])
    threads[2].join(5)  # the second place is next, whatever order the threads began in
    assert done == [(1, True)]
    stop.set()
    for thread in threads:
        thread.join(5)
    assert sorted(done) == [(1, True), (2, False), (3, False)]
    assert 3 in seen and 2 in seen  # each waiting question was told its position


def test_a_question_of_spaces_is_a_422_and_a_question_is_stripped(client, monkeypatch):
    runs = scripted_crew(monkeypatch)
    assert client.post("/api/ask", json={"question": "   \t "}).status_code == 422
    assert names(ask(client, f"  {QUESTION} \n"))[-1] == "done"
    assert runs == [(QUESTION, [])]
    over = "x" * 500 + " "
    # cut to 500 characters, so it is accepted
    assert client.post("/api/ask", json={"question": over}).status_code == 200
    assert client.post("/api/ask", json={"question": "x" * 501}).status_code == 422


@pytest.mark.parametrize("role", ["system", "Technician", "", "x" * 41])
def test_a_history_role_is_technician_or_assistant_and_nothing_else(client, role):
    turns = [{"role": role, "text": "hello"}]
    response = client.post("/api/ask", json={"question": QUESTION, "history": turns})
    assert response.status_code == 422


def test_a_second_question_waits_for_the_first(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    release, order = threading.Event(), []

    def fake(question, history=None, **kwargs):
        order.append(("start", question))
        if question == "first":
            release.wait(10)
        order.append(("end", question))
        return iter(())

    monkeypatch.setattr(crew, "stream_ask", fake)

    def post(question):
        TestClient(app).post("/api/ask", json={"question": question})

    first = threading.Thread(target=post, args=("first",))
    second = threading.Thread(target=post, args=("second",))
    first.start()
    while ("start", "first") not in order:
        pass
    second.start()
    second.join(0.5)
    assert order == [("start", "first")]  # the second has not begun
    release.set()
    first.join(10)
    second.join(10)
    assert order == [
        ("start", "first"),
        ("end", "first"),
        ("start", "second"),
        ("end", "second"),
    ]


# --- replay mode never pays ---


TYPESAFE_ONLY = Settings(typesafe_api_key="ts-test-secret-value")
LIVE = Settings(
    openai_api_key=SETTINGS.openai_api_key, typesafe_api_key="ts-test-secret-value"
)


def recorded_deciders(monkeypatch):
    """Record the decider of every crew run the API starts."""
    seen = []
    real = crew.stream_ask

    def stream_ask(question, history=None, **kwargs):
        seen.append(kwargs.get("decider"))
        kwargs.update(decider=FakeDecider(lambda p, s, q: NoDecisionBackend("miss")))
        return real(question, history, **kwargs)

    monkeypatch.setattr(crew, "stream_ask", stream_ask)
    return seen


def test_replay_mode_with_only_a_typesafe_key_asks_the_cache_only_decider(
    tmp_path, monkeypatch
):
    # without an OpenAI key the app is in replay mode, so a TypeSafe key alone must never make a call
    seen = recorded_deciders(monkeypatch)
    client = TestClient(make_app(tmp_path, TYPESAFE_ONLY))
    ask(client)
    (decider,) = seen
    assert isinstance(decider, CacheOnlyDecider)
    assert get_json_health(client)["decider"] == "cache"


def test_live_mode_leaves_the_decider_to_the_settings(tmp_path, monkeypatch):
    seen = recorded_deciders(monkeypatch)
    client = TestClient(make_app(tmp_path, LIVE))
    ask(client)
    assert seen == [None]
    assert get_json_health(client)["decider"] == "jev"


def get_json_health(client):
    return client.get("/api/health").json()
