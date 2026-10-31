"""Checks that the real raw folder can be ingested again without any keys.

The ingestion runs from saved model answers and must build the same graph as the one
committed in data/. It works on copies, so it never changes anything in data/.
"""

import hashlib
import shutil
import sqlite3
from contextlib import closing

import pytest

from app import config, ledger, llm, llm_cache
from app.config import RAW_DIR, Settings
from app.decide import base as decide_base
from app.decide import NoDecisionBackend
from app.ingest import graph
from load_stubs import FakeEmbedder

COMMITTED_KG = config.KG_PATH
COMMITTED_CACHES = (llm_cache.CACHE_PATH, decide_base.CACHE_PATH)
ADVICE = (
    "The committed caches or data/kg.sqlite do not match the current code. "
    "Run `make ingest` with keys, then commit data/cache/, data/kg.sqlite and "
    "data/pipeline/latest.json."
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(path):
    with closing(sqlite3.connect(path)) as conn:
        return {
            "nodes": dict(
                conn.execute("SELECT type, count(*) FROM nodes GROUP BY type")
            ),
            "edges": dict(conn.execute("SELECT rel, count(*) FROM edges GROUP BY rel")),
        }


@pytest.fixture
def keyless(tmp_path, monkeypatch):
    """No keys anywhere, copies of the committed caches, and a throwaway ledger."""
    for name in (
        "OPENAI_API_KEY",
        "TYPESAFE_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    caches = tmp_path / "caches"
    caches.mkdir()
    for source in COMMITTED_CACHES:
        shutil.copy(source, caches / source.name)
    monkeypatch.setattr(llm_cache, "CACHE_PATH", caches / "llm_cache.sqlite")
    monkeypatch.setattr(decide_base, "CACHE_PATH", caches / "decisions.sqlite")
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")
    monkeypatch.setattr(llm, "load_settings", lambda: Settings())
    monkeypatch.setattr(graph, "load_settings", lambda: Settings())
    yield


def test_the_real_raw_folder_replays_from_the_committed_caches_with_no_keys(
    keyless, tmp_path
):
    assert COMMITTED_KG.is_file(), ADVICE
    before = {path: digest(path) for path in COMMITTED_CACHES}
    kg = tmp_path / "replay" / "kg.sqlite"
    try:
        state = graph.run_full(RAW_DIR, kg, Settings(), embedder=FakeEmbedder())
    except (llm.MissingApiKey, NoDecisionBackend) as error:
        pytest.fail(
            f"a model or decision call was not in the caches ({error}). {ADVICE}"
        )

    assert counts(kg) == counts(COMMITTED_KG), ADVICE
    cost = state["report"]["totals"]["cost"]
    assert cost["paid_calls"] == 0 and cost["usd"] == 0
    assert (kg.parent / "pipeline" / "latest.json").is_file()
    assert {path: digest(path) for path in COMMITTED_CACHES} == before
