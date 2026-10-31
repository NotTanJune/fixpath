"""Live tests for the real models, the real embedding model and Langfuse.

They run only when the RUN_LIVE setting is 1. They write to the real spend ledger, so
the spend counts against the budget. The model cache is a temporary file, so the first
calls of each run are paid for. Later repeated calls replay from that cache. The whole
file costs well under 0.002 dollars. The embedding model downloads on first use.
"""

import os
import sqlite3
import time
import uuid
from contextlib import closing

import pytest
from langchain_core.globals import get_llm_cache, set_llm_cache
from pydantic import BaseModel

from app import config, embed, ledger, llm, llm_cache, store
from app.config import load_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="set RUN_LIVE=1 to call the real services",
)


class Alarm(BaseModel):
    alarm: str
    machine: str


SYSTEM = "Extract the alarm code and the machine ID from the operator note."
NOTE = "Operator note: E-214 tripped on CNC-01 during the night shift."
EXPECTED = Alarm(alarm="E-214", machine="CNC-01")
EFFORT = "low"  # the cheapest level this model accepts


@pytest.fixture(scope="module", autouse=True)
def temporary_cache(tmp_path_factory):
    """Use a temporary model cache for the whole file."""
    previous = get_llm_cache()
    llm_cache.enable_cache(tmp_path_factory.mktemp("live_cache") / "llm_cache.sqlite")
    yield
    set_llm_cache(previous)


def say(capsys, text: str) -> None:
    """Print a line so the run shows it, even though pytest hides output."""
    with capsys.disabled():
        print(f"\n  {text}")


def newest_id() -> int:
    ledger.spent_usd()  # make sure the ledger file exists
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()[0]


def rows_since(last_id: int) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM calls WHERE id > ? ORDER BY id", (last_id,)
        ).fetchall()


def describe(row: sqlite3.Row) -> str:
    return (
        f"ledger #{row['id']} model={row['model']} purpose={row['purpose']!r} "
        f"input={row['input_tokens']} cached={row['cached_input_tokens']} "
        f"output={row['output_tokens']} usd={row['usd']:.6f} "
        f"cache_hit={row['cache_hit']}"
    )


def check_ledger_row(row: sqlite3.Row, model: str, purpose: str) -> None:
    """The first call of a run is real, because the cache starts empty."""
    assert (row["model"], row["purpose"]) == (model, purpose)
    assert row["cache_hit"] == 0
    assert row["usd"] > 0
    assert row["input_tokens"] > 0
    assert row["output_tokens"] > 0


def test_sol_structured_call(capsys):
    before = newest_id()
    result = llm.structured("sol", Alarm, SYSTEM, NOTE, "live: sol", EFFORT)
    (row,) = rows_since(before)
    say(capsys, describe(row))
    assert result == EXPECTED
    check_ledger_row(row, config.SOL_MODEL, "live: sol")


def test_terra_structured_call(capsys):
    before = newest_id()
    result = llm.structured("terra", Alarm, SYSTEM, NOTE, "live: terra", EFFORT)
    (row,) = rows_since(before)
    say(capsys, describe(row))
    assert result == EXPECTED
    check_ledger_row(row, config.TERRA_MODEL, "live: terra")


def test_repeating_the_sol_call_is_a_cache_hit(capsys):
    first = llm.structured("sol", Alarm, SYSTEM, NOTE, "live: sol", EFFORT)
    spent = ledger.spent_usd()
    before = newest_id()

    second = llm.structured("sol", Alarm, SYSTEM, NOTE, "live: sol", EFFORT)

    (row,) = rows_since(before)
    say(capsys, describe(row))
    assert second == first == EXPECTED
    assert (row["cache_hit"], row["usd"]) == (1, 0)
    assert row["input_tokens"] > 0  # the replayed call keeps its token counts
    assert ledger.spent_usd() == spent  # and adds nothing to the spend


def test_local_embeddings_group_related_sentences(tmp_path, capsys):
    chiller_a = "The spindle chiller stopped because the coolant level was too low."
    chiller_b = "Low coolant in the chiller tripped the spindle alarm."
    unrelated = "The canteen serves chicken rice on Fridays."
    texts = [chiller_a, chiller_b, unrelated]

    vectors = embed.embed_passages(texts)

    assert vectors.shape == (3, embed.DIMENSIONS)
    assert str(vectors.dtype) == "float32"
    related = float(vectors[0] @ vectors[1])
    apart = [float(vectors[0] @ vectors[2]), float(vectors[1] @ vectors[2])]
    say(
        capsys,
        f"similarity related={related:.3f} unrelated={apart[0]:.3f}, {apart[1]:.3f}",
    )
    assert related > max(apart)

    # The same vectors work through the store.
    conn = store.connect(tmp_path / "kg.sqlite")
    try:
        store.init_schema(conn)
        store.upsert_document(conn, "DOC-0123abcd", "logs/a.log", "log", "ab" * 32)
        for order, (text, vector) in enumerate(zip(texts, vectors)):
            store.add_chunk(
                conn, f"c{order}", "DOC-0123abcd", None, order, text, vector
            )
        query = embed.embed_query("why did the chiller alarm trip")
        assert query.shape == (embed.DIMENSIONS,)
        hits = store.vector_search(conn, query, 3)
    finally:
        conn.close()
    assert {hit["text"] for hit in hits[:2]} == {chiller_a, chiller_b}
    assert hits[-1]["text"] == unrelated


def test_a_langfuse_trace_is_sent(capsys):
    settings = load_settings()
    if not settings.has_langfuse:
        pytest.skip("no Langfuse keys in .env")
    from langfuse import get_client, propagate_attributes

    session = f"live-test-{uuid.uuid4().hex[:12]}"
    # The cache already holds this answer, so the call costs nothing. Langfuse
    # still records it.
    with propagate_attributes(session_id=session):
        llm.structured("sol", Alarm, SYSTEM, NOTE, "live: trace", EFFORT)

    client = get_client()
    assert client.auth_check(), "Langfuse rejected the keys in .env"
    client.flush()  # send what is waiting, as a short script must before it exits

    started = time.monotonic()
    seen: list = []
    while time.monotonic() - started < 60 and not seen:
        seen = client.api.observations.get_many(session_id=session, limit=50).data
        if not seen:
            time.sleep(3)
    say(
        capsys,
        f"langfuse: flushed; {len(seen)} observation(s) visible on {settings.langfuse_host} "
        f"for session {session} after {time.monotonic() - started:.0f}s",
    )
    assert seen, "the trace was flushed but did not show up within 60 seconds"
