"""Shared stubs and helpers for the tests of the model client and its cache.

StubChatModel is a chat model that never calls out. It remembers what it was asked and
reports token usage like the real client, and StreamingStub also streams. gated puts the
budget check in front of a stub, run_config builds a call setup with a cost tracker, and
rows reads the ledger back. The isolated fixture keeps the developer's own ledger, cache
and keys out of every test.
"""

import sqlite3
from contextlib import closing
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import BaseModel, Field

from app import config, ledger, llm, llm_cache
from app.config import Settings

SOL, TERRA = config.SOL_MODEL, config.TERRA_MODEL
SETTINGS = Settings(openai_api_key="sk-test-key-not-real", budget_usd=40.0)
# A $1.00 budget, with and without a key.
TIGHT = Settings(openai_api_key="sk-test-key-not-real", budget_usd=1.0)
KEYLESS = Settings(budget_usd=1.0)


class Pair(BaseModel):
    alarm: str
    machine: str


REPLY = '{"alarm": "E-214", "machine": "CNC-01"}'


class StubChatModel(BaseChatModel):
    """A chat model that never calls out. It remembers what it was asked."""

    model: str = SOL
    reply: str = REPLY
    input_tokens: int = 1000
    cached_tokens: int = 200
    output_tokens: int = 300
    report_usage: bool = True
    fail: bool = False
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "stub"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model}

    def _usage(self) -> dict[str, Any] | None:
        if not self.report_usage:
            return None
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "input_token_details": {"cache_read": self.cached_tokens},
        }

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        if self.fail:
            raise RuntimeError("the provider is down")
        message = AIMessage(
            content=self.reply,
            usage_metadata=self._usage(),
            # The provider reports the exact model version it ran.
            response_metadata={"model_name": f"{self.model}-2026-10-01"},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def with_structured_output(self, schema, **kwargs):
        return self | PydanticOutputParser(pydantic_object=schema)


class StreamingStub(StubChatModel):
    """A stub that also streams its reply."""

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content="ok "))
        yield ChatGenerationChunk(
            message=AIMessageChunk(content="done", usage_metadata=self._usage())
        )


def gated(settings: Settings = SETTINGS, **fields) -> StubChatModel:
    """A stub behind the same budget check as the real client."""
    return StubChatModel(rate_limiter=llm.BudgetGate(settings), **fields)


def spend_the_budget() -> None:
    """Put $1.00 in the ledger, which uses up the whole test budget."""
    ledger.record(SOL, "earlier", 0, 0, 100_000, False)


class Spy(BaseCallbackHandler):
    """Records the start and end of each model call."""

    def __init__(self):
        self.events: list[tuple[str, Any]] = []

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.events.append(("start", None))

    def on_llm_end(self, response, *, run_id, **kwargs):
        self.events.append(("end", response.generations[0][0].generation_info))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Keep the developer's ledger, cache, keys and traces out of the test."""
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.sqlite")
    monkeypatch.setattr(llm_cache, "CACHE_PATH", tmp_path / "llm_cache.sqlite")
    monkeypatch.setattr(llm, "load_settings", lambda: SETTINGS)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE"):  # a missing value would fail fast
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "false")
    previous = get_llm_cache()
    set_llm_cache(None)
    yield
    set_llm_cache(previous)


def rows() -> list[dict[str, Any]]:
    if not ledger.LEDGER_PATH.exists():  # nothing has been recorded or checked yet
        return []
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM calls ORDER BY id")]


def run_config(purpose: str | None = "unit test", settings: Settings = SETTINGS):
    config_: dict[str, Any] = {"callbacks": [llm.CostTracker(settings)]}
    if purpose is not None:
        config_["metadata"] = {"purpose": purpose}
    return config_


def generation(text: str = "ok", **message_fields) -> ChatGeneration:
    return ChatGeneration(message=AIMessage(content=text, **message_fields))
