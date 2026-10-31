"""Checks each claim against the source quotes it cites.

A model reads every claim next to its quotes and says how well they support it. A claim
is kept if the support is at least partly there with enough confidence. Anything else,
including a missing answer, goes to the unverified list, which shows in the trace and
never on the card.

verify judges the claims and partition splits them into kept and unverified.
"""

from app.ask.types import Citation, Claim
from app.decide import Answer, Choice, Decider, DecisionError
from app.ingest.questions import DECIDER_ERROR, NO_DECISION, probability

PURPOSE = "verify"
MIN_P = 0.5

SUPPORT_OPTIONS = {
    "supported": "The quotes say what the claim says, or it follows directly from them",
    "partly": "The quotes support part of the claim, or support it only loosely",
    "not_supported": "The quotes do not support the claim, or they contradict it",
}
VERDICTS = {
    "supported": "supported",
    "partly": "partly",
    "not_supported": "unsupported",
}
KEPT = ("supported", "partly")


def support_mass(answer: Answer) -> float:
    """How likely the quotes support the claim at least partly."""
    return sum(answer.probabilities.get(choice, 0.0) for choice in KEPT)


def _question(claim_id: str) -> Choice:
    return Choice(
        f"Do the quotes listed for {claim_id} in the state support that claim? Judge "
        "the claim only against its quotes.",
        dict(SUPPORT_OPTIONS),
    )


def _judged(claim: Claim, answer: Answer | None) -> Claim:
    p = probability(answer) if answer is not None and answer.kind == "choice" else None
    if p is None or answer.choice not in VERDICTS:
        return claim.model_copy(update={"reason": NO_DECISION})
    return claim.model_copy(
        update={
            "verdict": VERDICTS[answer.choice],
            "p": p,
            "support": support_mass(answer),
        }
    )


def verify(
    claims: list[Claim], citations: dict[str, Citation], decider: Decider
) -> list[Claim]:
    """Judge every claim against the quotes it cites. A claim with no usable source is
    not asked about. The claims passed in are not changed."""
    quotes = {
        claim.id: [
            citations[cid].quote for cid in claim.citation_ids if cid in citations
        ]
        for claim in claims
    }
    asked = [claim for claim in claims if quotes[claim.id]]
    answers: dict[str, Answer] = {}
    error = None
    if asked:
        state = {
            "claims": [
                {"id": claim.id, "text": claim.text, "quotes": quotes[claim.id]}
                for claim in asked
            ]
        }
        try:
            answers = decider.ask(
                state, {c.id: _question(c.id) for c in asked}, PURPOSE
            )
        except DecisionError as exc:
            error = f"{DECIDER_ERROR}: {type(exc).__name__}"
    judged = []
    for claim in claims:
        if not quotes[claim.id]:
            judged.append(
                claim.model_copy(update={"reason": "no cited source was found"})
            )
        elif error:
            judged.append(claim.model_copy(update={"reason": error}))
        else:
            judged.append(_judged(claim, answers.get(claim.id)))
    return judged


def is_kept(claim: Claim) -> bool:
    """Whether the verifier found enough support to keep the claim."""
    return claim.support is not None and claim.support >= MIN_P


def failed_note(claim: Claim, subject: str) -> str:
    """Why a judged claim is not shown, in words for the technician."""
    if claim.verdict in ("supported", "partly"):
        return f"{subject}'s support was not certain enough to show it"
    return f"{subject} was not supported by its sources, so it is not shown"


def partition(claims: list[Claim]) -> tuple[list[Claim], list[Claim]]:
    """Split the claims into kept and unverified, each in order."""
    return [c for c in claims if is_kept(c)], [c for c in claims if not is_kept(c)]
