"""Tests for the types that the resolver and the linker pass on to the validator."""

import pytest
from pydantic import ValidationError

from app.ingest.types import (
    EvidenceDraft,
    ExtractorName,
    MergeDecision,
    Resolution,
    ResolvedEdge,
    ResolvedNode,
    ReviewItem,
    is_missing,
)


def evidence() -> EvidenceDraft:
    return EvidenceDraft(
        doc_path="a.txt", quote="q", extractor="linker", confidence=0.8
    )


def test_linker_is_an_extractor_name():
    assert "linker" in ExtractorName.__args__


def test_resolution_round_trips_through_json():
    resolution = Resolution(
        nodes=[
            ResolvedNode(
                id="HP-01",
                type="Asset",
                label="Press",
                props={"aliases": ["the press"]},
                evidence=[evidence()],
                sources=["inventory/x.xlsx"],
            )
        ],
        edges=[
            ResolvedEdge(
                src="HP-01", rel="RAISED", dst="E-217", props={"count": 2}, evidence=[]
            )
        ],
        merges=[
            MergeDecision(
                doc_path="a.txt",
                type="Part",
                mention="HLP46",
                candidate="OIL-HLP46-20L",
                p=0.95,
                decision="merge",
                backend="fake",
            )
        ],
        review=[
            ReviewItem(
                kind="merge", doc_paths=["a.txt"], payload={"p": 0.7}, confidence=0.7
            )
        ],
    )
    assert Resolution.model_validate_json(resolution.model_dump_json()) == resolution


def test_resolution_lists_default_to_empty():
    resolution = Resolution()
    assert (
        resolution.nodes,
        resolution.edges,
        resolution.merges,
        resolution.review,
    ) == (
        [],
        [],
        [],
        [],
    )


def test_a_merge_decision_takes_only_the_five_decisions():
    for decision in ("rule", "merge", "queue", "unresolved", "new"):
        MergeDecision(
            doc_path="a",
            type="Part",
            mention="m",
            candidate=None,
            p=None,
            decision=decision,
            backend=None,
        )
    with pytest.raises(ValidationError):
        MergeDecision(
            doc_path="a",
            type="Part",
            mention="m",
            candidate=None,
            p=None,
            decision="maybe",
            backend=None,
        )


def test_a_review_item_takes_only_the_four_kinds():
    for kind in ("merge", "conflict", "unresolved", "link"):
        ReviewItem(kind=kind, doc_paths=[], payload={}, confidence=None)
    with pytest.raises(ValidationError):
        ReviewItem(kind="other", doc_paths=[], payload={}, confidence=None)


@pytest.mark.parametrize("value", [None, "", "  ", "NA", "N/A", "n/a", "-", "?", []])
def test_placeholders_are_missing(value):
    assert is_missing(value)


@pytest.mark.parametrize("value", [0, 0.0, False, "x", "none", [""], ["a"], {"a": 1}])
def test_real_values_are_not_missing(value):
    assert not is_missing(value)
