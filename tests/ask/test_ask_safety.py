"""Tests of the safety block, which is plain code. It reads the lockout paragraph and the
procedure warning from the manuals and decides what the answer card must say.
"""

import pytest
from crew_stubs import CNC_LOTO, HP_LOTO, HP_WARNING, open_crew_kg
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.ask import tools
from app.ask.safety import FIXED_LINE, safety_block
from app.ask.types import Citation, Fix, GraphFacts, Triage

STATUS = dict(status="act")


@pytest.fixture
def conn(tmp_path):
    connection = open_crew_kg(tmp_path)
    yield connection
    connection.close()


def facts_for(conn, asset="HP-01", modes=("FM-SEAL-WEAR",)):
    fixes = [fix for mode in modes for fix in tools.fixes_for(conn, mode)]
    return GraphFacts(asset_id=asset, fixes=fixes)


def flag(p, asset="HP-01"):
    return Triage(asset_id=asset, safety_critical=p, **STATUS)


def test_a_fix_that_requires_lockout_makes_the_block_required(conn):
    block = safety_block(facts_for(conn), flag(0.05), conn)
    assert block.required
    assert block.procedure_ids == ["PROC-HP-04"]
    assert "PROC-HP-04" in block.reason and "lockout" in block.reason.lower()


def test_the_text_is_the_machines_own_paragraph_then_the_fixed_line(conn):
    block = safety_block(facts_for(conn), flag(0.05), conn)
    assert block.text.startswith(
        "Switch off the main isolator and fit your lock and tag."
    )
    assert "accumulator keeps stored hydraulic energy" in block.text
    assert not block.text.startswith("1.2")  # the heading is not part of the paragraph
    assert block.text.endswith(
        "Apply lockout and tagout before starting. Verify zero energy."
    )
    assert FIXED_LINE == "Apply lockout and tagout before starting. Verify zero energy."
    assert "spindle" not in block.text  # not the lathe's paragraph


def test_the_citations_are_the_paragraph_chunk_and_the_procedure_warning_chunk(conn):
    block = safety_block(facts_for(conn), flag(0.05), conn)
    assert [c.chunk_id for c in block.citations] == [HP_LOTO, HP_WARNING]
    paragraph, warning = block.citations
    assert (paragraph.doc_path, paragraph.page) == ("manuals/hp.pdf", 2)
    assert (warning.doc_path, warning.page) == ("manuals/hp.pdf", 5)
    assert (
        warning.quote
        == "WARNING: Apply lockout and tagout before starting. Verify zero energy."
    )


def test_a_lathe_gets_the_lathe_paragraph(conn):
    block = safety_block(GraphFacts(asset_id="CNC-01"), flag(0.9, asset="CNC-01"), conn)
    assert block.required and "compressed air" in block.text
    assert [c.chunk_id for c in block.citations] == [CNC_LOTO]
    assert block.procedure_ids == []


def test_a_fix_that_needs_no_lockout_and_a_low_flag_gives_no_block(conn):
    block = safety_block(
        facts_for(conn, modes=("FM-RELIEF-MISADJUSTED",)), flag(0.49), conn
    )
    assert not block.required
    assert block.text == "" and block.citations == [] and block.procedure_ids == []


def test_the_safety_flag_from_half_makes_the_block_required(conn):
    low, edge = safety_block(GraphFacts(asset_id="HP-01"), flag(0.49), conn), None
    assert not low.required
    edge = safety_block(GraphFacts(asset_id="HP-01"), flag(0.5), conn)
    assert edge.required and "hazard" in edge.reason.lower()
    assert edge.procedure_ids == [] and [c.chunk_id for c in edge.citations] == [
        HP_LOTO
    ]


def test_a_missing_flag_is_not_a_flag(conn):
    block = safety_block(GraphFacts(asset_id="HP-01"), flag(None), conn)
    assert not block.required


def test_the_flag_with_no_machine_still_warns_with_the_fixed_line_only(conn):
    block = safety_block(
        GraphFacts(), Triage(safety_critical=0.9, status="clarify"), conn
    )
    assert block.required
    assert block.text == FIXED_LINE and block.citations == []


def test_the_machine_comes_from_triage_when_the_facts_have_none(conn):
    block = safety_block(GraphFacts(), flag(0.9, asset="CNC-01"), conn)
    assert [c.chunk_id for c in block.citations] == [CNC_LOTO]


def test_both_reasons_are_given_when_both_apply(conn):
    block = safety_block(facts_for(conn), flag(0.9), conn)
    assert "PROC-HP-04" in block.reason and "hazard" in block.reason.lower()


def test_a_machine_with_no_manual_gets_the_fixed_line(conn):
    block = safety_block(GraphFacts(asset_id="RW-01"), flag(0.9, asset="RW-01"), conn)
    assert block.required and block.text == FIXED_LINE and block.citations == []


def test_a_procedure_chunk_is_found_through_the_fix_citation_when_the_manual_does_not_name_it(
    conn,
):
    fix = Fix(
        procedure_id="PROC-X-01",
        title="Other job",
        steps=["Do it."],
        requires_loto=True,
        est_minutes=10,
        citations=[
            Citation(doc_path="manuals/hp.pdf", page=5, chunk_id=HP_WARNING, quote="q")
        ],
    )
    block = safety_block(GraphFacts(asset_id="HP-01", fixes=[fix]), flag(0.0), conn)
    assert [c.chunk_id for c in block.citations] == [HP_LOTO, HP_WARNING]


# --- a safety question that could not be answered ---


def unanswered(asset="HP-01"):
    """What triage returns when the safety question got no usable answer. The flag and
    its confidence are both empty."""
    confidence = {
        "machine": 0.9,
        "alarm": 0.9,
        "root_cause": 0.9,
        "intent": 0.9,
        "safety_critical": None,
    }
    return Triage(asset_id=asset, confidence=confidence, **STATUS)


def test_an_unanswered_safety_question_with_a_known_machine_requires_the_block(conn):
    block = safety_block(GraphFacts(asset_id="HP-01"), unanswered(), conn)
    assert block.required and block.procedure_ids == []
    assert "could not be answered" in block.reason
    assert "accumulator" in block.text and block.text.endswith(FIXED_LINE)
    assert [c.chunk_id for c in block.citations] == [HP_LOTO]


def test_an_unanswered_safety_question_gives_the_fixed_line_for_a_machine_with_no_manual(
    conn,
):
    block = safety_block(GraphFacts(), unanswered("RW-01"), conn)
    assert block.required and block.text == FIXED_LINE


def test_an_unanswered_safety_question_with_no_machine_gives_no_block(conn):
    assert not safety_block(GraphFacts(), unanswered(None), conn).required


def test_an_answered_no_is_still_no_block_and_a_fix_adds_its_own_reason(conn):
    answered = unanswered().model_copy(
        update={"safety_critical": 0.05, "confidence": {"safety_critical": 0.95}}
    )
    assert not safety_block(GraphFacts(asset_id="HP-01"), answered, conn).required
    both = safety_block(facts_for(conn), unanswered(), conn)
    assert "PROC-HP-04" in both.reason and "could not be answered" in both.reason
