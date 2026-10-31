"""Tests for what the rule-based extractors have in common: the module that lists
them and the types they return. No test calls a model; a call would fail the test.
"""

import pytest
from ingest_stubs import (
    ALIASES,
    EXPORT,
    OLD_COUNT,
    WORKBOOK,
    no_model,  # noqa: F401  (an autouse fixture)
)
from pydantic import ValidationError

from app.config import RAW_DIR
from app.ingest import parsers
from app.ingest.types import CandidateEdge, Extraction


# --- The shared types -----------------------------------------------------------


def real_extractions() -> dict[str, Extraction]:
    """The extraction of every file that a deterministic extractor reads."""
    found = {
        "inventory/spares_2026-10.xlsx": parsers.parse_inventory(WORKBOOK),
        "inventory/spares_old.csv": parsers.parse_inventory(OLD_COUNT),
        "maintenance/maint_log_export_2026.csv": parsers.parse_maintenance_csv(
            EXPORT, ALIASES
        ),
    }
    for path in sorted((RAW_DIR / "logs").iterdir()):
        found[f"logs/{path.name}"] = parsers.parse_log(path, ALIASES)
    return found


def test_every_real_extraction_reads_back_equal_from_json():
    for name, extraction in real_extractions().items():
        text = extraction.model_dump_json()
        assert Extraction.model_validate_json(text) == extraction, name


def test_the_same_file_gives_the_same_extraction_every_time():
    first = {k: v.model_dump_json() for k, v in real_extractions().items()}
    second = {k: v.model_dump_json() for k, v in real_extractions().items()}
    assert first == second


def test_every_real_extraction_has_evidence_on_every_node_and_edge():
    for name, extraction in real_extractions().items():
        assert extraction.nodes, name
        for fact in [*extraction.nodes, *extraction.edges]:
            assert fact.evidence, name
            for item in fact.evidence:
                assert item.doc_path == extraction.doc_path
                assert item.extractor == extraction.extractor
                assert 0 < len(item.quote) <= 300


def test_every_real_extraction_uses_node_types_and_relations_from_the_ontology():
    from app.ontology import NODE_TYPES, is_allowed

    for name, extraction in real_extractions().items():
        for node in extraction.nodes:
            assert node.ref.type in NODE_TYPES, name
        for edge in extraction.edges:
            assert is_allowed(edge.src.type, edge.rel, edge.dst.type), (name, edge)


def test_a_node_ref_is_a_hashable_value():
    from app.ingest.types import NodeRef

    first = NodeRef(type="Asset", key="HP-01")
    assert first == NodeRef(type="Asset", key="HP-01")
    assert len({first, NodeRef(type="Asset", key="HP-01")}) == 1
    with pytest.raises(ValidationError):
        first.key = "HP-02"


def test_the_extractor_names_are_the_ones_of_the_contract():
    from typing import get_args

    from app.ingest.types import ExtractorName

    assert get_args(ExtractorName) == (
        "log-parser",
        "inventory-normalizer",
        "csv-parser",
        "sol-text",
        "sol-manual",
        "sol-doc",  # added with the document reader
        "linker",  # added with the resolver, for the links it creates
    )


def test_evidence_and_extractions_check_the_values_the_contract_fixes():
    from app.ingest.types import EvidenceDraft

    def evidence(**changes):
        values = {
            "doc_path": "logs/a.log",
            "quote": "q",
            "extractor": "log-parser",
            "confidence": 0.5,
        }
        return EvidenceDraft(**{**values, **changes})

    assert evidence().page is None
    for bad in ({"confidence": 1.5}, {"confidence": -0.1}, {"extractor": "magic"}):
        with pytest.raises(ValidationError):
            evidence(**bad)
    with pytest.raises(ValidationError):
        Extraction(doc_path="a", doc_type="spreadsheet", extractor="log-parser")


def test_an_evidence_quote_is_at_most_three_hundred_characters():
    from app.ingest.types import EvidenceDraft

    def evidence(quote: str) -> EvidenceDraft:
        return EvidenceDraft(
            doc_path="a", quote=quote, extractor="log-parser", confidence=1
        )

    assert len(evidence("x" * 300).quote) == 300
    with pytest.raises(ValidationError, match="300"):
        evidence("x" * 301)


def test_the_types_leave_checking_node_types_and_relations_to_the_validator():
    from app.ingest.types import NodeRef

    odd = CandidateEdge(
        src=NodeRef(type="Nope", key="x"), rel="NOPE", dst=NodeRef(type="Nope", key="y")
    )
    assert odd.rel == "NOPE" and odd.evidence == [] and odd.props == {}
