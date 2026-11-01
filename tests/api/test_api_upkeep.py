"""Test of the upkeep route on the committed graph."""

from api_stubs import NO_KEYS
from fastapi.testclient import TestClient
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.api.app import create_app
from app.config import KG_PATH


def test_upkeep_serves_the_predictions():
    client = TestClient(create_app(NO_KEYS, KG_PATH, replay_check=lambda q: False))
    data = client.get("/api/upkeep").json()
    assert data["as_of"] == "2026-09-28" and data["items"] and data["terms"]
