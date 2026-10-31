"""Tests for the model client: how it is built, the check that runs before each paid
call, cost tracking and tracing.

No test uses the network. Calls go to a stand-in chat model, and every ledger and
cache is a temporary file. The saved-answer cache has its own tests in
test_llm_cache.py. The shared stand-ins are in llm_stubs.py.
"""

import asyncio
import copy
import json
import logging
import uuid
from collections import Counter
from typing import Any

import pytest
from langchain_core.caches import BaseCache
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.rate_limiters import BaseRateLimiter
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from llm_stubs import (
    KEYLESS,
    REPLY,
    SETTINGS,
    SOL,
    TERRA,
    TIGHT,
    Pair,
    StreamingStub,
    StubChatModel,
    gated,
    generation,
    isolated,  # noqa: F401  (an autouse fixture)
    rows,
    run_config,
    spend_the_budget,
)
from pydantic import BaseModel, ValidationError, create_model

from app import ledger, llm, llm_cache
from app.config import Settings


def counting_gate(settings: Settings = SETTINGS) -> "llm.BudgetGate":
    """A gate that counts how often each entry point ran."""

    class CountingGate(llm.BudgetGate):
        def __init__(self, settings: Settings):
            super().__init__(settings)
            self.calls: Counter = Counter()

        def acquire(self, *, blocking: bool = True) -> bool:
            self.calls["acquire"] += 1
            return super().acquire(blocking=blocking)

        async def aacquire(self, *, blocking: bool = True) -> bool:
            self.calls["aacquire"] += 1
            return await super().aacquire(blocking=blocking)

    return CountingGate(settings)


# --- the model clients ------------------------------------------------------


@pytest.mark.parametrize(("role", "model_id"), [("sol", SOL), ("terra", TERRA)])
def test_chat_model_builds_the_openai_client_for_a_role(role, model_id):
    model = llm.chat_model(role)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == model_id
    assert model.use_responses_api is True
    assert model.reasoning_effort == "medium"
    assert model.request_timeout == 120
    assert model.max_retries == 2
    assert model.openai_api_key.get_secret_value() == SETTINGS.openai_api_key


def test_chat_model_passes_the_reasoning_effort_through():
    assert llm.chat_model("sol", "low").reasoning_effort == "low"
    assert llm.chat_model("terra", reasoning_effort="high").reasoning_effort == "high"


def test_chat_model_rejects_an_unknown_role():
    with pytest.raises(ValueError, match="sol.*terra"):
        llm.chat_model("luna")


# --- the cost tracker -------------------------------------------------------


def test_the_cost_tracker_makes_callback_errors_propagate():
    assert llm.CostTracker.raise_error is True


def test_a_real_call_is_recorded_with_its_tokens_and_dollars():
    StubChatModel().invoke("hello", config=run_config("extract alarms"))
    (row,) = rows()
    assert (row["model"], row["purpose"], row["cache_hit"]) == (
        SOL,
        "extract alarms",
        0,
    )
    assert (row["input_tokens"], row["cached_input_tokens"]) == (1000, 200)
    assert row["output_tokens"] == 300
    assert row["usd"] == pytest.approx((800 * 2.0 + 200 * 0.1 + 300 * 10.0) / 1e6)


def test_the_model_asked_for_is_billed_not_the_snapshot_the_provider_reports():
    StubChatModel().invoke("hello", config=run_config())
    (row,) = rows()
    assert row["model"] == SOL  # the stub's reply says gpt-6.1-sol-2026-10-01


def test_a_terra_call_is_billed_at_the_terra_price():
    StubChatModel(model=TERRA).invoke("hello", config=run_config())
    (row,) = rows()
    assert row["model"] == TERRA
    assert row["usd"] == pytest.approx((800 * 1.0 + 200 * 0.05 + 300 * 5.0) / 1e6)


def test_a_cache_hit_is_recorded_at_zero_dollars_with_the_replayed_tokens(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = StubChatModel()
    stub.invoke("hello", config=run_config())
    stub.invoke("hello", config=run_config())
    first, second = rows()
    assert (first["cache_hit"], second["cache_hit"]) == (0, 1)
    assert first["usd"] > 0
    assert second["usd"] == 0
    assert (second["input_tokens"], second["cached_input_tokens"]) == (1000, 200)
    assert second["output_tokens"] == 300
    assert ledger.spent_usd() == pytest.approx(first["usd"])  # the hit adds nothing


def test_the_purpose_comes_from_the_run_metadata_and_has_a_default():
    stub = StubChatModel()
    stub.invoke("one", config=run_config("rank causes"))
    stub.invoke("two", config=run_config(None))
    assert [row["purpose"] for row in rows()] == ["rank causes", "unspecified"]


def test_calls_with_no_cached_input_details_count_no_cached_tokens():
    StubChatModel(cached_tokens=0).invoke("hello", config=run_config())
    (row,) = rows()
    assert row["cached_input_tokens"] == 0
    assert row["usd"] == pytest.approx((1000 * 2.0 + 300 * 10.0) / 1e6)


def test_every_call_of_a_batch_is_recorded():
    StubChatModel().batch(["a", "b", "c"], config=run_config("batch"))
    assert [(row["purpose"], row["cache_hit"]) for row in rows()] == [("batch", 0)] * 3


@pytest.mark.usefixtures("cache_installed")
def test_ainvoke_on_a_cache_miss_books_the_stub_calls_cost():
    gate = counting_gate(SETTINGS)
    stub = StubChatModel(rate_limiter=gate)

    asyncio.run(stub.ainvoke("hello", config=run_config("async miss")))

    (row,) = rows()
    assert (row["model"], row["purpose"], row["cache_hit"]) == (SOL, "async miss", 0)
    assert (row["input_tokens"], row["cached_input_tokens"]) == (1000, 200)
    assert row["output_tokens"] == 300
    assert row["usd"] == pytest.approx((800 * 2.0 + 200 * 0.1 + 300 * 10.0) / 1e6)
    assert gate.calls == Counter({"aacquire": 1})  # the real call passed the check


def test_ainvoke_on_a_cache_hit_books_zero_dollars_with_cache_hit_set(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    gate = counting_gate(SETTINGS)
    stub = StubChatModel(rate_limiter=gate)

    async def twice():
        await stub.ainvoke("hello", config=run_config("async"))
        return await stub.ainvoke("hello", config=run_config("async"))

    replayed = asyncio.run(twice())

    assert replayed.content == REPLY
    assert len(stub.seen) == 1
    miss, hit = rows()
    assert (miss["cache_hit"], hit["cache_hit"]) == (0, 1)
    assert miss["usd"] > 0
    assert hit["usd"] == 0
    assert (hit["input_tokens"], hit["cached_input_tokens"]) == (1000, 200)
    assert hit["output_tokens"] == 300
    assert gate.calls == Counter({"aacquire": 1})  # only a miss is checked; a hit skips the check


def test_a_call_that_ends_without_starting_is_an_error_not_a_free_call():
    tracker = llm.CostTracker(SETTINGS)
    ended = LLMResult(generations=[[generation("x")]])
    with pytest.raises(RuntimeError, match="chat models only"):
        tracker.on_llm_end(ended, run_id=uuid.uuid4())
    assert rows() == []


# --- a real call that reports no usage --------------------------------------


def llm_warnings(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "app.llm" and record.levelno >= logging.WARNING
    ]


def test_a_real_call_with_no_usage_is_booked_as_an_estimate_not_as_free(caplog):
    stub = StubChatModel(report_usage=False, reply="abcde")
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        stub.invoke("x" * 100, config=run_config("extract alarms"))
    (row,) = rows()
    assert row["purpose"] == "extract alarms [usage estimated]"
    assert (row["model"], row["cache_hit"], row["cached_input_tokens"]) == (SOL, 0, 0)
    assert row["input_tokens"] == 34  # 100 characters / 3, rounded up
    assert row["output_tokens"] == 2002  # 5 characters / 3 rounded up, plus 2000
    assert row["usd"] > 0
    assert row["usd"] == pytest.approx((34 * 2.0 + 2002 * 10.0) / 1e6)
    assert ledger.spent_usd() == pytest.approx(row["usd"])


def test_ainvoke_with_no_usage_is_booked_as_an_estimate_not_as_free(caplog):
    stub = StreamingStub(report_usage=False, reply="abcde")
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        asyncio.run(stub.ainvoke("x" * 100, config=run_config("extract alarms")))
    (row,) = rows()
    assert row["purpose"] == "extract alarms [usage estimated]"
    assert (row["model"], row["cache_hit"], row["cached_input_tokens"]) == (SOL, 0, 0)
    assert row["input_tokens"] == 34  # 100 characters / 3, rounded up
    assert row["output_tokens"] == 2002  # 5 characters / 3 rounded up, plus 2000
    assert row["usd"] == pytest.approx((34 * 2.0 + 2002 * 10.0) / 1e6)
    (warning,) = llm_warnings(caplog)
    assert SOL in warning.getMessage()
    assert "extract alarms" in warning.getMessage()


def test_stream_with_no_usage_is_booked_as_an_estimate_not_as_free(caplog):
    stub = StreamingStub(report_usage=False)
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        chunks = list(stub.stream("x" * 100, config=run_config("extract alarms")))
    assert "".join(chunk.content for chunk in chunks) == "ok done"
    (row,) = rows()
    assert row["purpose"] == "extract alarms [usage estimated]"
    assert (row["model"], row["cache_hit"], row["cached_input_tokens"]) == (SOL, 0, 0)
    assert row["input_tokens"] == 34  # 100 characters / 3, rounded up
    assert row["output_tokens"] == 2003  # 7 characters / 3 rounded up, plus 2000
    assert row["usd"] == pytest.approx((34 * 2.0 + 2003 * 10.0) / 1e6)
    (warning,) = llm_warnings(caplog)
    assert SOL in warning.getMessage()
    assert "extract alarms" in warning.getMessage()


def test_the_estimate_warns_once_naming_the_model_and_purpose_but_not_the_content(
    caplog,
):
    stub = StubChatModel(report_usage=False, reply="secret reply text")
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        stub.invoke("secret prompt text", config=run_config("extract alarms"))
    (warning,) = llm_warnings(caplog)
    message = warning.getMessage()
    assert SOL in message
    assert "extract alarms" in message
    assert "secret" not in message


def test_the_prompt_estimate_counts_every_message_of_the_call():
    messages = [("system", "s" * 30), ("user", "u" * 31)]
    StubChatModel(report_usage=False).invoke(messages, config=run_config())
    (row,) = rows()
    assert row["input_tokens"] == 21  # 61 characters / 3 is 20.3, rounded up


def test_the_prompt_estimate_counts_the_text_of_content_blocks():
    blocks = [{"type": "text", "text": "y" * 9}, "zz"]
    prompt = [("user", blocks)]
    StubChatModel(report_usage=False).invoke(prompt, config=run_config())
    (row,) = rows()
    assert row["input_tokens"] == 4  # 11 characters / 3, rounded up


def test_usage_with_zero_input_tokens_counts_as_no_usage():
    stub = StubChatModel(
        input_tokens=0, cached_tokens=0, output_tokens=0, reply="abcde"
    )
    stub.invoke("hello", config=run_config("zero"))
    (row,) = rows()
    assert row["purpose"] == "zero [usage estimated]"
    assert (row["input_tokens"], row["output_tokens"]) == (2, 2002)


def test_a_call_that_reports_usage_is_booked_as_reported_without_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        StubChatModel().invoke("hello", config=run_config("exact"))
    (row,) = rows()
    assert row["purpose"] == "exact"
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 300)
    assert llm_warnings(caplog) == []


def test_a_cache_hit_with_no_usage_is_not_estimated(tmp_path, caplog):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = StubChatModel(report_usage=False)
    stub.invoke("hello", config=run_config("q"))  # the real call warns once
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        stub.invoke("hello", config=run_config("q"))
    first, second = rows()
    assert first["purpose"] == "q [usage estimated]"
    assert (second["purpose"], second["cache_hit"], second["usd"]) == ("q", 1, 0)
    assert (second["input_tokens"], second["output_tokens"]) == (0, 0)
    assert llm_warnings(caplog) == []  # the replay is free and honest, so it is quiet


def test_an_estimated_call_counts_toward_the_budget():
    StubChatModel(report_usage=False).invoke("hello", config=run_config())
    assert ledger.spent_usd() > 0.02  # the 2000 hidden tokens alone cost $0.02
    with pytest.raises(ledger.BudgetExceeded):
        ledger.check_budget(Settings(budget_usd=0.02))


# --- usage that contradicts itself ------------------------------------------


def test_more_cached_tokens_than_input_tokens_are_booked_as_all_input_cached(caplog):
    stub = StubChatModel(input_tokens=100, cached_tokens=150, output_tokens=10)
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        stub.invoke("hello", config=run_config("clamp"))
    (row,) = rows()
    assert row["purpose"] == "clamp"  # the call is real and reported, so no suffix
    assert (row["input_tokens"], row["cached_input_tokens"]) == (100, 100)
    assert row["output_tokens"] == 10
    assert row["usd"] == pytest.approx((100 * 0.1 + 10 * 10.0) / 1e6)
    (warning,) = llm_warnings(caplog)
    assert SOL in warning.getMessage()
    assert "clamp" in warning.getMessage()


def test_a_replay_of_an_entry_with_that_usage_books_zero_dollars_and_does_not_raise(
    tmp_path, caplog
):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = StubChatModel(input_tokens=100, cached_tokens=150)
    stub.invoke("hello", config=run_config("clamp"))  # stores the entry as reported
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        replayed = stub.invoke("hello", config=run_config("clamp"))
    assert replayed.content == REPLY
    assert len(stub.seen) == 1  # the second call never reached the model
    first, second = rows()
    assert (first["cache_hit"], first["cached_input_tokens"]) == (0, 100)
    assert (second["cache_hit"], second["usd"]) == (1, 0)
    assert (second["input_tokens"], second["cached_input_tokens"]) == (100, 100)
    assert len(llm_warnings(caplog)) == 1  # the replay is clamped and warned too


def test_cached_tokens_equal_to_the_input_tokens_are_consistent_and_quiet(caplog):
    stub = StubChatModel(input_tokens=100, cached_tokens=100, output_tokens=10)
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        stub.invoke("hello", config=run_config())
    (row,) = rows()
    assert (row["input_tokens"], row["cached_input_tokens"]) == (100, 100)
    assert llm_warnings(caplog) == []


# --- a paid call that fails -------------------------------------------------


class TruncatedReply(StubChatModel):
    """The provider answers, and then the client fails on the reply: a structured
    reply cut off by the token limit fails validation after the call was paid for."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        Pair.model_validate_json('{"alarm": "E-214", "machine": "CN')  # raises


# Each runs one call of a 100-character prompt: (model, config) -> result.
FAILING_ENTRY_POINTS = {
    "invoke": lambda model, config: model.invoke("x" * 100, config=config),
    "ainvoke": lambda model, config: asyncio.run(
        model.ainvoke("x" * 100, config=config)
    ),
}


@pytest.mark.parametrize("entry", FAILING_ENTRY_POINTS)
def test_a_paid_call_that_fails_is_booked_as_an_estimate_not_as_free(entry):
    config = run_config("extract alarms")
    (tracker,) = config["callbacks"]
    with pytest.raises(ValidationError):
        FAILING_ENTRY_POINTS[entry](TruncatedReply(), config)
    (row,) = rows()
    assert row["purpose"] == "extract alarms [failed call estimated]"
    assert (row["model"], row["cache_hit"], row["cached_input_tokens"]) == (SOL, 0, 0)
    assert row["input_tokens"] == 34  # 100 characters / 3, rounded up
    assert row["output_tokens"] == 2000
    assert row["usd"] == pytest.approx((34 * 2.0 + 2000 * 10.0) / 1e6)
    assert ledger.spent_usd() == pytest.approx(row["usd"])
    assert tracker._started == {}


def test_the_failed_call_estimate_warns_once_naming_the_model_and_purpose(caplog):
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        with pytest.raises(ValidationError):
            TruncatedReply().invoke("secret prompt", config=run_config("extract"))
    (warning,) = llm_warnings(caplog)
    message = warning.getMessage()
    assert SOL in message
    assert "extract" in message
    assert "secret" not in message  # not the prompt
    assert "E-214" not in message  # and not the reply the error quotes


def test_any_error_after_the_call_started_is_booked_and_leaves_no_state_behind():
    # A refused connection is booked too: the request may have gone out first.
    tracker = llm.CostTracker(SETTINGS)
    with pytest.raises(RuntimeError, match="provider is down"):
        StubChatModel(fail=True).invoke("hello", config={"callbacks": [tracker]})
    (row,) = rows()
    assert row["purpose"] == "unspecified [failed call estimated]"
    assert (row["input_tokens"], row["output_tokens"]) == (2, 2000)
    assert tracker._started == {}


@pytest.mark.parametrize("entry", FAILING_ENTRY_POINTS)
@pytest.mark.parametrize("refusal", ["no key", "spent budget"])
@pytest.mark.usefixtures("cache_installed")
def test_a_refusal_by_the_gate_books_nothing_because_no_request_went_out(
    refusal, entry, caplog
):
    if refusal == "no key":
        model, error = gated(KEYLESS), llm.MissingApiKey
    else:
        spend_the_budget()
        model, error = gated(TIGHT), ledger.BudgetExceeded
    earlier = rows()  # nothing, or only the spend that used the budget up
    config = run_config()
    (tracker,) = config["callbacks"]
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        with pytest.raises(error):
            FAILING_ENTRY_POINTS[entry](model, config)
    assert rows() == earlier
    assert model.seen == []
    assert tracker._started == {}
    assert llm_warnings(caplog) == []


# --- the gate in front of every real call -----------------------------------


def test_the_gate_is_a_langchain_rate_limiter_built_from_the_settings():
    gate = llm.BudgetGate(SETTINGS)
    assert isinstance(gate, BaseRateLimiter)
    assert gate.settings is SETTINGS


def test_missing_api_key_is_a_runtime_error_with_a_plain_message():
    assert issubclass(llm.MissingApiKey, RuntimeError)
    assert str(llm.MissingApiKey()) == (
        "No cached answer for this request and OPENAI_API_KEY is not set."
    )


def test_the_placeholder_key_is_not_shaped_like_a_real_key():
    assert llm.NO_KEY_PLACEHOLDER == "placeholder-no-live-calls"
    assert not llm.NO_KEY_PLACEHOLDER.startswith("sk-")


@pytest.fixture
def cache_installed(tmp_path):
    """A temporary model cache, for a test that sends a real request through the gate:
    the gate refuses a paid request that has nowhere to be kept."""
    return llm_cache.enable_cache(tmp_path / "gate-cache.sqlite")


def test_no_cache_installed_is_a_runtime_error_with_a_plain_message():
    assert issubclass(llm.NoCacheInstalled, RuntimeError)
    assert str(llm.NoCacheInstalled()) == (
        "No LLM cache is installed, so this paid answer would not be kept; "
        "call app.llm_cache.enable_cache() first"
    )


@pytest.mark.parametrize("entry", ["invoke", "ainvoke"])
def test_a_real_request_with_no_cache_installed_raises_and_makes_no_network_call(entry):
    stub = gated(SETTINGS)  # a key and budget, but no cache: the fixture clears it
    assert get_llm_cache() is None
    config_ = run_config()
    with pytest.raises(llm.NoCacheInstalled):
        if entry == "invoke":
            stub.invoke("hello", config=config_)
        else:
            asyncio.run(stub.ainvoke("hello", config=config_))
    assert stub.seen == []  # the model never ran
    assert rows() == []  # and the refused call booked nothing


def test_the_gate_names_the_missing_key_before_the_missing_cache():
    gate = llm.BudgetGate(KEYLESS)
    with pytest.raises(llm.MissingApiKey):
        gate.acquire()
    with pytest.raises(llm.MissingApiKey):
        asyncio.run(gate.aacquire())


def test_the_gate_names_the_missing_cache_before_the_spent_budget():
    spend_the_budget()
    gate = llm.BudgetGate(TIGHT)
    with pytest.raises(llm.NoCacheInstalled):
        gate.acquire()
    with pytest.raises(llm.NoCacheInstalled):
        asyncio.run(gate.aacquire())


def test_a_cached_prompt_still_replays_with_no_key_and_the_gate_never_asks_for_a_cache(
    tmp_path,
):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    gated(SETTINGS).invoke("hello", config=run_config())
    assert gated(KEYLESS).invoke("hello", config=run_config()).content == REPLY


@pytest.mark.usefixtures("cache_installed")
def test_the_gate_lets_a_call_through_when_there_is_a_key_and_budget_left():
    gate = llm.BudgetGate(TIGHT)
    assert gate.acquire() is True
    assert gate.acquire(blocking=False) is True
    assert asyncio.run(gate.aacquire()) is True


@pytest.mark.parametrize("key", [None, ""])
def test_the_gate_checks_the_key_before_it_looks_at_the_budget(key):
    spend_the_budget()  # no budget left either, but the missing key is named first
    gate = llm.BudgetGate(Settings(openai_api_key=key, budget_usd=1.0))
    with pytest.raises(llm.MissingApiKey, match="OPENAI_API_KEY is not set"):
        gate.acquire()
    with pytest.raises(llm.MissingApiKey, match="OPENAI_API_KEY is not set"):
        asyncio.run(gate.aacquire())


@pytest.mark.usefixtures("cache_installed")
def test_the_gate_raises_budget_exceeded_once_the_spend_reaches_the_budget():
    gate = llm.BudgetGate(TIGHT)
    gate.acquire()
    spend_the_budget()  # exactly the $1.00 budget
    with pytest.raises(ledger.BudgetExceeded):
        gate.acquire()
    with pytest.raises(ledger.BudgetExceeded):
        asyncio.run(gate.aacquire())


def test_a_spent_budget_still_replays_a_cached_prompt(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    stub = gated(TIGHT)
    stub.invoke("hello", config=run_config())  # a real call, while budget remained
    spend_the_budget()

    replayed = stub.invoke("hello", config=run_config())

    assert replayed.content == REPLY
    assert len(stub.seen) == 1  # the model ran only for the first call
    hit = rows()[-1]
    assert (hit["cache_hit"], hit["usd"]) == (1, 0)


def test_a_spent_budget_stops_an_uncached_prompt_before_the_model_runs(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    spend_the_budget()
    stub = gated(TIGHT)
    with pytest.raises(ledger.BudgetExceeded):
        stub.invoke("hello", config=run_config())
    assert stub.seen == []  # the model was never called
    assert len(rows()) == 1  # and the refused call booked nothing


def test_with_no_key_a_cached_prompt_replays(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    gated(SETTINGS).invoke("hello", config=run_config())  # written by a keyed run
    keyless = gated(KEYLESS)

    replayed = keyless.invoke("hello", config=run_config())

    assert replayed.content == REPLY
    assert keyless.seen == []
    hit = rows()[-1]
    assert (hit["cache_hit"], hit["usd"]) == (1, 0)


def test_with_no_key_an_uncached_prompt_raises_before_any_request(tmp_path):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    keyless = gated(KEYLESS)
    with pytest.raises(llm.MissingApiKey):
        keyless.invoke("hello", config=run_config())
    assert keyless.seen == []
    assert rows() == []


def test_the_cost_tracker_alone_no_longer_checks_the_budget():
    spend_the_budget()
    stub = StubChatModel()  # no gate, so nothing stops it: the gate owns the budget
    stub.invoke("hello", config=run_config(settings=KEYLESS))
    assert len(stub.seen) == 1


async def collect(stream) -> list:
    return [chunk async for chunk in stream]


ENTRY_POINTS = {
    "invoke": lambda model: model.invoke("hello", config=run_config()),
    "ainvoke": lambda model: asyncio.run(model.ainvoke("hello", config=run_config())),
    "stream": lambda model: list(model.stream("hello", config=run_config())),
    "astream": lambda model: asyncio.run(collect(model.astream("hello"))),
}


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.usefixtures("cache_installed")
def test_invoke_ainvoke_and_the_streams_all_pass_through_the_gate(entry):
    gate = counting_gate(SETTINGS)
    ENTRY_POINTS[entry](StreamingStub(rate_limiter=gate))
    sync_entry = entry in ("invoke", "stream")
    assert gate.calls == Counter({"acquire" if sync_entry else "aacquire": 1})


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.usefixtures("cache_installed")
def test_every_entry_point_stops_at_a_spent_budget_before_the_model_runs(entry):
    spend_the_budget()
    model = StreamingStub(rate_limiter=llm.BudgetGate(TIGHT))
    with pytest.raises(ledger.BudgetExceeded):
        ENTRY_POINTS[entry](model)
    assert model.seen == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_every_entry_point_stops_without_a_key_before_the_model_runs(entry):
    model = StreamingStub(rate_limiter=llm.BudgetGate(KEYLESS))
    with pytest.raises(llm.MissingApiKey):
        ENTRY_POINTS[entry](model)
    assert model.seen == []


def test_a_stream_never_reads_the_cache_so_the_gate_decides_every_streamed_call(
    tmp_path,
):
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    model = StreamingStub(rate_limiter=llm.BudgetGate(TIGHT))
    model.invoke("hello", config=run_config())  # cached now
    spend_the_budget()
    assert model.invoke("hello", config=run_config()).content == REPLY  # replays
    with pytest.raises(ledger.BudgetExceeded):
        list(model.stream("hello", config=run_config()))  # a stream always goes out
    assert len(model.seen) == 1


# --- the budget stop reaches the caller -------------------------------------


@pytest.mark.usefixtures("cache_installed")
def test_the_budget_stop_reaches_the_caller_through_a_chain():
    spend_the_budget()
    chain = gated(TIGHT) | PydanticOutputParser(pydantic_object=Pair)
    with pytest.raises(ledger.BudgetExceeded):
        chain.invoke("hello", config=run_config())


@pytest.mark.usefixtures("cache_installed")
def test_a_call_under_the_budget_goes_ahead():
    ledger.record(SOL, "earlier", 0, 0, 100_000, False)  # $1.00
    stub = gated(Settings(openai_api_key="sk-test-key-not-real", budget_usd=1.01))
    stub.invoke("hello", config=run_config())
    assert len(stub.seen) == 1


def test_a_model_with_no_price_stops_before_the_call():
    stub = StubChatModel(model="gpt-0-unknown")
    with pytest.raises(KeyError, match="gpt-0-unknown"):
        stub.invoke("hello", config=run_config())
    assert stub.seen == []
    assert rows() == []


def test_without_raise_error_langchain_would_swallow_the_unpriced_model_stop():
    class Lenient(llm.CostTracker):
        raise_error = False

    stub = StubChatModel(model="gpt-0-unknown")
    stub.invoke("hello", config={"callbacks": [Lenient(SETTINGS)]})  # no exception
    assert len(stub.seen) == 1  # the model ran anyway
    assert rows() == []  # and the call never reached the ledger


# --- tracing ----------------------------------------------------------------


class FakeLangfuse:
    clients: list[dict[str, Any]] = []

    def __init__(self, **kwargs):
        FakeLangfuse.clients.append(kwargs)


class FakeHandler(BaseCallbackHandler):
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def fake_langfuse(monkeypatch):
    FakeLangfuse.clients = []
    monkeypatch.setattr("langfuse.Langfuse", FakeLangfuse)
    monkeypatch.setattr("langfuse.langchain.CallbackHandler", FakeHandler)


LANGFUSE = Settings(
    langfuse_public_key="pk-lf-test",
    langfuse_secret_key="sk-lf-test",
    langfuse_host="https://jp.cloud.langfuse.com",
)


def test_tracing_callbacks_is_empty_without_keys(fake_langfuse):
    assert llm.tracing_callbacks(Settings()) == []
    assert llm.tracing_callbacks(Settings(langfuse_public_key="pk-lf-test")) == []
    assert llm.tracing_callbacks(Settings(langfuse_secret_key="sk-lf-test")) == []
    assert FakeLangfuse.clients == []  # no client was started either


def test_tracing_callbacks_starts_the_client_from_the_settings(fake_langfuse):
    (handler,) = llm.tracing_callbacks(LANGFUSE)
    assert isinstance(handler, FakeHandler)
    # The .env file is not exported, so the client is given everything explicitly.
    assert FakeLangfuse.clients == [
        {
            "public_key": "pk-lf-test",
            "secret_key": "sk-lf-test",
            "base_url": "https://jp.cloud.langfuse.com",
        }
    ]
    assert handler.kwargs == {"public_key": "pk-lf-test"}  # use that client


def test_callbacks_is_the_cost_tracker_then_the_tracing_handlers(fake_langfuse):
    plain = Settings()
    (tracker,) = llm.callbacks(plain)
    assert isinstance(tracker, llm.CostTracker)
    assert tracker.settings is plain

    tracker, handler = llm.callbacks(LANGFUSE)
    assert isinstance(tracker, llm.CostTracker)
    assert isinstance(handler, FakeHandler)
    assert tracker.settings is LANGFUSE


# --- structured -------------------------------------------------------------


@pytest.fixture
def stub_client(monkeypatch):
    """Make chat_model return a stub, and note what it was asked for. The stub has
    the cost tracker as a constructor callback, as the real client has."""
    stub, asked = StubChatModel(callbacks=[llm.CostTracker(SETTINGS)]), []

    def fake_chat_model(role, reasoning_effort="medium", *, settings=None):
        asked.append((role, reasoning_effort))
        return stub

    monkeypatch.setattr(llm, "chat_model", fake_chat_model)
    return stub, asked


def test_structured_returns_the_parsed_object_and_records_the_call(stub_client):
    stub, asked = stub_client
    result = llm.structured(
        "terra", Pair, "Extract the alarm.", "E-214 on CNC-01", "extract", "low"
    )
    assert result == Pair(alarm="E-214", machine="CNC-01")
    assert asked == [("terra", "low")]
    (messages,) = stub.seen
    assert [(m.type, m.content) for m in messages] == [
        ("system", "Extract the alarm."),
        ("human", "E-214 on CNC-01"),
    ]
    (row,) = rows()
    assert (row["purpose"], row["cache_hit"]) == ("extract", 0)


def test_structured_defaults_to_medium_reasoning(stub_client):
    _, asked = stub_client
    llm.structured("sol", Pair, "system", "user", "p")
    assert asked == [("sol", "medium")]


def test_structured_replays_from_the_cache(stub_client, tmp_path):
    stub, _ = stub_client
    llm_cache.enable_cache(tmp_path / "c.sqlite")
    first = llm.structured("sol", Pair, "system", "user", "extract")
    second = llm.structured("sol", Pair, "system", "user", "extract")
    assert first == second == Pair(alarm="E-214", machine="CNC-01")
    assert len(stub.seen) == 1
    assert [(row["cache_hit"], row["usd"] > 0) for row in rows()] == [
        (0, True),
        (1, False),
    ]


def test_structured_starts_tracing_when_there_are_keys(
    stub_client, fake_langfuse, monkeypatch
):
    monkeypatch.setattr(llm, "load_settings", lambda: LANGFUSE)
    llm.structured("sol", Pair, "system", "user", "p")
    assert len(FakeLangfuse.clients) == 1


# --- settings passed in -------------------------------------------------------


OTHER = Settings(openai_api_key="sk-other-key-not-real", budget_usd=2.0)


def test_chat_model_uses_the_settings_it_is_given_and_loads_none(monkeypatch):
    monkeypatch.setattr(llm, "load_settings", lambda: pytest.fail("loaded settings"))
    model = llm.chat_model("sol", "low", settings=OTHER)
    assert model.openai_api_key.get_secret_value() == "sk-other-key-not-real"
    assert model.rate_limiter.settings is OTHER
    assert model.reasoning_effort == "low"


def test_chat_model_loads_the_settings_once_when_given_none(monkeypatch):
    loads = []

    def load():
        loads.append("loaded")
        return OTHER

    monkeypatch.setattr(llm, "load_settings", load)
    model = llm.chat_model("terra")
    assert len(loads) == 1
    assert model.rate_limiter.settings is OTHER


def test_the_settings_argument_of_chat_model_is_keyword_only():
    with pytest.raises(TypeError):
        llm.chat_model("sol", "medium", OTHER)


@pytest.fixture
def spies(monkeypatch):
    """Record how often settings are loaded and which settings chat_model and
    tracing_callbacks receive from structured."""
    seen = {"loads": 0, "chat_model": [], "tracing": []}
    stub = StubChatModel(callbacks=[llm.CostTracker(SETTINGS)])
    real_tracing = llm.tracing_callbacks

    def load():
        seen["loads"] += 1
        return SETTINGS

    def fake_chat_model(role, reasoning_effort="medium", *, settings=None):
        seen["chat_model"].append(settings)
        return stub

    def spy_on_tracing(settings):
        seen["tracing"].append(settings)
        return real_tracing(settings)

    monkeypatch.setattr(llm, "load_settings", load)
    monkeypatch.setattr(llm, "chat_model", fake_chat_model)
    monkeypatch.setattr(llm, "tracing_callbacks", spy_on_tracing)
    return seen


def test_structured_loads_the_settings_once_and_passes_them_on(spies):
    llm.structured("sol", Pair, "system", "user", "p")
    assert spies["loads"] == 1
    assert spies["chat_model"] == [SETTINGS]
    assert spies["tracing"] == [SETTINGS]
    assert spies["chat_model"][0] is spies["tracing"][0]


def test_structured_uses_the_settings_it_is_given_and_loads_none(spies):
    llm.structured("sol", Pair, "system", "user", "p", settings=OTHER)
    assert spies["loads"] == 0
    assert spies["chat_model"] == [OTHER]
    assert spies["tracing"] == [OTHER]


def test_the_settings_argument_of_structured_is_keyword_only(spies):
    with pytest.raises(TypeError):
        llm.structured("sol", Pair, "system", "user", "p", "medium", OTHER)


def test_structured_starts_tracing_from_the_settings_it_is_given(
    stub_client, fake_langfuse
):
    # The settings load_settings returns have no Langfuse keys, so only the ones
    # passed in can start a client.
    llm.structured("sol", Pair, "system", "user", "p", settings=LANGFUSE)
    assert len(FakeLangfuse.clients) == 1


# --- inside a graph ------------------------------------------------------------


def test_chat_model_attaches_the_cost_tracker_as_a_constructor_callback():
    model = llm.chat_model("sol", settings=OTHER)
    (tracker,) = model.callbacks
    assert isinstance(tracker, llm.CostTracker)
    assert tracker.settings is OTHER


class Nest(BaseCallbackHandler):
    """Records the model starts it sees, with their parent run."""

    def __init__(self):
        self.starts: list[Any] = []

    def on_chat_model_start(
        self, serialized, messages, *, run_id, parent_run_id=None, **kwargs
    ):
        self.starts.append(parent_run_id)


def test_structured_inside_a_run_nests_under_it_and_books_the_call_once(stub_client):
    nest = Nest()
    inner = RunnableLambda(
        lambda _: llm.structured("sol", Pair, "system", "user", "in graph")
    )
    result = inner.invoke(None, {"callbacks": [nest]})
    assert result == Pair(alarm="E-214", machine="CNC-01")
    assert (
        len(nest.starts) == 1 and nest.starts[0] is not None
    )  # the parent's handler saw it
    (row,) = rows()
    assert (row["purpose"], row["cache_hit"]) == ("in graph", 0)


def test_structured_inside_a_run_does_not_start_its_own_tracing(
    stub_client, fake_langfuse
):
    RunnableLambda(
        lambda _: llm.structured("sol", Pair, "system", "user", "p", settings=LANGFUSE)
    ).invoke(None)
    assert FakeLangfuse.clients == []


def test_structured_outside_a_run_books_the_call_once_and_traces(
    stub_client, fake_langfuse
):
    llm.structured("sol", Pair, "system", "user", "alone", settings=LANGFUSE)
    assert len(rows()) == 1
    assert len(FakeLangfuse.clients) == 1


def test_structured_inside_a_run_keeps_the_purpose_in_the_metadata(stub_client):
    RunnableLambda(
        lambda _: llm.structured("sol", Pair, "system", "user", "kept")
    ).invoke(None, {"metadata": {"purpose": "outer"}})
    assert [row["purpose"] for row in rows()] == ["kept"]


def test_the_cache_key_does_not_depend_on_the_constructor_callbacks():
    plain = llm._ChatOpenAI(model=SOL, use_responses_api=True, api_key="x")
    tracked = llm.chat_model("sol", settings=OTHER)
    assert plain._get_llm_string(stop=None, response_format=Pair.__name__) == (
        llm._ChatOpenAI(
            model=SOL,
            use_responses_api=True,
            api_key="x",
            callbacks=[llm.CostTracker(OTHER)],
        )._get_llm_string(stop=None, response_format=Pair.__name__)
    )
    assert "CostTracker" not in tracked._get_llm_string(stop=None)


# --- the real client, answered from a canned cache ---------------------------


class Recorder(BaseCache):
    """Answers every lookup with one canned generation and keeps the keys it saw."""

    def __init__(self, parsed: Any = None):
        self.keys: list[tuple[str, str]] = []
        self.parsed = (
            parsed
            if parsed is not None
            else {
                "alarm": "E-214",
                "machine": "CNC-01",
                "severity": "warning",
            }
        )

    def lookup(self, prompt, llm_string):
        self.keys.append((prompt, llm_string))
        return [canned(self.parsed)]

    def update(self, prompt, llm_string, return_val):
        raise AssertionError("a canned answer means no real call, so no update")

    def clear(self, **kwargs):
        pass


def canned(parsed: Any) -> ChatGeneration:
    """What the Responses API path hands back for a structured call."""
    return ChatGeneration(
        message=AIMessage(
            content=[{"type": "text", "text": REPLY, "annotations": []}],
            additional_kwargs={"parsed": parsed},
            response_metadata={"model_name": "gpt-6.1-sol-2026-10-01"},
            usage_metadata={
                "input_tokens": 1200,
                "output_tokens": 80,
                "total_tokens": 1280,
                "input_token_details": {"cache_read": 200},
                "output_token_details": {"reasoning": 50},
            },
            id="resp_canned",
        )
    )


def schema_with(*fields: str) -> type[BaseModel]:
    """Models with one name and module but different fields."""
    return create_model(
        "Result", __module__=__name__, **{name: (str, ...) for name in fields}
    )


def key_for(schema: type[BaseModel], system: str = "system") -> tuple[str, str]:
    recorder = Recorder()
    set_llm_cache(recorder)
    llm.structured("sol", schema, system, "user", "key test")
    return recorder.keys[0]


def test_the_api_key_never_reaches_the_cache_key():
    prompt, llm_string = key_for(Pair)
    assert SETTINGS.openai_api_key not in prompt + llm_string


def test_the_cache_key_is_the_same_for_the_same_call():
    assert key_for(Pair) == key_for(Pair)
    assert key_for(Pair) != key_for(Pair, system="a different system prompt")


def test_the_cache_key_carries_the_schema_not_just_its_class_name():
    narrow, wide = schema_with("alarm"), schema_with("alarm", "severity")
    assert repr(narrow) == repr(wide)  # langchain alone would give both one key
    assert key_for(narrow) != key_for(wide)
    assert json.dumps(wide.model_json_schema(), sort_keys=True) in key_for(wide)[1]


# What the environment can set without changing what a request asks for.
KEY_ENVIRONMENT = {
    "OPENAI_BASE_URL": "http://127.0.0.1:9",
    "OPENAI_API_BASE": "http://127.0.0.1:9",
    "OPENAI_ORG_ID": "org-dummy",
    "OPENAI_ORGANIZATION": "org-dummy",
    "OPENAI_PROXY": "http://127.0.0.1:9",
}


@pytest.fixture
def clean_environment(monkeypatch):
    """No OPENAI_* variable, as on a fresh machine. The autouse fixture sets two of
    them so that a stray request fails fast."""
    for name in KEY_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LANGSMITH_GATEWAY", raising=False)


def real_client(**fields) -> "llm._ChatOpenAI":
    """The client as chat_model builds it, with fields to change."""
    settings = {
        "model": SOL,
        "api_key": "sk-test-key-not-real",
        "use_responses_api": True,
        "reasoning_effort": "low",
        "timeout": 120,
        "max_retries": 2,
    }
    return llm._ChatOpenAI(**{**settings, **fields})


@pytest.mark.parametrize("name", [*KEY_ENVIRONMENT, "LANGSMITH_GATEWAY"])
def test_the_cache_key_ignores_each_environment_variable_that_only_routes_a_request(
    name, clean_environment, monkeypatch
):
    clean = key_for(Pair)
    monkeypatch.setenv(name, "http://127.0.0.1:9")
    assert key_for(Pair) == clean


def test_the_cache_key_is_the_same_with_or_without_an_api_key(monkeypatch):
    keyed = key_for(Pair)
    monkeypatch.setattr(llm, "load_settings", lambda: KEYLESS)
    assert key_for(Pair) == keyed


def test_the_cache_key_ignores_the_module_a_schema_is_defined_in():
    here = create_model("Result", __module__="app_one.schemas", alarm=(str, ...))
    there = create_model("Result", __module__="app_two.models", alarm=(str, ...))
    assert repr(here) != repr(there)
    assert key_for(here) == key_for(there)
    assert key_for(here) != key_for(schema_with("alarm", "severity"))


@pytest.mark.parametrize(
    "change",
    [
        {"timeout": 30},
        {"max_retries": 5},
        {"base_url": "http://127.0.0.1:10"},  # not the one in the environment
        {"organization": "org-dummy"},
        {"openai_proxy": "http://127.0.0.1:9"},
        {"stream_usage": False},
    ],
    ids=lambda change: next(iter(change)),
)
def test_the_cache_key_ignores_the_transport_only_fields(change):
    assert real_client(**change)._get_llm_string() == real_client()._get_llm_string()


@pytest.mark.parametrize(
    "change",
    [
        {"model": TERRA},
        {"reasoning_effort": "high"},
        {"reasoning": {"effort": "high"}},
        {"verbosity": "low"},
        {"temperature": 0.2},
        {"max_tokens": 100},
        {"top_p": 0.5},
        {"seed": 7},
        {"service_tier": "flex"},
        {"truncation": "auto"},
    ],
    ids=lambda change: next(iter(change)),
)
def test_the_cache_key_still_changes_with_every_field_that_shapes_the_request(change):
    assert real_client(**change)._get_llm_string() != real_client()._get_llm_string()


# The client fields that only say where or how a request is sent. This restates
# llm._TRANSPORT_FIELDS on purpose: reading the tuple instead would let a change that
# moves a request-shaping field into it also remove that field's case below.
TRANSPORT_ONLY = frozenset(
    {
        "openai_api_base",
        "openai_organization",
        "openai_proxy",
        "stream_usage",
        "request_timeout",
        "max_retries",
    }
)


def default_client() -> "llm._ChatOpenAI":
    """The client chat_model builds with no key, so it cannot reach the network."""
    return llm.chat_model("sol", settings=KEYLESS)


# Every serialized field of that client that is not transport. The key has to hold
# each of them, so a change to any one means a new cache entry.
DEFAULT_KEY_FIELDS = sorted(
    set(default_client()._serialized["kwargs"]) - TRANSPORT_ONLY
)


def test_the_default_client_serializes_the_fields_that_decide_what_a_request_asks():
    # Guards the parametrization below against going empty or missing these.
    assert {"model_name", "reasoning_effort", "use_responses_api"} <= set(
        DEFAULT_KEY_FIELDS
    )


@pytest.mark.parametrize("name", DEFAULT_KEY_FIELDS)
def test_the_cache_key_holds_every_field_of_a_default_client_that_is_not_transport(
    name,
):
    client = default_client()
    before = client._get_llm_string()
    client._serialized["kwargs"][name] = "changed"  # the dict the key is built from
    assert client._get_llm_string() != before


def test_schema_classes_nested_in_call_arguments_are_keyed_by_schema_not_by_repr():
    here = create_model("Result", __module__="app_one.schemas", alarm=(str, ...))
    there = create_model("Result", __module__="app_two.models", alarm=(str, ...))

    def string(schema):
        return real_client()._get_llm_string(
            stop=["END"],
            ls_structured_output_format={"kwargs": {"method": "x"}, "schema": schema},
            tools=[{"type": "function", "parameters": [schema]}],
        )

    assert string(here) == string(there)
    assert "<class" not in string(here)
    assert json.dumps(here.model_json_schema(), sort_keys=True) in string(here)
    assert string(here) != string(schema_with("alarm", "severity"))
    assert "END" in string(here)  # the stop words are still part of the key


def test_building_the_cache_key_leaves_the_serialized_client_untouched():
    # Callbacks receive this same dict, so dropping fields from it would hide them.
    model = real_client(base_url="http://127.0.0.1:9")
    before = copy.deepcopy(model._serialized)
    model._get_llm_string(response_format=Pair)
    assert model._serialized == before
    assert "openai_api_base" in before["kwargs"]


def test_an_entry_written_in_a_clean_environment_replays_under_another_with_no_request(
    tmp_path, clean_environment, no_requests, monkeypatch
):
    prompt, llm_string = key_for(Pair, "Extract the alarm.")  # a clean machine, a key
    cache = llm_cache.enable_cache(tmp_path / "c.sqlite")
    cache.update(prompt, llm_string, [canned(Pair(alarm="E-214", machine="CNC-01"))])

    # Replayed elsewhere: other endpoints, an organisation, a proxy and no key.
    for name, value in KEY_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(llm, "load_settings", lambda: KEYLESS)

    result = llm.structured("sol", Pair, "Extract the alarm.", "user", "replay")

    assert result == Pair(alarm="E-214", machine="CNC-01")
    assert no_requests == []
    last = rows()[-1]
    assert (last["purpose"], last["model"]) == ("replay", SOL)
    assert (last["cache_hit"], last["usd"]) == (1, 0)


def test_the_cache_key_names_the_model_and_its_reasoning_effort():
    recorder = Recorder()
    set_llm_cache(recorder)
    for role, effort in (("sol", "low"), ("sol", "high"), ("terra", "low")):
        llm.structured(role, Pair, "system", "user", "p", effort)
    strings = [llm_string for _, llm_string in recorder.keys]
    assert len(set(strings)) == 3
    assert SOL in strings[0] and '"reasoning_effort": "low"' in strings[0]
    assert '"reasoning_effort": "high"' in strings[1]
    assert TERRA in strings[2]


def test_a_structured_call_replays_through_the_real_client_with_no_network(tmp_path):
    # Learn the key the real client uses, then store a canned answer under it.
    prompt, llm_string = key_for(Pair, "Extract the alarm.")
    cache = llm_cache.enable_cache(tmp_path / "c.sqlite")
    cache.update(prompt, llm_string, [canned(Pair(alarm="E-214", machine="CNC-01"))])

    result = llm.structured("sol", Pair, "Extract the alarm.", "user", "replay")

    assert result == Pair(alarm="E-214", machine="CNC-01")
    last = rows()[-1]
    assert (last["purpose"], last["model"], last["cache_hit"]) == ("replay", SOL, 1)
    assert last["usd"] == 0
    assert (last["input_tokens"], last["cached_input_tokens"]) == (1200, 200)
    assert last["output_tokens"] == 80


# --- the real client behind its gate -----------------------------------------


@pytest.fixture
def no_requests(monkeypatch):
    """Make any request the real client tries to send fail the test. The returned
    list names the request methods that were reached, so it is empty when the gate
    or the cache answered first."""
    reached: list[str] = []

    def refuse(name):
        def method(*args, **kwargs):
            reached.append(name)
            raise AssertionError(f"{name} would have sent a request")

        return method

    async def arefuse(*args, **kwargs):
        reached.append("_agenerate")
        raise AssertionError("_agenerate would have sent a request")

    async def arefuse_stream(*args, **kwargs):
        reached.append("_astream")
        raise AssertionError("_astream would have sent a request")
        yield  # makes this an async generator, as _astream is

    monkeypatch.setattr(llm._ChatOpenAI, "_generate", refuse("_generate"))
    monkeypatch.setattr(llm._ChatOpenAI, "_stream", refuse("_stream"))
    monkeypatch.setattr(llm._ChatOpenAI, "_agenerate", arefuse)
    monkeypatch.setattr(llm._ChatOpenAI, "_astream", arefuse_stream)
    return reached


def test_chat_model_puts_the_budget_gate_in_front_of_the_client():
    model = llm.chat_model("sol")
    assert isinstance(model.rate_limiter, llm.BudgetGate)
    assert model.rate_limiter.settings is SETTINGS


def test_chat_model_with_no_key_is_built_with_the_placeholder(monkeypatch):
    monkeypatch.setattr(llm, "load_settings", lambda: KEYLESS)
    model = llm.chat_model("terra")
    assert model.openai_api_key.get_secret_value() == llm.NO_KEY_PLACEHOLDER
    assert isinstance(model.rate_limiter, llm.BudgetGate)
    assert model.rate_limiter.settings is KEYLESS


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_the_real_client_refuses_every_uncached_request_without_a_key(
    entry, no_requests, monkeypatch
):
    monkeypatch.setattr(llm, "load_settings", lambda: KEYLESS)
    with pytest.raises(llm.MissingApiKey):
        ENTRY_POINTS[entry](llm.chat_model("sol"))
    assert no_requests == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.usefixtures("cache_installed")
def test_the_real_client_refuses_every_uncached_request_at_a_spent_budget(
    entry, no_requests, monkeypatch
):
    monkeypatch.setattr(llm, "load_settings", lambda: TIGHT)
    spend_the_budget()
    with pytest.raises(ledger.BudgetExceeded):
        ENTRY_POINTS[entry](llm.chat_model("sol"))
    assert no_requests == []


@pytest.mark.usefixtures("cache_installed")
def test_structured_stops_at_the_budget_before_the_request_goes_out(
    no_requests, monkeypatch
):
    monkeypatch.setattr(llm, "load_settings", lambda: TIGHT)
    spend_the_budget()
    with pytest.raises(ledger.BudgetExceeded):
        llm.structured("sol", Pair, "system", "user", "p")
    assert no_requests == []
    assert len(rows()) == 1  # only the earlier spend; the refused call booked nothing


@pytest.mark.usefixtures("cache_installed")
def test_structured_hands_the_settings_it_was_given_to_the_gate(no_requests):
    # load_settings returns a keyed object here, so only the argument can refuse.
    with pytest.raises(llm.MissingApiKey):
        llm.structured("sol", Pair, "system", "user", "p", settings=KEYLESS)
    spend_the_budget()
    with pytest.raises(ledger.BudgetExceeded):
        llm.structured("sol", Pair, "system", "user", "p", settings=TIGHT)
    assert no_requests == []


def test_structured_with_no_key_and_no_cached_answer_raises_missing_api_key(
    no_requests, monkeypatch
):
    monkeypatch.setattr(llm, "load_settings", lambda: KEYLESS)
    with pytest.raises(llm.MissingApiKey, match="OPENAI_API_KEY is not set"):
        llm.structured("sol", Pair, "system", "user", "p")
    assert no_requests == []
    assert rows() == []


def test_a_cached_structured_call_replays_with_no_key_and_the_budget_spent(
    tmp_path, no_requests, monkeypatch
):
    prompt, llm_string = key_for(Pair, "Extract the alarm.")
    cache = llm_cache.enable_cache(tmp_path / "c.sqlite")
    cache.update(prompt, llm_string, [canned(Pair(alarm="E-214", machine="CNC-01"))])
    spend_the_budget()
    monkeypatch.setattr(llm, "load_settings", lambda: KEYLESS)

    result = llm.structured("sol", Pair, "Extract the alarm.", "user", "replay")

    assert result == Pair(alarm="E-214", machine="CNC-01")
    assert no_requests == []
    last = rows()[-1]
    assert (last["purpose"], last["cache_hit"], last["usd"]) == ("replay", 1, 0)


# --- call_config ---------------------------------------------------------------


def test_call_config_carries_the_purpose_and_loads_the_settings_once(monkeypatch):
    loaded = []
    monkeypatch.setattr(llm, "load_settings", lambda: loaded.append(1) or SETTINGS)
    assert llm.call_config("graph agent") == {
        "metadata": {"purpose": "graph agent"},
        "callbacks": [],
    }
    assert loaded == [1]


def test_call_config_uses_the_settings_it_is_given_and_loads_none(monkeypatch):
    monkeypatch.setattr(llm, "load_settings", lambda: pytest.fail("loaded settings"))
    assert llm.call_config("p", OTHER) == {
        "metadata": {"purpose": "p"},
        "callbacks": [],
    }


def test_call_config_traces_only_outside_a_run(fake_langfuse):
    outside = llm.call_config("p", LANGFUSE)
    assert len(outside["callbacks"]) == 1 and len(FakeLangfuse.clients) == 1
    inside = RunnableLambda(lambda _: llm.call_config("p", LANGFUSE)).invoke(None)
    assert inside == {"metadata": {"purpose": "p"}}
