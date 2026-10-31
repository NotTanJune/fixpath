"""Fast decisions behind one interface.

A caller asks a multiple-choice or yes/no question about some text and gets back the
chosen answer with a probability. The TypeSafe JEV service is the main backend, and the
OpenAI Decisions API is the fallback. get_decider picks one from the settings, based on
which keys are set. A live backend always runs inside CachedDecider, which adds an
answer cache, the budget stop and the spend ledger. With no key, answers can only be
replayed from the cache.
"""

from app.config import DECIDERS, Settings
from app.decide.base import (
    Answer,
    CachedDecider,
    CacheOnlyDecider,
    Choice,
    Decider,
    DecisionCache,
    DecisionError,
    NoDecisionBackend,
    Noul,
    Question,
    Score,
)
from app.decide.jev import JevBackend
from app.decide.openai_decisions import DecisionsBackend
from app.ledger import BudgetExceeded

__all__ = [
    "Answer",
    "BudgetExceeded",
    "Choice",
    "Decider",
    "DecisionError",
    "NoDecisionBackend",
    "Noul",
    "Question",
    "Score",
    "backend_name",
    "get_decider",
]


def backend_name(settings: Settings) -> str:
    """The backend the settings ask for: "jev", "decisions" or "cache" (stored answers
    only). Raises ValueError if the setting is unknown or its key is missing."""
    if settings.decider not in DECIDERS:
        raise ValueError(
            f"DECIDER must be one of {', '.join(DECIDERS)}; got {settings.decider!r}"
        )
    if settings.decider == "jev" and not settings.has_typesafe:
        raise ValueError("DECIDER=jev needs TYPESAFE_API_KEY")
    if settings.decider == "decisions" and not settings.has_openai:
        raise ValueError("DECIDER=decisions needs OPENAI_API_KEY")
    auto = settings.decider == "auto"
    if settings.decider == "jev" or (auto and settings.has_typesafe):
        return "jev"
    if settings.decider == "decisions" or (auto and settings.has_openai):
        return "decisions"
    return "cache"


def get_decider(settings: Settings) -> Decider:
    """The decider the settings ask for."""
    backend = backend_name(settings)
    cache = DecisionCache()
    if backend == "jev":
        return CachedDecider(JevBackend(settings.typesafe_api_key), cache, settings)
    if backend == "decisions":
        return CachedDecider(DecisionsBackend(settings.openai_api_key), cache, settings)
    return CacheOnlyDecider(cache)
