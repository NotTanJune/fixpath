"""Checks that a run uses the settings it was given for every model call, so a run
without keys never picks up keys from the environment or from .env."""

import socket

import pytest
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)
from load_stubs import FakeEmbedder
from resolve_stubs import ScriptedDecider

from app import config, llm
from app.config import Settings
from app.ingest import graph
from app.llm import MissingApiKey

HANDOVER = (
    "Shift handover 22 Aug B\n"
    "HP1 stopped with E217, oil under the ram. Seals replaced by Arun.\n"
)


@pytest.fixture
def network(monkeypatch):
    """Every attempt to open a connection is recorded and refused."""
    attempts = []

    def refuse(self, address, *args, **kwargs):
        attempts.append(address)
        raise AssertionError(f"network use: {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(
        socket.socket, "connect_ex", lambda self, address: attempts.append(address) or 1
    )
    return attempts


def test_keyless_settings_stop_an_uncached_extraction_even_when_the_environment_has_a_key(
    tmp_path, monkeypatch, network
):
    monkeypatch.setattr(
        llm, "load_settings", config.load_settings
    )  # turn the test isolation off
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-not-real")
    assert config.load_settings().has_openai  # a key the run must not use
    raw = tmp_path / "raw"
    (raw / "handovers").mkdir(parents=True)
    (raw / "handovers" / "handover_2026-08-22_B.txt").write_text(
        HANDOVER, encoding="utf-8"
    )
    with pytest.raises(MissingApiKey):
        list(
            graph.stream_ingest(
                raw_dir=raw,
                kg_path=tmp_path / "out" / "kg.sqlite",
                settings=Settings(),
                decider=ScriptedDecider(lambda purpose, state: 0.0),
                embedder=FakeEmbedder(),
            )
        )
    assert network == []
