"""Tests for the cache of saved model answers: storing and replaying an answer,
refreshing it, and what a model returns when the answer is already saved.

No test uses the network. Calls go to a stand-in chat model, and every cache and
ledger is a temporary file.
"""

import asyncio
import json
import logging
import sqlite3
from contextlib import closing

import pytest
from ask_stubs import calls, say, terra_model
from langchain_core.caches import BaseCache
from langchain_core.globals import get_llm_cache
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, Generation
from llm_stubs import (
    REPLY,
    TIGHT,
    Pair,
    Spy,
    StubChatModel,
    gated,
    generation,
    isolated,  # noqa: F401  (an autouse fixture)
    rows,
    run_config,
    spend_the_budget,
)

from app import config, ledger, llm, llm_cache

# Read before any fixture redirects them.
DEFAULT_CACHE_PATH = llm_cache.CACHE_PATH
DEFAULT_LEDGER_PATH = ledger.LEDGER_PATH

# What used to live in app.llm. It does not come back through a re-export.
MOVED_NAMES = (
    "CACHE_PATH",
    "CACHE_HIT_KEY",
    "SQLiteLLMCache",
    "enable_cache",
    "refresh_cache",
    "is_cache_hit",
    "_CACHE_SCHEMA",
    "_encode",
    "_decode",
    "_refreshing",
)


# --- the cache --------------------------------------------------------------


def test_the_cache_file_lives_beside_the_ledger():
    assert DEFAULT_CACHE_PATH == config.CACHE_DIR / "llm_cache.sqlite"
    assert DEFAULT_CACHE_PATH.parent == DEFAULT_LEDGER_PATH.parent


def test_the_cache_lives_in_llm_cache_and_app_llm_does_not_re_export_it():
    assert [name for name in MOVED_NAMES if not hasattr(llm_cache, name)] == []
    assert [name for name in MOVED_NAMES if hasattr(llm, name)] == []


def test_a_lookup_that_finds_nothing_returns_none(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    assert cache.lookup("prompt", "llm") is None


def test_a_stored_generation_comes_back_marked_as_a_hit(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    stored = [
        generation(
            "answer",
            usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            response_metadata={"model_name": "m"},
            id="resp_1",
        )
    ]
    cache.update("prompt", "llm", stored)

    (hit,) = cache.lookup("prompt", "llm")
    assert isinstance(hit, ChatGeneration)
    assert hit.message.content == "answer"
    assert hit.message.usage_metadata == stored[0].message.usage_metadata
    assert hit.message.response_metadata == {"model_name": "m"}
    assert hit.message.id == "resp_1"
    assert hit.generation_info == {llm_cache.CACHE_HIT_KEY: True}
    assert llm_cache.is_cache_hit(hit)


def test_update_does_not_mark_the_generations_it_was_given(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    stored = [generation("answer")]
    cache.update("prompt", "llm", stored)
    cache.lookup("prompt", "llm")
    assert stored[0].generation_info is None
    assert not llm_cache.is_cache_hit(stored[0])


def test_an_existing_generation_info_is_kept_beside_the_marker(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    stored = [ChatGeneration(message=AIMessage(content="x"), generation_info={"a": 1})]
    cache.update("prompt", "llm", stored)
    (hit,) = cache.lookup("prompt", "llm")
    assert hit.generation_info == {"a": 1, llm_cache.CACHE_HIT_KEY: True}


def test_the_key_is_the_pair_of_prompt_and_llm_string(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("prompt", "llm", [generation("one")])
    cache.update("prompt", "other llm", [generation("two")])
    cache.update("other prompt", "llm", [generation("three")])
    assert cache.lookup("prompt", "llm")[0].message.content == "one"
    assert cache.lookup("prompt", "other llm")[0].message.content == "two"
    assert cache.lookup("other prompt", "llm")[0].message.content == "three"
    assert cache.lookup("prompt", "third llm") is None


def test_the_two_strings_of_the_key_cannot_run_into_each_other(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("y\x00z", "x", [generation("one")])
    # Joined with a bare NUL, both pairs would read x NUL y NUL z.
    assert cache.lookup("z", "x\x00y") is None


def test_updating_a_key_again_replaces_the_stored_answer(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("prompt", "llm", [generation("old")])
    cache.update("prompt", "llm", [generation("new")])
    assert cache.lookup("prompt", "llm")[0].message.content == "new"


def test_the_cache_persists_in_its_file_between_instances(tmp_path):
    path = tmp_path / "deep" / "c.sqlite"
    llm_cache.SQLiteLLMCache(path).update("prompt", "llm", [generation("kept")])
    assert (
        llm_cache.SQLiteLLMCache(path).lookup("prompt", "llm")[0].message.content
        == "kept"
    )


def test_the_cache_keeps_the_prompt_and_llm_string_for_inspection(tmp_path):
    path = tmp_path / "c.sqlite"
    llm_cache.SQLiteLLMCache(path).update("the prompt", "the llm", [generation("x")])
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT prompt, llm_string FROM llm_cache").fetchall() == [
            ("the prompt", "the llm")
        ]


def test_clear_empties_the_cache(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("prompt", "llm", [generation("x")])
    cache.clear()
    assert cache.lookup("prompt", "llm") is None


def test_plain_text_generations_round_trip_too(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("p", "l", [Generation(text="plain", generation_info={"n": 1})])
    (hit,) = cache.lookup("p", "l")
    assert (hit.text, hit.generation_info) == (
        "plain",
        {"n": 1, llm_cache.CACHE_HIT_KEY: True},
    )


def test_a_parsed_pydantic_object_is_stored_as_a_dict_the_parser_accepts(tmp_path):
    parsed = Pair(alarm="E-214", machine="CNC-01")
    stored = [generation(REPLY, additional_kwargs={"parsed": parsed})]
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("prompt", "llm", stored)

    (hit,) = cache.lookup("prompt", "llm")
    assert hit.message.additional_kwargs["parsed"] == {
        "alarm": "E-214",
        "machine": "CNC-01",
    }
    assert Pair(**hit.message.additional_kwargs["parsed"]) == parsed
    assert stored[0].message.additional_kwargs["parsed"] is parsed  # not changed


def test_a_response_that_cannot_be_stored_is_skipped_with_a_warning(tmp_path, caplog):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    bad = [generation("x", additional_kwargs={"handle": object()})]
    with caplog.at_level(logging.WARNING, logger="app.llm_cache"):
        cache.update("prompt", "llm", bad)  # must not raise: the call is paid for
    assert "cache" in caplog.text
    assert cache.lookup("prompt", "llm") is None


def test_an_unreadable_entry_counts_as_a_miss(tmp_path, caplog):
    path = tmp_path / "c.sqlite"
    cache = llm_cache.SQLiteLLMCache(path)
    cache.update("prompt", "llm", [generation("x")])
    with closing(sqlite3.connect(path)) as conn, conn:  # conn commits the change
        conn.execute("UPDATE llm_cache SET generations = 'not json'")
    with caplog.at_level(logging.WARNING, logger="app.llm_cache"):
        assert cache.lookup("prompt", "llm") is None
    assert "cache" in caplog.text


def test_enable_cache_installs_the_sqlite_cache_globally(tmp_path):
    cache = llm_cache.enable_cache(tmp_path / "c.sqlite")
    assert isinstance(cache, llm_cache.SQLiteLLMCache)
    assert isinstance(cache, BaseCache)
    assert get_llm_cache() is cache
    assert cache.path == tmp_path / "c.sqlite"


def test_enable_cache_uses_the_default_cache_file(tmp_path):
    # The isolated fixture points the default at tmp_path.
    assert llm_cache.enable_cache().path == tmp_path / "llm_cache.sqlite"


# --- refreshing a cached answer ----------------------------------------------


def test_refresh_cache_makes_lookup_miss_and_leaves_the_stored_entry_alone(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("prompt", "llm", [generation("kept")])
    with llm_cache.refresh_cache():
        assert cache.lookup("prompt", "llm") is None
    assert cache.lookup("prompt", "llm")[0].message.content == "kept"


def test_refresh_cache_nests_and_ends_even_when_the_block_raises(tmp_path):
    cache = llm_cache.SQLiteLLMCache(tmp_path / "c.sqlite")
    cache.update("prompt", "llm", [generation("kept")])
    with llm_cache.refresh_cache():
        with llm_cache.refresh_cache():
            assert cache.lookup("prompt", "llm") is None
        assert cache.lookup("prompt", "llm") is None  # the outer block still holds
    assert cache.lookup("prompt", "llm") is not None
    with pytest.raises(RuntimeError, match="boom"), llm_cache.refresh_cache():
        raise RuntimeError("boom")
    assert cache.lookup("prompt", "llm") is not None


def test_refresh_cache_sends_an_invoke_to_the_model_and_overwrites_the_answer(
    tmp_path,
):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = gated(reply="bad answer")
    stub.invoke("q", config=run_config())
    assert stub.invoke("q", config=run_config()).content == "bad answer"  # replayed
    assert len(stub.seen) == 1
    stub.reply = "good answer"

    with llm_cache.refresh_cache():
        refreshed = stub.invoke("q", config=run_config())

    assert refreshed.content == "good answer"
    assert len(stub.seen) == 2
    assert stub.invoke("q", config=run_config()).content == "good answer"
    assert len(stub.seen) == 2  # outside the block the new entry replays
    assert [(row["cache_hit"], row["usd"] > 0) for row in rows()] == [
        (0, True),
        (1, False),
        (0, True),  # the refresh is a real call and is billed
        (1, False),
    ]


def test_refresh_cache_sends_an_ainvoke_to_the_model_and_overwrites_the_answer(
    tmp_path,
):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = gated(reply="bad answer")

    async def scenario():
        await stub.ainvoke("q", config=run_config())
        assert (await stub.ainvoke("q", config=run_config())).content == "bad answer"
        stub.reply = "good answer"
        with llm_cache.refresh_cache():  # alookup runs lookup on a worker thread
            refreshed = await stub.ainvoke("q", config=run_config())
        replayed = await stub.ainvoke("q", config=run_config())
        return refreshed.content, replayed.content

    assert asyncio.run(scenario()) == ("good answer", "good answer")
    assert len(stub.seen) == 2
    assert [row["cache_hit"] for row in rows()] == [0, 1, 0, 1]


def test_a_refresh_still_goes_through_the_gate(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = gated(TIGHT)
    stub.invoke("q", config=run_config())
    spend_the_budget()
    with llm_cache.refresh_cache(), pytest.raises(ledger.BudgetExceeded):
        stub.invoke("q", config=run_config())
    assert len(stub.seen) == 1
    assert stub.invoke("q", config=run_config()).content == REPLY  # entry survived


# --- cache hits through a model ---------------------------------------------


def test_the_second_identical_call_is_answered_from_the_cache(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = StubChatModel()
    first = stub.invoke("same question")
    second = stub.invoke("same question")
    assert len(stub.seen) == 1  # the model ran once
    assert second.content == first.content == REPLY
    stub.invoke("another question")
    assert len(stub.seen) == 2


def test_on_llm_end_fires_on_a_cache_hit_and_carries_the_marker(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub, spy = StubChatModel(), Spy()
    stub.invoke("same question", config={"callbacks": [spy]})
    stub.invoke("same question", config={"callbacks": [spy]})
    assert len(stub.seen) == 1
    assert spy.events == [
        ("start", None),
        ("end", None),  # a real call carries no marker
        ("start", None),  # a hit still starts ...
        ("end", {llm_cache.CACHE_HIT_KEY: True}),  # ... and ends, with the marker
    ]


def test_a_cache_hit_stays_a_hit_however_often_it_is_replayed(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub, spy = StubChatModel(), Spy()
    for _ in range(3):
        stub.invoke("same question", config={"callbacks": [spy]})
    ends = [info for kind, info in spy.events if kind == "end"]
    assert ends == [
        None,
        {llm_cache.CACHE_HIT_KEY: True},
        {llm_cache.CACHE_HIT_KEY: True},
    ]


# --- a tool loop replays every round ----------------------------------------


def _four_round_loop(model):
    """A tool loop of four model calls. Each round sends back the reply it got, with
    the answers to its tool calls, the way the graph agent does."""
    bound = model.bind_tools([])
    messages: list = [HumanMessage("which cause?")]
    for _ in range(4):
        reply = bound.invoke(messages, config=llm.call_config("tool loop"))
        messages.append(reply)
        messages += [
            ToolMessage(content="[]", tool_call_id=call["id"], name=call["name"])
            for call in reply.tool_calls
        ]
    return messages


def test_a_tool_loop_replays_every_round_on_the_second_run(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    script = [
        calls(("find_machine", {"text": "press"})),
        calls(("find_machine", {"text": "lathe"})),
        calls(("find_machine", {"text": "saw"})),
        say(),
    ]
    first, second = terra_model(script=script), terra_model(script=script)
    paid = _four_round_loop(first)
    replayed = _four_round_loop(second)
    assert (
        len(first.seen) == 4 and second.seen == []
    )  # the second run made no model call
    assert [row["cache_hit"] for row in rows()] == [0, 0, 0, 0, 1, 1, 1, 1]
    assert [m.content for m in replayed] == [m.content for m in paid]


def test_the_key_ignores_the_zero_cost_that_langchain_adds_to_a_replayed_message():
    def prompt(usage: dict) -> str:
        message = {
            "lc": 1,
            "type": "constructor",
            "id": ["langchain", "schema", "messages", "AIMessage"],
            "kwargs": {"content": "x", "usage_metadata": usage, "type": "ai"},
        }
        return json.dumps([message])

    paid = {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}
    replayed = {**paid, "total_cost": 0}
    key = llm_cache.SQLiteLLMCache._key
    assert key(prompt(replayed), "s") == key(prompt(paid), "s")
    assert key(prompt({**paid, "output_tokens": 2}), "s") != key(prompt(paid), "s")
