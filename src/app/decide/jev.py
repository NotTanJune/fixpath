"""Sends questions to the TypeSafe JEV service and reads its answers.

JEV picks from the options it is given and returns a probability for each. It never
writes text. A busy or failing service is retried a few times with a short wait. Any
other error is raised without ever including the API key. A reply that was billed is
always booked, even when its answers cannot be used.

JevBackend is the class that does the work.
"""

import math
import time
from typing import Any

import httpx

from app.config import JEV_MODEL
from app.decide.base import (
    JEV_BACKEND,
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

API_URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
MAX_RETRY_AFTER_SECONDS = 10.0
BACKOFF_SECONDS = 2.0  # multiplied by the attempt number when the service gives no wait time


def _question_body(question: Question) -> dict[str, Any]:
    match question:
        case Choice():
            return {
                "type": "choice",
                "instructions": question.instructions,
                "criteria": question.options,
            }
        case Score():
            return {
                "type": "score",
                "instructions": question.instructions,
                "criteria": question.levels,
            }
        case Noul():
            body: dict[str, Any] = {
                "type": "noul",
                "instructions": question.instructions,
            }
            if question.true is not None and question.false is not None:
                body["criteria"] = {"true": question.true, "false": question.false}
            return body
    raise TypeError(f"not a question: {question!r}")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before the next attempt, never more than 10."""
    try:
        seconds = float(response.headers.get("retry-after", ""))
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds):
        seconds = BACKOFF_SECONDS * attempt
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)


def _floats(probabilities: dict) -> dict[str, float]:
    return {str(name): float(value) for name, value in probabilities.items()}


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _answer(item: dict, question: Question) -> Answer:
    match question:
        case Choice():
            return Answer(
                "choice",
                choice=str(item["choice"]),
                probabilities=_floats(item["probabilities"]),
                confidence=_optional_float(item.get("confidence")),
                backend=JEV_BACKEND,
            )
        case Score():
            return Answer(
                "score",
                score=float(item["score"]),
                probabilities=_floats(item["probabilities"]),
                confidence=_optional_float(item.get("confidence")),
                backend=JEV_BACKEND,
            )
        case Noul():
            p = float(item["noul"])
            return Answer(
                "noul",
                p=p,
                probabilities={"yes": p, "no": 1 - p},
                backend=JEV_BACKEND,
            )
    raise TypeError(f"not a question: {question!r}")


def _answers(
    data: dict, questions: dict[str, Question], billed: Usage
) -> dict[str, Answer]:
    """Read the answers from a reply. If they cannot be used, raise an error that
    carries the billed usage."""
    raw = data.get("answers")
    if not isinstance(raw, dict):
        raise UnusableReply("JEV reply has no answers", billed)
    answers: dict[str, Answer] = {}
    for question_id, question in questions.items():
        item = raw.get(question_id)
        if not isinstance(item, dict):
            raise UnusableReply(
                f"JEV reply has no answer for question {question_id!r}", billed
            )
        failure = None
        try:
            answers[question_id] = _answer(item, question)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            failure = (
                f"JEV answer for question {question_id!r} is malformed: "
                f"{type(exc).__name__}: {exc}"
            )
        if failure is not None:  # raised here so the error carries no earlier cause
            raise UnusableReply(failure, billed)
    return answers


def _usage(data: dict, body: dict) -> Usage:
    """The tokens the reply reports, or an estimate if it reports none."""
    try:
        reported = data["usage"]
        return Usage(
            input_tokens=reported["input_tokens"],
            output_tokens=reported.get("output_tokens", 0),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return estimate_usage(body)


class JevBackend:
    """Answers questions with TypeSafe JEV. http lets tests swap in a fake client."""

    name = JEV_BACKEND

    def __init__(
        self,
        api_key: str,
        model: str = JEV_MODEL,
        http: httpx.Client | None = None,
    ):
        self.model = model
        self._api_key = api_key
        self._http = http if http is not None else httpx.Client()

    def decide(
        self, state: str | dict, questions: dict[str, Question]
    ) -> tuple[dict[str, Answer], Usage]:
        body = {
            "model": self.model,
            "state": state,
            "questions": {qid: _question_body(q) for qid, q in questions.items()},
        }
        data = self._post(body)
        usage = _usage(data, body)
        return _answers(data, questions, usage), usage

    def _post(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            failure = None
            try:
                response = self._http.post(
                    API_URL, json=body, headers=headers, timeout=TIMEOUT_SECONDS
                )
            except httpx.HTTPError as exc:
                # Keep only the error class name. Some errors quote the whole header,
                # which holds the key, so the error is raised below without its cause.
                failure = f"JEV request failed: {type(exc).__name__}"
            if failure is not None:
                raise DecisionError(failure)
            if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                break
            time.sleep(_retry_delay(response, attempt))
        if not response.is_success:
            raise DecisionError(
                f"JEV error {response.status_code}: "
                f"{excerpt(response.text, self._api_key)}"
            )
        # The request was billed from here on. A reply that cannot be read must still
        # be booked, so its error carries an estimate.
        data, failure = None, None
        try:
            data = response.json()
        except ValueError:
            failure = "JEV reply is not JSON"
        if failure is None and not isinstance(data, dict):
            failure = "JEV reply is not a JSON object"
        if failure is not None:
            raise UnusableReply(failure, estimate_usage(body))
        return data
