"""Tests for the verifier, which checks each claim in an answer against its sources. A
scripted decider stands in for the real one. The tests cover which claims are kept and
which are dropped."""

import pytest
from crew_stubs import FakeDecider, choice, refusal
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.ask.types import Citation, Claim
from app.ask.verify import MIN_P, is_kept, partition, verify
from app.decide import Choice, DecisionError
from app.ledger import BudgetExceeded

CITATIONS = {
    "C1": Citation(
        doc_path="manuals/hp.pdf",
        page=3,
        chunk_id="HP-3",
        quote="Check the seals for wear.",
    ),
    "C2": Citation(
        doc_path="notes/note.txt", quote="E-217 seal replaced again, oil on the floor."
    ),
}


def claims(n=1):
    return [
        Claim(
            id=f"claim-{i}",
            text=f"Statement {i}.",
            citation_ids=["C1", "C2"] if i == 1 else ["C2"],
        )
        for i in range(1, n + 1)
    ]


def decide(**by_claim):
    """A decider that answers each claim as given, in a single call."""
    return FakeDecider(lambda purpose, state, questions: dict(by_claim))


def supported(p=0.9):
    return choice("supported", p, {"partly": 0.05, "not_supported": 1 - p - 0.05})


def test_one_ask_holds_one_support_choice_per_claim_and_the_quotes_of_their_citations():
    fake = decide(**{"claim-1": supported(), "claim-2": supported()})
    verify(claims(2), CITATIONS, fake)
    ((purpose, state, questions),) = fake.calls
    assert purpose == "verify"
    assert list(questions) == ["claim-1", "claim-2"]
    assert all(isinstance(q, Choice) for q in questions.values())
    assert list(questions["claim-1"].options) == [
        "supported",
        "partly",
        "not_supported",
    ]
    assert "claim-1" in questions["claim-1"].instructions
    assert state == {
        "claims": [
            {
                "id": "claim-1",
                "text": "Statement 1.",
                "quotes": [
                    "Check the seals for wear.",
                    "E-217 seal replaced again, oil on the floor.",
                ],
            },
            {
                "id": "claim-2",
                "text": "Statement 2.",
                "quotes": ["E-217 seal replaced again, oil on the floor."],
            },
        ]
    }


def test_a_supported_claim_is_kept_with_its_verdict_and_p():
    (claim,) = verify(claims(), CITATIONS, decide(**{"claim-1": supported(0.9)}))
    assert (claim.verdict, claim.p, claim.reason) == ("supported", 0.9, None)
    kept, unverified = partition([claim])
    assert kept == [claim] and unverified == []


def test_a_claim_is_kept_when_supported_plus_partly_reaches_half():
    assert MIN_P == 0.5
    edge = verify(
        claims(),
        CITATIONS,
        decide(
            **{
                "claim-1": choice(
                    "partly", 0.5, {"supported": 0.0, "not_supported": 0.5}
                )
            }
        ),
    )
    assert partition(edge) == (edge, [])
    below = verify(
        claims(),
        CITATIONS,
        decide(
            **{
                "claim-1": choice(
                    "partly", 0.3, {"supported": 0.19, "not_supported": 0.51}
                )
            }
        ),
    )
    assert partition(below) == ([], below)
    assert below[0].verdict == "partly" and below[0].p == pytest.approx(0.3)
    assert below[0].support == pytest.approx(0.49)


def test_the_support_mass_is_stored_and_the_top_choice_stays_the_verdict():
    # the rehearsal summary: supported 0.47, partly 0.42, not_supported 0.11
    answer = choice("supported", 0.47, {"partly": 0.42, "not_supported": 0.11})
    (claim,) = verify(claims(), CITATIONS, decide(**{"claim-1": answer}))
    assert (claim.verdict, claim.p) == ("supported", 0.47)
    assert claim.support == pytest.approx(0.89)
    assert partition([claim]) == ([claim], [])
    assert is_kept(claim)


def test_a_supported_claim_has_its_mass_even_without_the_other_probabilities():
    (claim,) = verify(claims(), CITATIONS, decide(**{"claim-1": supported(0.9)}))
    assert claim.support == pytest.approx(0.95)


def test_a_claim_with_a_top_choice_of_not_supported_below_half_support_is_not_kept():
    answer = choice("not_supported", 0.6, {"supported": 0.3, "partly": 0.1})
    (claim,) = verify(claims(), CITATIONS, decide(**{"claim-1": answer}))
    assert claim.verdict == "unsupported" and claim.support == pytest.approx(0.4)
    assert partition([claim]) == ([], [claim])


def test_not_supported_is_unverified_whatever_its_p():
    result = verify(
        claims(), CITATIONS, decide(**{"claim-1": choice("not_supported", 0.95)})
    )
    assert result[0].verdict == "unsupported" and result[0].p == pytest.approx(0.95)
    assert partition(result) == ([], result)


def test_a_refusal_keeps_nothing_and_says_why():
    result = verify(
        claims(2), CITATIONS, decide(**{"claim-1": refusal(), "claim-2": supported()})
    )
    first, second = result
    assert first.verdict is None and first.p is None and "no decision" in first.reason
    kept, unverified = partition(result)
    assert kept == [second] and unverified == [first]


def test_a_missing_answer_is_no_decision_too():
    result = verify(claims(2), CITATIONS, decide(**{"claim-1": supported()}))
    assert result[1].verdict is None and "no decision" in result[1].reason


def test_an_unusable_probability_is_no_decision():
    result = verify(
        claims(), CITATIONS, decide(**{"claim-1": choice("supported", float("nan"))})
    )
    assert result[0].verdict is None and partition(result)[0] == []


def test_a_decider_error_sends_every_claim_to_unverified_with_the_reason():
    fake = FakeDecider(lambda purpose, state, questions: DecisionError("down"))
    result = verify(claims(2), CITATIONS, fake)
    assert [c.verdict for c in result] == [None, None]
    assert all(c.reason.startswith("decider error: DecisionError") for c in result)
    assert partition(result) == ([], result)


def test_a_budget_stop_is_not_swallowed():
    fake = FakeDecider(lambda purpose, state, questions: BudgetExceeded("spent"))
    with pytest.raises(BudgetExceeded):
        verify(claims(), CITATIONS, fake)


def test_a_claim_whose_citations_are_missing_is_unverified_without_asking():
    lone = [Claim(id="claim-1", text="Orphan.", citation_ids=["C9"])]
    fake = decide()
    (result,) = verify(lone, CITATIONS, fake)
    assert fake.calls == []
    assert result.verdict is None and "cited" in result.reason


def test_no_claims_means_no_ask():
    fake = decide()
    assert verify([], CITATIONS, fake) == []
    assert fake.calls == []


def test_the_input_claims_are_not_changed():
    original = claims()
    verify(original, CITATIONS, decide(**{"claim-1": supported()}))
    assert original[0].verdict is None
