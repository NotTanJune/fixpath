"""Paths, model names and settings.

Settings come from environment variables first, then from the .env file. Extra spaces
are trimmed, and an empty value counts as unset. `load_settings` reads and checks them.
"""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
TRUTH_DIR = DATA_DIR / "truth"
KG_PATH = DATA_DIR / "kg.sqlite"
CACHE_DIR = DATA_DIR / "cache"

SOL_MODEL = "gpt-6.1-sol"
TERRA_MODEL = "gpt-6.1-terra"
DECISIONS_MODEL = "gpt-6-luna"
JEV_MODEL = "jev-1.14.0"

DECIDERS = ("auto", "jev", "decisions")
DEFAULT_DECIDER = "auto"
DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"
DEFAULT_BUDGET_USD = 40.0


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = field(default=None, repr=False)
    typesafe_api_key: str | None = field(default=None, repr=False)
    langfuse_public_key: str | None = field(default=None, repr=False)
    langfuse_secret_key: str | None = field(default=None, repr=False)
    langfuse_host: str = DEFAULT_LANGFUSE_HOST
    decider: str = DEFAULT_DECIDER
    budget_usd: float = DEFAULT_BUDGET_USD

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_typesafe(self) -> bool:
        return bool(self.typesafe_api_key)

    @property
    def has_langfuse(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


def _read_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from an env file, skipping blanks and comments."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_settings(env_file: Path = ROOT / ".env") -> Settings:
    file_values = _read_env_file(env_file)

    def lookup(name: str) -> str | None:
        # Trim the value. A key that ends in a stray newline breaks web requests.
        from_environment = os.environ.get(name, "").strip()
        return from_environment or file_values.get(name) or None

    decider = lookup("DECIDER") or DEFAULT_DECIDER
    if decider not in DECIDERS:
        raise ValueError(
            f"DECIDER must be one of {', '.join(DECIDERS)}; got {decider!r}"
        )

    budget_text = lookup("BUDGET_USD")
    try:
        budget = DEFAULT_BUDGET_USD if budget_text is None else float(budget_text)
    except ValueError:
        budget = math.nan  # Not a number, so it is rejected below.
    if not (math.isfinite(budget) and budget > 0):
        raise ValueError(
            f"BUDGET_USD must be a finite number above 0; got {budget_text!r}"
        )

    return Settings(
        openai_api_key=lookup("OPENAI_API_KEY"),
        typesafe_api_key=lookup("TYPESAFE_API_KEY"),
        langfuse_public_key=lookup("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=lookup("LANGFUSE_SECRET_KEY"),
        langfuse_host=lookup("LANGFUSE_HOST") or DEFAULT_LANGFUSE_HOST,
        decider=decider,
        budget_usd=budget,
    )
