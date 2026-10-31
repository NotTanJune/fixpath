"""The saved answers for model calls, so a call made once is answered from disk later.

`enable_cache` turns the cache on. Each replayed answer is marked, and `is_cache_hit`
reads the mark, which is how the cost tracker tells a replay from a real call.
`refresh_cache` makes the calls inside a block skip the saved answers and save new ones.
"""

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.caches import BaseCache
from langchain_core.globals import set_llm_cache
from langchain_core.messages import messages_from_dict
from langchain_core.outputs import ChatGeneration, Generation

from app.config import CACHE_DIR

logger = logging.getLogger(__name__)

CACHE_PATH = CACHE_DIR / "llm_cache.sqlite"
CACHE_HIT_KEY = "cache_hit"

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache(
    key TEXT PRIMARY KEY,
    llm_string TEXT NOT NULL,
    prompt TEXT NOT NULL,
    generations TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


# True while refresh_cache is active. It follows the caller but stays out of other tasks.
_refreshing: ContextVar[bool] = ContextVar("refresh_cache", default=False)


@contextmanager
def refresh_cache() -> Iterator[None]:
    """Make model calls inside the block ignore saved answers and save new ones.

    Use it to replace a bad saved answer, such as a refusal."""
    token = _refreshing.set(True)
    try:
        yield
    finally:
        _refreshing.reset(token)


def is_cache_hit(generation: Generation) -> bool:
    """Say whether an answer was replayed from the cache."""
    return bool((generation.generation_info or {}).get(CACHE_HIT_KEY))


def _encode(generations: list[Generation]) -> str:
    """Turn the answers into JSON text for storage."""
    items = []
    for generation in generations:
        item: dict[str, Any] = {"generation_info": generation.generation_info}
        if isinstance(generation, ChatGeneration):
            item["message"] = generation.message.model_dump(mode="json")
        else:
            item["text"] = generation.text
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def _decode(text: str) -> list[Generation]:
    generations: list[Generation] = []
    for item in json.loads(text):
        if "message" in item:
            data = item["message"]
            (message,) = messages_from_dict([{"type": data["type"], "data": data}])
            generations.append(
                ChatGeneration(message=message, generation_info=item["generation_info"])
            )
        else:
            generations.append(
                Generation(text=item["text"], generation_info=item["generation_info"])
            )
    return generations


def _without_replay_cost(prompt: str) -> str:
    """Remove the cost field that langchain adds to a replayed message.

    Without this, a replayed message would look different from the original in the next
    round, and the lookup would miss. A prompt without the field is returned unchanged."""
    if '"total_cost"' not in prompt:
        return prompt
    try:
        messages = json.loads(prompt)
    except ValueError:
        return prompt
    removed = False
    for message in messages if isinstance(messages, list) else []:
        kwargs = message.get("kwargs") if isinstance(message, dict) else None
        usage = kwargs.get("usage_metadata") if isinstance(kwargs, dict) else None
        if isinstance(usage, dict) and "total_cost" in usage:
            del usage["total_cost"]
            removed = True
    # Use the same separators as langchain.
    return json.dumps(messages) if removed else prompt


class SQLiteLLMCache(BaseCache):
    """A model cache kept in one database file.

    Each replayed answer is marked, so the cost tracker can tell it from a real call.
    A save that fails is logged and skipped, because the call is already paid for."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else CACHE_PATH

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute(_CACHE_SCHEMA)
        return conn

    @staticmethod
    def _key(prompt: str, llm_string: str) -> str:
        # The length keeps the two strings from running together.
        joined = f"{len(llm_string)}:{llm_string}{_without_replay_cost(prompt)}"
        return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()

    def lookup(self, prompt: str, llm_string: str) -> list[Generation] | None:
        if _refreshing.get():
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT generations FROM llm_cache WHERE key = ?",
                (self._key(prompt, llm_string),),
            ).fetchone()
        if row is None:
            return None
        try:
            generations = _decode(row[0])
        except Exception:
            logger.warning("ignoring an unreadable model cache entry", exc_info=True)
            return None
        for generation in generations:
            generation.generation_info = {
                **(generation.generation_info or {}),
                CACHE_HIT_KEY: True,
            }
        return generations

    def update(self, prompt: str, llm_string: str, return_val) -> None:
        try:
            generations = _encode(list(return_val))
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache"
                    "(key, llm_string, prompt, generations, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        self._key(prompt, llm_string),
                        llm_string,
                        prompt,
                        generations,
                        datetime.now(UTC).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            logger.warning(
                "could not store a model response in the cache", exc_info=True
            )

    def clear(self, **kwargs: Any) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM llm_cache")


def enable_cache(path: Path | str | None = None) -> SQLiteLLMCache:
    """Turn on the cache for every model in this process."""
    cache = SQLiteLLMCache(path)
    set_llm_cache(cache)
    return cache
