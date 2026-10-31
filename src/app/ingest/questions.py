"""The questions the pipeline asks the model when it must decide something.

The model is asked whether two records are the same machine, part or incident, and
which root cause explains a breakdown. This file holds the wording of those
questions, the probability thresholds that turn an answer into an action, and ask,
which sends a question and handles an answer that cannot be used.
"""

import math
from dataclasses import dataclass
from typing import Any, Literal

from app.decide import Answer, Choice, Decider, DecisionError, Noul, Question

# The thresholds that turn a probability into an action, tuned by testing. At or above
# a merge threshold the records are merged. Between the queue and merge thresholds
# a person reviews them. Below the queue threshold they are not a match. Root causes
# work the same way with the link and link_review thresholds.
GATES = {
    "merge_entity": 0.7,
    "queue_entity": 0.5,
    "merge_incident": 0.9,
    "queue_incident": 0.5,
    "link": 0.75,
    "link_review": 0.4,
}

# The cost ledger label for each kind of question.
PURPOSES = {
    "Asset": "merge Asset",
    "Part": "merge Part",
    "Incident": "merge Incident",
    "root_cause": "link root cause",
}

SAME_ENTITY = Noul(
    "Does the mention refer to the candidate? Spelling variants, abbreviations, "
    "shop-floor names and vendor numbers for the same item count as the same."
)
SAME_INCIDENT = Noul(
    "Do these two records describe the same breakdown? Records of one breakdown "
    "come from different documents, use different words, and can differ a little "
    "in time or downtime."
)
ROOT_CAUSE_INSTRUCTIONS = (
    "Which root cause explains this breakdown? Pick the option that the record, the "
    "alarm and the listed causes support best, or unknown when none of them is "
    "supported."
)
# The answer option that means none of the listed root causes fits.
UNKNOWN = "unknown"
UNKNOWN_DESCRIPTION = "None of the listed root causes is supported by the record"

MAX_CANDIDATE_QUOTES = 2
NO_DECISION = "no decision"
# The start of the reason given when the model call failed.
DECIDER_ERROR = "decider error"


def merge_band(
    p: float, kind: Literal["entity", "incident"]
) -> Literal["merge", "queue", "none"]:
    """Say whether a probability is high enough to merge, to send to review, or neither."""
    if p >= GATES[f"merge_{kind}"]:
        return "merge"
    if p >= GATES[f"queue_{kind}"]:
        return "queue"
    return "none"


def link_band(p: float) -> Literal["link", "review", "none"]:
    """Say whether a probability is high enough to link, to send to review, or neither."""
    if p >= GATES["link"]:
        return "link"
    if p >= GATES["link_review"]:
        return "review"
    return "none"


def probability(answer: Answer) -> float | None:
    """Return the probability to compare with a threshold, or None if there is no decision.

    None covers a refusal, an answer of the wrong kind, and a missing or invalid number.
    """
    if answer.kind == "noul":
        p = answer.p
    elif answer.kind == "choice" and answer.choice is not None:
        p = answer.probabilities.get(answer.choice)
    else:
        return None
    if p is None or not math.isfinite(p) or not 0 <= p <= 1:
        return None
    return p


def entity_state(
    *,
    type: str,
    text: str,
    quote: str,
    doc_path: str,
    candidate_id: str,
    label: str,
    description: str,
    aliases: list[str],
    quotes: list[str],
) -> dict[str, Any]:
    """Describe a mention and the candidate it may refer to, for the model to compare."""
    return {
        "mention": {"type": type, "text": text, "quote": quote, "doc_path": doc_path},
        "candidate": {
            "id": candidate_id,
            "label": label,
            "description": description,
            "aliases": list(aliases),
            "evidence": list(quotes[:MAX_CANDIDATE_QUOTES]),
        },
    }


def incident_summary(
    *,
    source_type: str | None = None,
    date: str | None = None,
    shift: str | None = None,
    machine: str | None = None,
    alarm: str | None = None,
    symptom: str | None = None,
    action: str | None = None,
    parts: list[dict[str, Any]] | None = None,
    technician: str | None = None,
    downtime_min: int | None = None,
    quote: str | None = None,
) -> dict[str, Any]:
    """Describe one incident record in the form the model reads."""
    return {
        "source_type": source_type,
        "date": date,
        "shift": shift,
        "machine": machine,
        "alarm": alarm,
        "symptom": symptom,
        "action": action,
        "parts": list(parts or []),
        "technician": technician,
        "downtime_min": downtime_min,
        "quote": quote,
    }


def incident_state(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Put two incident summaries side by side for the model to compare."""
    return {"record_a": a, "record_b": b}


def root_cause_question(options: dict[str, str]) -> Choice:
    """Build the root cause question from the given options, plus an unknown option."""
    return Choice(ROOT_CAUSE_INSTRUCTIONS, {**options, UNKNOWN: UNKNOWN_DESCRIPTION})


def root_cause_state(
    incident: dict[str, Any], alarms: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe an incident and the alarms behind it, with the manual's listed causes."""
    return {"incident": incident, "alarms": list(alarms)}


@dataclass(frozen=True)
class Verdict:
    """The outcome of one question: a probability, or no decision with the reason why."""

    p: float | None
    backend: str | None
    reason: str | None
    answer: Answer | None = None


def ask(
    decider: Decider, state: Any, qid: str, question: Question, purpose: str
) -> Verdict:
    """Ask the model one question and handle an answer that cannot be used.

    A refusal, a failed call or an unusable answer counts as no decision, and never
    merges or links anything. Budget and missing-key errors are not caught, so they
    stop the run instead of passing silently.
    """
    try:
        answers = decider.ask(state, {qid: question}, purpose)
    except DecisionError as error:
        return Verdict(None, None, f"{DECIDER_ERROR}: {type(error).__name__}")
    answer = answers.get(qid)
    p = probability(answer) if answer is not None else None
    backend = (answer.backend or None) if answer is not None else None
    if p is None:
        return Verdict(None, backend, NO_DECISION, answer)
    return Verdict(p, backend, None, answer)
