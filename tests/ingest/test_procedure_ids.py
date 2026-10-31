"""Tests for the ids given to the procedures that the document reader finds, and for
how those ids pass through the resolver and the validator. The input is built by hand
and the decider is scripted, so no test calls a model.
"""

import re

import pytest
from resolve_stubs import (
    ALIASES,
    BH250,
    INVENTORY,
    ScriptedDecider,
    edge,
    evidence,
    extraction,
    node,
    parts_fixture,
)

from app.ingest.procedure_ids import mint_procedure_ids
from app.ingest.resolve import Registry, resolve
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    Extraction,
    NodeRef,
    ResolvedNode,
)
from app.ingest.validate import validate
from app.ontology import validate_id

DOC = "procedures/07_pump_procedure.docx.txt"
OTHER = "procedures/08_work_instruction_gripper.html.txt"
TITLE = "Procedure: change the hydraulic oil and filter on PR-2"


def doc_extraction(doc_path: str = DOC, prefix: str | None = "HP") -> Extraction:
    """What extract_document gives for a pump procedure."""
    key = f"{doc_path}#procedure"
    props = {
        "title": TITLE,
        "steps": ["Lock out the main isolator.", "Drain the tank."],
        "requires_loto": True,
        "est_minutes": 90,
        "manual_ref": None,
    }
    if prefix is not None:
        props["id_prefix"] = prefix
    procedure = CandidateNode(
        ref=NodeRef(type="Procedure", key=key),
        label=TITLE,
        props=props,
        evidence=evidence(doc_path, TITLE, "sol-doc", 0.8),
        canonical=False,
    )
    machine = node(
        "Asset",
        "HP-01",
        doc=doc_path,
        canonical=False,
        quote=TITLE,
        extractor="sol-doc",
    )
    machine.props = {}
    part = node(
        "Part",
        "OIL-HLP46-20L",
        doc=doc_path,
        canonical=False,
        quote="OIL HLP46 20L | 3",
        extractor="sol-doc",
    )
    part.props = {}
    requires = edge(
        ("Procedure", key),
        "REQUIRES",
        ("Part", "OIL-HLP46-20L"),
        doc=doc_path,
        props={"qty": 3},
        quote="OIL HLP46 20L | 3",
        extractor="sol-doc",
    )
    return Extraction(
        doc_path=doc_path,
        doc_type="procedure",
        extractor="sol-doc",
        nodes=[procedure, machine, part],
        edges=[requires],
    )


def manual_with_procedures(*ids: str) -> Extraction:
    return extraction(
        BH250,
        "manual",
        [
            node(
                "Procedure",
                proc_id,
                doc=BH250,
                props={"id": proc_id, "title": proc_id},
                extractor="sol-manual",
            )
            for proc_id in ids
        ],
    )


def procedure_id(result: list[Extraction], index: int = 0) -> str:
    return next(n.ref.key for n in result[index].nodes if n.ref.type == "Procedure")


# --- Minting --------------------------------------------------------------------


def test_the_next_free_number_for_the_prefix_comes_after_the_graph(monkeypatch):
    result = mint_procedure_ids(
        [doc_extraction()], ["PROC-HP-04", "PROC-HP-06", "PROC-RW-01"]
    )
    assert procedure_id(result) == "PROC-HP-07"


def test_a_prefix_with_no_procedure_starts_at_one():
    assert procedure_id(mint_procedure_ids([doc_extraction()], ["PROC-RW-01"])) == (
        "PROC-HP-01"
    )
    assert procedure_id(mint_procedure_ids([doc_extraction(prefix="RW")], [])) == (
        "PROC-RW-01"
    )


def test_procedures_of_manuals_in_the_same_run_count_as_taken():
    manual = manual_with_procedures("PROC-HP-04", "PROC-HP-05", "PROC-HP-06")
    result = mint_procedure_ids([manual, doc_extraction()], [])
    assert procedure_id(result, 1) == "PROC-HP-07"


def test_two_documents_in_one_run_get_different_numbers_in_processing_order():
    result = mint_procedure_ids(
        [doc_extraction(), doc_extraction(OTHER, "HP")], ["PROC-HP-04"]
    )
    assert (procedure_id(result, 0), procedure_id(result, 1)) == (
        "PROC-HP-05",
        "PROC-HP-06",
    )


def test_ids_never_collide_with_each_other_or_with_the_graph():
    known = [f"PROC-HP-{n:02d}" for n in (1, 2, 5)] + ["PROC-RW-03"]
    files = [doc_extraction(f"procedures/p{n}.txt", "HP") for n in range(4)]
    files += [doc_extraction("procedures/q.txt", "RW")]
    ids = [procedure_id(mint_procedure_ids(files, known), i) for i in range(5)]
    assert ids == [
        "PROC-HP-06",
        "PROC-HP-07",
        "PROC-HP-08",
        "PROC-HP-09",
        "PROC-RW-04",
    ]
    assert not set(ids) & set(known)
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("prefix", ["HP", "CNC", "RW", "LT", "DOC"])
def test_every_minted_id_matches_the_procedure_id_pattern(prefix):
    result = mint_procedure_ids([doc_extraction(prefix=prefix)], [])
    minted = procedure_id(result)
    assert validate_id("Procedure", minted)
    assert re.fullmatch(r"PROC-[A-Z]{2,3}-\d{2}", minted)


def test_the_node_and_its_edges_use_the_minted_id_and_the_node_is_canonical():
    original = doc_extraction()
    (result,) = mint_procedure_ids([original], ["PROC-HP-04"])
    procedure = next(n for n in result.nodes if n.ref.type == "Procedure")
    assert procedure.canonical is True
    assert procedure.ref.key == "PROC-HP-05"
    assert procedure.props["id"] == "PROC-HP-05"
    assert "id_prefix" not in procedure.props
    assert procedure.label == TITLE
    assert [(e.src.key, e.dst.key) for e in result.edges] == [
        ("PROC-HP-05", "OIL-HLP46-20L")
    ]
    assert [n.ref.type for n in result.nodes] == ["Procedure", "Asset", "Part"]


def test_the_input_is_not_changed_and_other_extractions_are_returned_as_they_are():
    original = doc_extraction()
    before = original.model_dump()
    manual = manual_with_procedures("PROC-HP-04")
    result = mint_procedure_ids([manual, original], [])
    assert original.model_dump() == before
    assert result[0] is manual


def test_minting_is_the_same_for_the_same_input():
    first = mint_procedure_ids([doc_extraction()], ["PROC-HP-04"])
    second = mint_procedure_ids([doc_extraction()], ["PROC-HP-04"])
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]


def test_a_prefix_with_no_numbers_left_drops_the_procedure_with_a_note():
    known = [f"PROC-HP-{n:02d}" for n in range(1, 100)]
    (result,) = mint_procedure_ids([doc_extraction()], known)
    assert [n.ref.type for n in result.nodes] == ["Asset", "Part"]
    assert result.edges == []
    assert any("PROC-HP" in note and "used" in note for note in result.notes)


def test_a_bad_prefix_falls_back_to_the_placeholder_prefix():
    (result,) = mint_procedure_ids([doc_extraction(prefix="x1")], [])
    assert procedure_id([result]) == "PROC-DOC-01"
    (missing,) = mint_procedure_ids([doc_extraction(prefix=None)], [])
    assert procedure_id([missing]) == "PROC-DOC-01"


# --- Through the resolver and the validator -------------------------------------


def resolved(extractions, registry=None):
    decider = ScriptedDecider(0.0)
    machines = extraction(
        BH250,
        "manual",
        [
            node(
                "Asset",
                "HP-01",
                doc=BH250,
                props={
                    "id": "HP-01",
                    "name": "Hydraulic press",
                    "machine_type": "press",
                    "vendor": "Bramwell Hydraulics",
                    "model": "BH-250",
                },
                extractor="sol-manual",
            )
        ],
    )
    return resolve(
        [*parts_fixture(), machines, *extractions], ALIASES, decider, registry
    )


def by_id(resolution, node_id):
    return next(n for n in resolution.nodes if n.id == node_id)


def test_the_resolver_mints_the_id_and_keeps_the_procedure_and_its_part_edge():
    resolution = resolved([doc_extraction()])
    procedure = by_id(resolution, "PROC-HP-01")
    assert procedure.type == "Procedure"
    assert procedure.props["id"] == "PROC-HP-01"
    assert (
        procedure.props["requires_loto"] is True
        and procedure.props["est_minutes"] == 90
    )
    assert procedure.sources == [DOC]
    edges = [e for e in resolution.edges if e.rel == "REQUIRES"]
    assert [(e.src, e.dst, e.props) for e in edges] == [
        ("PROC-HP-01", "OIL-HLP46-20L", {"qty": 3})
    ]
    assert edges[0].evidence[0].quote == "OIL HLP46 20L | 3"


def test_the_machine_and_the_part_get_the_document_as_evidence():
    resolution = resolved([doc_extraction()])
    machine = by_id(resolution, "HP-01")
    assert DOC in machine.sources
    assert any(e.doc_path == DOC and e.quote == TITLE for e in machine.evidence)
    part = by_id(resolution, "OIL-HLP46-20L")
    assert any(
        e.doc_path == DOC and e.quote == "OIL HLP46 20L | 3" for e in part.evidence
    )


def test_the_number_follows_the_procedures_the_registry_already_holds():
    registry = Registry(
        nodes=[
            ResolvedNode(id="PROC-HP-04", type="Procedure", label="x"),
            ResolvedNode(id="PROC-HP-05", type="Procedure", label="x"),
        ]
    )
    resolution = resolved([doc_extraction()], registry)
    assert by_id(resolution, "PROC-HP-06").type == "Procedure"


def test_the_validator_accepts_the_resolved_procedure():
    validated = validate(resolved([doc_extraction()]))
    errors = [f for f in validated.flags if f.severity == "error"]
    assert errors == []
    assert {n.id for n in validated.nodes} >= {"PROC-HP-01", "HP-01", "OIL-HLP46-20L"}
    assert any(e.rel == "REQUIRES" and e.src == "PROC-HP-01" for e in validated.edges)


def test_without_a_document_reader_extraction_the_resolution_is_the_same():
    plain = resolved([])
    assert not [n for n in plain.nodes if n.type == "Procedure"]
    inventory = extraction(INVENTORY, "inventory")
    assert mint_procedure_ids([inventory], []) == [inventory]


def test_a_candidate_edge_type_is_unchanged_by_minting():
    (result,) = mint_procedure_ids([doc_extraction()], [])
    assert all(isinstance(e, CandidateEdge) for e in result.edges)
