"""The model clients, with a budget gate, cost tracking and tracing.

`chat_model` builds the two OpenAI clients. Each one reads saved answers first, so a
question asked before costs nothing. A real request goes out only if a key is set and
the budget is not used up. `CostTracker` writes every call to the spend ledger.
`structured` makes one call and returns the parsed answer. `tracing_callbacks` adds
Langfuse tracing when its keys are set.
"""

import asyncio
import copy
import json
import logging
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import get_llm_cache
from langchain_core.language_models.chat_models import _cleanup_llm_representation
from langchain_core.outputs import Generation, LLMResult
from langchain_core.rate_limiters import BaseRateLimiter
from langchain_core.runnables.config import var_child_runnable_config
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app import llm_cache
from app.config import SOL_MODEL, TERRA_MODEL, Settings, load_settings
from app.ledger import PRICES, BudgetExceeded, check_budget, record

logger = logging.getLogger(__name__)

MODELS = {"sol": SOL_MODEL, "terra": TERRA_MODEL}
DEFAULT_PURPOSE = "unspecified"

# The client needs some key to be built. With no key, this stand-in lets a saved
# answer still replay. The budget gate refuses every real request, so the stand-in
# never reaches the network.
NO_KEY_PLACEHOLDER = "placeholder-no-live-calls"
MISSING_KEY_MESSAGE = "No cached answer for this request and OPENAI_API_KEY is not set."
NO_CACHE_MESSAGE = (
    "No LLM cache is installed, so this paid answer would not be kept; "
    "call app.llm_cache.enable_cache() first"
)

# A real call that comes back with no usage is booked as an estimate. The numbers lean
# high on purpose, because the models also spend hidden tokens that never show in text.
CHARS_PER_TOKEN = 3
HIDDEN_REASONING_TOKENS = 2000
USAGE_ESTIMATED = " [usage estimated]"

# A paid call can fail after the provider has answered, so a failed call is booked as
# an estimate. The estimate leans high to keep the budget stop safe.
FAILED_CALL_OUTPUT_TOKENS = 2000
FAILED_CALL_ESTIMATED = " [failed call estimated]"


# --- the clients --------------------------------------------------------------


# Client fields that set where a request goes, not what it asks. They are left out of
# the cache key, so an answer saved on one machine can replay on another. Every other
# field stays in the key, so a change to the model or its settings gets a new key.
_TRANSPORT_FIELDS = (
    "openai_api_base",
    "openai_organization",
    "openai_proxy",
    "stream_usage",
    "request_timeout",
    "max_retries",
)


def _schema_text(value: Any) -> Any:
    """Replace every model class in the value with the text of its schema."""
    if isinstance(value, type) and issubclass(value, BaseModel):
        return json.dumps(value.model_json_schema(), sort_keys=True)
    if isinstance(value, dict):
        return {key: _schema_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_schema_text(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_schema_text(item) for item in value)
    return value


class _ChatOpenAI(ChatOpenAI):
    """An OpenAI client whose cache key depends only on what shapes the request.

    The default key changes with the environment and ignores edits to a schema, so
    old answers could replay for new fields. This version fixes both."""

    def _get_llm_string(self, stop: list[str] | None = None, **kwargs: Any) -> str:
        # Work on a copy, because other code reads the original.
        serialized = copy.deepcopy(self._serialized)
        _cleanup_llm_representation(serialized, 1)
        client_fields = serialized.get("kwargs")
        if isinstance(client_fields, dict):
            for name in _TRANSPORT_FIELDS:
                client_fields.pop(name, None)
        params = _schema_text({**kwargs, "stop": stop})
        param_string = str(sorted(params.items()))
        return json.dumps(serialized, sort_keys=True) + "---" + param_string


class MissingApiKey(RuntimeError):
    """Raised when a request must go out but there is no OpenAI key."""

    def __init__(self, message: str = MISSING_KEY_MESSAGE):
        super().__init__(message)


class NoCacheInstalled(RuntimeError):
    """Raised when a paid request has no cache to keep its answer, so it would be paid for again."""

    def __init__(self, message: str = NO_CACHE_MESSAGE):
        super().__init__(message)


class BudgetGate(BaseRateLimiter):
    """Stops a real request that has no key, no cache or no budget behind it.

    It runs only when the saved answers missed, so a saved answer replays even with no
    key and a spent budget."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _require_key(self) -> None:
        if not self.settings.has_openai:
            raise MissingApiKey()

    @staticmethod
    def _require_cache() -> None:
        if get_llm_cache() is None:
            raise NoCacheInstalled()

    def acquire(self, *, blocking: bool = True) -> bool:
        self._require_key()
        self._require_cache()
        check_budget(self.settings)
        return True

    async def aacquire(self, *, blocking: bool = True) -> bool:
        self._require_key()
        self._require_cache()
        # The budget check reads a database, so keep it off the main loop.
        await asyncio.to_thread(check_budget, self.settings)
        return True


def chat_model(
    role: str, reasoning_effort: str = "medium", *, settings: Settings | None = None
) -> ChatOpenAI:
    """Build the OpenAI client for a role, "sol" or "terra".

    Every request goes through the budget gate. With no key, saved answers still
    replay and everything else is refused. The client also books each call to the
    ledger, wherever it is called from."""
    try:
        model_id = MODELS[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; expected 'sol' or 'terra'") from None
    if settings is None:
        settings = load_settings()
    return _ChatOpenAI(
        model=model_id,
        use_responses_api=True,
        reasoning_effort=reasoning_effort,
        api_key=settings.openai_api_key or NO_KEY_PLACEHOLDER,
        timeout=120,
        max_retries=2,
        rate_limiter=BudgetGate(settings),
        callbacks=[CostTracker(settings)],
    )


# --- cost tracking ------------------------------------------------------------


def _requested_model(params: dict | None, metadata: dict | None) -> str | None:
    """Return the model a call asked for. The reply names a dated version with no price."""
    params = params or {}
    return (
        params.get("model")
        or params.get("model_name")
        or (metadata or {}).get("ls_model_name")
    )


def _tokens_for(chars: int) -> int:
    """Estimate the token count for this many characters."""
    return -(-chars // CHARS_PER_TOKEN)


def _content_chars(content: Any) -> int:
    """Count the characters in message content of any shape."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return len(text)
        return len(json.dumps(content, default=str))
    if isinstance(content, list):
        return sum(_content_chars(block) for block in content)
    return len(str(content))


def _prompt_chars(messages: list) -> int:
    """Count the characters in all the messages of a call."""
    return sum(_content_chars(message.content) for run in messages for message in run)


def _output_chars(generation: Generation) -> int:
    """Count the characters in an answer, including any tool calls."""
    message = getattr(generation, "message", None)
    if message is None:
        return len(generation.text)
    chars = _content_chars(message.content)
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        chars += len(json.dumps(tool_calls, default=str))
    return chars


class CostTracker(BaseCallbackHandler):
    """Writes each chat model call to the spend ledger.

    It checks that the model has a price before a call starts, and records the tokens
    when it ends. A replayed answer is booked as free. A real call that reports no
    usage is booked as an estimate, never as free, so spend stays visible. A refusal by
    the budget gate books nothing, because no request went out."""

    raise_error = True

    def __init__(self, settings: Settings):
        # The settings are kept so one object feeds every part.
        self.settings = settings
        # Maps each running call to its model, purpose and prompt length.
        self._started: dict[UUID, tuple[str, str, int]] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        model = _requested_model(kwargs.get("invocation_params"), metadata)
        if model not in PRICES:
            raise KeyError(f"no price for model {model!r}")
        purpose = (metadata or {}).get("purpose") or DEFAULT_PURPOSE
        self._started[run_id] = (model, purpose, _prompt_chars(messages))

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        started = self._started.pop(run_id, None)
        if started is None:
            raise RuntimeError(
                "a model call ended that CostTracker never saw start; "
                "it supports chat models only"
            )
        model, purpose, prompt_chars = started
        generation = response.generations[0][0]
        usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
        usage = usage or {}
        cache_hit = llm_cache.is_cache_hit(generation)
        if cache_hit or usage.get("input_tokens"):
            input_tokens = usage.get("input_tokens", 0)
            cached = (usage.get("input_token_details") or {}).get("cache_read", 0)
            if cached > input_tokens:
                # The numbers cannot be right, but the answer is already saved.
                # Raising would break every replay, so book it as all cached.
                logger.warning(
                    "a %s call (purpose: %s) reported %d cached input tokens but "
                    "%d input tokens; booking %d cached",
                    model,
                    purpose,
                    cached,
                    input_tokens,
                    input_tokens,
                )
                cached = input_tokens
            record(
                model,
                purpose,
                input_tokens,
                cached,
                usage.get("output_tokens", 0),
                cache_hit,
            )
            return
        logger.warning(
            "no token usage came back for a %s call (purpose: %s); booking an estimate",
            model,
            purpose,
        )
        record(
            model,
            purpose + USAGE_ESTIMATED,
            _tokens_for(prompt_chars),
            0,
            _tokens_for(_output_chars(generation)) + HIDDEN_REASONING_TOKENS,
            False,
        )

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        """Book a cautious estimate for a call that started and then failed.

        The provider may have billed the call before the failure, so it is not free.
        Refusals by the budget gate are skipped, since no request went out."""
        started = self._started.pop(run_id, None)
        if started is None or isinstance(
            error, (MissingApiKey, NoCacheInstalled, BudgetExceeded)
        ):
            return
        model, purpose, prompt_chars = started
        logger.warning(
            "a %s call (purpose: %s) failed with %s; booking an estimate",
            model,
            purpose,
            type(error).__name__,
        )
        record(
            model,
            purpose + FAILED_CALL_ESTIMATED,
            _tokens_for(prompt_chars),
            0,
            FAILED_CALL_OUTPUT_TOKENS,
            False,
        )


# --- tracing ------------------------------------------------------------------


def tracing_callbacks(settings: Settings) -> list[BaseCallbackHandler]:
    """Return the Langfuse tracing handler when both keys are set, otherwise nothing.

    Short scripts should flush the Langfuse client before they exit."""
    if not settings.has_langfuse:
        return []
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_host,
    )
    return [CallbackHandler(public_key=settings.langfuse_public_key)]


def callbacks(settings: Settings) -> list[BaseCallbackHandler]:
    """Return the cost tracker and the tracing handlers, for a model not built by chat_model."""
    return [CostTracker(settings), *tracing_callbacks(settings)]


# --- structured calls ---------------------------------------------------------


def _inside_a_run() -> bool:
    """Say whether the caller is already inside another run, such as a graph step."""
    config = var_child_runnable_config.get()
    manager = (config or {}).get("callbacks")
    return getattr(manager, "parent_run_id", None) is not None


def call_config(purpose: str, settings: Settings | None = None) -> dict[str, Any]:
    """Build the settings for one model call: its ledger label, and tracing if it applies.

    Inside another run, tracing is left out so the call stays part of the parent's trace."""
    if settings is None:
        settings = load_settings()
    config: dict[str, Any] = {"metadata": {"purpose": purpose}}
    if not _inside_a_run():
        config["callbacks"] = tracing_callbacks(settings)
    return config


def structured[T: BaseModel](
    role: str,
    schema: type[T],
    system: str,
    user: str,
    purpose: str,
    reasoning_effort: str = "medium",
    *,
    settings: Settings | None = None,
) -> T:
    """Call a model with a system and a user message and return the parsed answer.

    The call is booked in the ledger under the given purpose."""
    if settings is None:
        settings = load_settings()
    client = chat_model(role, reasoning_effort, settings=settings)
    return client.with_structured_output(schema).invoke(
        [("system", system), ("user", user)],
        config=call_config(purpose, settings),
    )
