"""Sends questions to the OpenAI Decisions API. This is the fallback to JEV.

It has the same interface as the JEV backend and returns a probability for each option.
The OpenAI library retries failed requests on its own, so one call can wait for
minutes in the worst case. A failed request raises an error that never includes the API
key.

DecisionsBackend is the class that does the work.
"""

import json
import logging
from typing import Any

import openai

from app.config import DECISIONS_MODEL
from app.decide.base import (
    DECISIONS_BACKEND,
    Answer,
    Choice,
    DecisionError,
    Noul,
    Question,
    Score,
    UnusableReply,
    Usage,
    estimate_usage,
    excerpt,
)

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2  # the library default, set here so the policy is visible


def _input(state: str | dict) -> str | list[dict[str, Any]]:
    """Text is sent as it is. A dict is wrapped as one user message."""
    if isinstance(state, str):
        return state
    return [{"role": "user", "content": [{"type": "input_json", "data": state}]}]


def _question_body(question_id: str, question: Question) -> dict[str, Any]:
    match question:
        case Choice():
            return {
                "type": "choice",
                "name": question_id,
                "instructions": question.instructions,
                "choices": [
                    {"value": key, "description": text}
                    for key, text in question.options.items()
                ],
            }
        case Score():
            return {
                "type": "score",
                "name": question_id,
                "instructions": question.instructions,
                "levels": [
                    {"label": f"L{index}", "description": text}
                    for index, text in enumerate(question.levels)
                ],
            }
        case Noul():
            instructions = question.instructions
            if question.true is not None and question.false is not None:
                instructions += (
                    f" Yes means: {question.true}. No means: {question.false}."
                )
            return {
                "type": "predicate",
                "name": question_id,
                "instructions": instructions,
            }
    raise TypeError(f"not a question: {question!r}")


# The answer type the API returns for each kind of question.
_ANSWER_TYPE = {Choice: "choice", Score: "score", Noul: "predicate"}


def _answer(item: Any, question: Question) -> Answer:
    if item.type == "refusal":
        return Answer("refusal", backend=DECISIONS_BACKEND)
    match question:
        case Choice():
            return Answer(
                "choice",
                choice=str(item.choice),
                probabilities={
                    str(entry.value): float(entry.probability)
                    for entry in item.probabilities
                },
                confidence=float(item.confidence),
                backend=DECISIONS_BACKEND,
            )
        case Score():
            return Answer(
                "score",
                score=float(item.score),
                probabilities={
                    str(entry.value): float(entry.probability)
                    for entry in item.probabilities
                },
                confidence=float(item.confidence),
                backend=DECISIONS_BACKEND,
            )
        case Noul():
            p = float(item.probability)
            return Answer(
                "noul",
                p=p,
                probabilities={"yes": p, "no": 1 - p},
                backend=DECISIONS_BACKEND,
            )
    raise TypeError(f"not a question: {question!r}")


def _answers(
    result: Any, questions: dict[str, Question], billed: Usage
) -> dict[str, Answer]:
    """Read the answers from a reply. If they cannot be used, raise an error that
    carries the billed usage."""
    # The library can hand back text, a list or an empty object. None of these can be used.
    items = getattr(result, "answers", None)
    if not isinstance(items, list):
        raise UnusableReply("Decisions reply has no answers", billed)
    # An item that is not an answer object names no question.
    by_name: dict[str, Any] = {}
    for item in items:
        name = getattr(item, "name", None)
        if name is not None:
            by_name[name] = item
    answers: dict[str, Answer] = {}
    for question_id, question in questions.items():
        item = by_name.get(question_id)
        if item is None:
            raise UnusableReply(
                f"Decisions reply has no answer for question {question_id!r}", billed
            )
        expected = _ANSWER_TYPE[type(question)]
        if item.type not in (expected, "refusal"):
            raise UnusableReply(
                f"Decisions answer for question {question_id!r} has type "
                f"{item.type!r}, expected {expected!r}",
                billed,
            )
        failure = None
        try:
            answers[question_id] = _answer(item, question)
        except (AttributeError, TypeError, ValueError) as exc:
            failure = (
                f"Decisions answer for question {question_id!r} is malformed: "
                f"{type(exc).__name__}: {exc}"
            )
        if failure is not None:  # raised here so the error carries no earlier cause
            raise UnusableReply(failure, billed)
    return answers


def _usage(result: Any, model: str, request: dict[str, Any]) -> Usage:
    """The token counts the reply reports, or an estimate if it reports none.

    Cached tokens are part of the input, so a higher count is cut down to match with a
    warning. The call is already paid for and must still be booked."""
    try:
        reported = result.usage
        input_tokens = reported.input_tokens
        cached = reported.input_tokens_details.cached_tokens
        if cached > input_tokens:
            logger.warning(
                "a %s call reported %d cached input tokens but %d input tokens; "
                "booking %d cached",
                model,
                cached,
                input_tokens,
                input_tokens,
            )
            cached = input_tokens
        return Usage(input_tokens, cached, reported.output_tokens)
    except (AttributeError, TypeError, ValueError):
        return estimate_usage(request)


class DecisionsBackend:
    """Answers questions with the OpenAI Decisions API. client lets tests swap in a fake."""

    name = DECISIONS_BACKEND

    def __init__(
        self,
        api_key: str,
        model: str = DECISIONS_MODEL,
        client: openai.OpenAI | None = None,
    ):
        self.model = model
        self._api_key = api_key
        self._client = (
            client
            if client is not None
            else openai.OpenAI(
                api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES
            )
        )

    def decide(
        self, state: str | dict, questions: dict[str, Question]
    ) -> tuple[dict[str, Answer], Usage]:
        request = {
            "model": self.model,
            "input": _input(state),
            "questions": [_question_body(qid, q) for qid, q in questions.items()],
        }
        failure = unreadable = None
        try:
            result = self._client.decisions.create(**request)
        except openai.APIStatusError as exc:
            # Build the message here and raise it below without the original error.
            # That error can hold a masked key or the whole header.
            failure = (
                f"Decisions error {exc.status_code}: "
                f"{excerpt(exc.response.text, self._api_key)}"
            )
        except openai.OpenAIError as exc:
            failure = f"Decisions request failed: {type(exc).__name__}"
        except json.JSONDecodeError:
            # A successful reply whose body is not valid JSON. It was billed.
            unreadable = "Decisions reply is not JSON"
        if failure is not None:
            raise DecisionError(failure)
        if unreadable is not None:
            raise UnusableReply(unreadable, estimate_usage(request))
        usage = _usage(result, self.model, request)
        return _answers(result, questions, usage), usage
