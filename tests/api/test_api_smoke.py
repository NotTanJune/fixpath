"""Smoke test of the API on the committed data. In replay mode with no keys, every
suggested question must stream to the end from the committed caches, with no paid call.

It works on copies of the caches and a scratch ledger, so a question that was not warmed
fails instead of making a request. The manual search needs the local embedding model, and
the test is skipped on a machine that has not downloaded it.
"""

import pytest
from api_stubs import parse_events
from fastapi.testclient import TestClient

from app.api import suggestions
from app.api.app import create_app
from app.config import KG_PATH, Settings
from app.embed import MODEL_DIR

pytestmark = pytest.mark.skipif(
    not MODEL_DIR.is_dir(), reason="the local embedding model is not downloaded"
)


@pytest.fixture(scope="module")
def client():
    with suggestions.scratch_state():
        with TestClient(create_app(Settings(), KG_PATH)) as client:
            yield client


def test_every_suggested_question_is_replayable_from_the_committed_caches(client):
    found = client.get("/api/suggestions").json()
    assert [s["question"] for s in found] == suggestions.QUESTIONS
    assert [s["replayable"] for s in found] == [True] * len(suggestions.QUESTIONS)


@pytest.mark.parametrize("question", suggestions.QUESTIONS)
def test_a_suggested_question_streams_to_done_with_no_paid_call(client, question):
    response = client.post("/api/ask", json={"question": question, "history": []})
    events = parse_events(response.text)
    names = [name for name, _ in events]
    assert "error" not in names, events[-1]
    assert names[-3:] == ["answer", "graph_path", "done"]
    done = events[-1][1]
    assert done["cost_usd"] == 0.0 and done["cache_hits"] == done["calls"] > 0
    card = events[-3][1]
    assert card["question"] == question and card["triage"]["status"] in (
        "act",
        "confirm",
        "clarify",
    )
