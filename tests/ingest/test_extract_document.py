"""Tests for the document reader, which handles procedures and work instructions.

No test calls a model. Each test supplies a canned model answer for a real test file.
What is tested is the work done after the answer: document type, nodes, edges, quotes
and the cache key."""

import re

import pytest
from ingest_stubs import (
    ALIASES,
    FakeModel,
    count_by_relation,
    count_by_type,
    fake_model,  # noqa: F401  (a fixture)
    no_model,  # noqa: F401  (an autouse fixture)
    nodes_of,
)
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app import llm
from app.config import DATA_DIR
from app.ingest import convert, router
from app.ingest.extract_document import (
    DOC_SYSTEM,
    EXTRACTOR,
    PURPOSE,
    DocExtraction,
    FailureModeMention,
    PartNeed,
    extract_document,
    names_procedure,
)
from app.ingest.extract_manual import ManualExtraction
from app.ingest.extract_text import NOTE_SYSTEM, NoteExtraction
from app.ingest.manual_schema import MANUAL_SYSTEM
from app.ingest.normalize import chunk_id, document_id
from app.ingest.types import Extraction
from app.ontology import is_allowed

REAL_STRUCTURED = llm.structured  # before the module fixture replaces it
TESTSET = DATA_DIR / "testset" / "ingest"


def saved(tmp_path, name: str):
    """A test-set file as the upload saves it: converted to text, in procedures/."""
    found = convert.readable(name, (TESTSET / name).read_bytes())
    path = tmp_path / "procedures" / found.name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(found.data)
    return path


def pump_answer(**changes) -> DocExtraction:
    fields = {
        "title": "Procedure: change the hydraulic oil and filter on PR-2",
        "machine_mentions": ["PR-2"],
        "steps": [
            "Lock out the main isolator and release the stored pressure.",
            "Drain the tank into the oil trolley and clean the strainer.",
            "Fill to the sight glass mark, run the pump for ten minutes and look for leaks.",
        ],
        "requires_lockout": True,
        "minutes": 90,
        "parts": [
            PartNeed(mention="HLP46", qty=3),
            PartNeed(mention="FLT HYD 10", qty=1),
        ],
        "alarm_mentions": ["E221", "E230"],
        "failure_modes": [],
    }
    return DocExtraction(**(fields | changes))


def gripper_answer(**changes) -> DocExtraction:
    fields = {
        "title": "Work instruction WI-27: replace the gripper fingers on robot cell",
        "machine_mentions": ["robot cell"],
        "steps": [
            "Stop the cell and open the light curtain muting key switch.",
            "Remove the two worn gripper finger with the 4 mm key.",
            "Fit new GRP FNG 12 and torque to 6 Nm.",
            "Run ten pick cycles and check that RB551 does not return.",
        ],
        "requires_lockout": False,
        "minutes": None,
        "parts": [PartNeed(mention="GRPFNG12", qty=2)],
        "alarm_mentions": ["RB551"],
        "failure_modes": [],
    }
    return DocExtraction(**(fields | changes))


def extract(monkeypatch, path, canned: DocExtraction) -> Extraction:
    fake = FakeModel(canned)
    monkeypatch.setattr(llm, "structured", fake)
    result = extract_document(path, ALIASES)
    return result


def node_of(result: Extraction, node_type: str, key: str):
    return next(n for n in nodes_of(result, node_type) if n.ref.key == key)


# --- The call -------------------------------------------------------------------


def test_one_sol_call_with_the_text_and_its_own_purpose(fake_model, tmp_path):  # noqa: F811
    fake_model.answer = pump_answer()
    path = saved(tmp_path, "07_pump_procedure.docx")
    extract_document(path, ALIASES)
    assert len(fake_model.calls) == 1
    call = fake_model.calls[0]
    assert call["role"] == "sol"
    assert call["schema"] is DocExtraction
    assert call["system"] == DOC_SYSTEM
    assert call["user"] == path.read_text(encoding="utf-8")
    assert call["purpose"] == PURPOSE == "extract document"


def test_the_settings_of_the_caller_go_to_the_call(fake_model, tmp_path):  # noqa: F811
    from app.config import Settings

    fake_model.answer = pump_answer()
    path = saved(tmp_path, "07_pump_procedure.docx")
    mine = Settings(budget_usd=3.0)
    extract_document(path, ALIASES, settings=mine)
    assert fake_model.calls[0]["settings"] is mine


def test_the_prompt_asks_for_exact_mentions_and_the_parts_of_a_procedure():
    text = DOC_SYSTEM.lower()
    assert "exactly as" in text
    assert "never" in text
    for word in ("procedure", "machine", "part", "alarm", "failure mode", "lockout"):
        assert word in text


def test_the_prompt_the_purpose_and_the_schema_are_not_those_of_another_reader():
    assert DOC_SYSTEM not in (NOTE_SYSTEM, MANUAL_SYSTEM)
    assert PURPOSE not in ("extract note", "extract handover", "extract manual")
    assert DocExtraction.model_json_schema() not in (
        NoteExtraction.model_json_schema(),
        ManualExtraction.model_json_schema(),
    )


def test_the_cache_key_differs_from_the_keys_of_the_other_extractors(monkeypatch):
    """The key is the prompt and the client string, which carries the schema."""
    from langchain_core.globals import set_llm_cache
    from test_llm import Recorder

    monkeypatch.setattr(llm, "structured", REAL_STRUCTURED)  # the key needs the client

    def key(schema, system, parsed):
        recorder = Recorder(parsed)
        set_llm_cache(recorder)
        llm.structured("sol", schema, system, "the same text", "key test")
        return recorder.keys[0]

    note = NoteExtraction(
        note_date_text=None,
        shift_text=None,
        from_mention=None,
        to_mention=None,
        items=[],
    )
    manual = ManualExtraction(
        vendor="",
        model="",
        components=[],
        failure_modes=[],
        alarms=[],
        procedures=[],
        spare_parts=[],
    )
    keys = {
        "document": key(DocExtraction, DOC_SYSTEM, pump_answer()),
        "note": key(NoteExtraction, NOTE_SYSTEM, note),
        "manual": key(ManualExtraction, MANUAL_SYSTEM, manual),
    }
    assert len(set(keys.values())) == 3
    assert len({prompt for prompt, _ in keys.values()}) == 3
    assert len({llm_string for _, llm_string in keys.values()}) == 3
    # the document reader's key is the same for the same text, and not for another
    assert key(DocExtraction, DOC_SYSTEM, pump_answer()) == keys["document"]


# --- The extraction -------------------------------------------------------------


def test_the_extraction_is_a_procedure_with_one_chunk_of_the_whole_text(
    monkeypatch, tmp_path
):
    path = saved(tmp_path, "07_pump_procedure.docx")
    result = extract(monkeypatch, path, pump_answer())
    doc = document_id("procedures/07_pump_procedure.docx.txt")
    assert (result.doc_type, result.extractor) == ("procedure", EXTRACTOR)
    assert EXTRACTOR == "sol-doc"
    assert result.doc_path == "procedures/07_pump_procedure.docx.txt"
    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.id == chunk_id(doc, None, 0)
    assert (chunk.page, chunk.ord, chunk.doc_path) == (None, 0, result.doc_path)
    assert chunk.text == path.read_text(encoding="utf-8")
    assert router.classify(path) == result.doc_type


def test_the_procedure_node_carries_the_title_steps_lockout_and_minutes(
    monkeypatch, tmp_path
):
    result = extract(
        monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), pump_answer()
    )
    (procedure,) = nodes_of(result, "Procedure")
    assert procedure.canonical is False  # its id is minted when the graph is known
    assert procedure.ref.key == "procedures/07_pump_procedure.docx.txt#procedure"
    assert procedure.label == "Procedure: change the hydraulic oil and filter on PR-2"
    assert procedure.props == {
        "id_prefix": "HP",
        "title": "Procedure: change the hydraulic oil and filter on PR-2",
        "steps": pump_answer().steps,
        "requires_loto": True,
        "est_minutes": 90,
        "manual_ref": None,
    }
    assert [e.quote for e in procedure.evidence] == [
        "Procedure: change the hydraulic oil and filter on PR-2"
    ]
    assert procedure.evidence[0].extractor == "sol-doc"


def test_the_machine_is_found_through_the_alias_table_and_gets_the_evidence(
    monkeypatch, tmp_path
):
    result = extract(
        monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), pump_answer()
    )
    (machine,) = nodes_of(result, "Asset")
    assert machine.ref.key == "HP-01"  # PR-2 is an alias of HP-01
    assert machine.canonical is False and machine.props == {}
    assert [e.quote for e in machine.evidence] == [
        "Procedure: change the hydraulic oil and filter on PR-2"
    ]


def test_parts_become_requires_edges_with_their_quantities(monkeypatch, tmp_path):
    result = extract(
        monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), pump_answer()
    )
    requires = [e for e in result.edges if e.rel == "REQUIRES"]
    assert [(e.dst.type, e.dst.key, e.props) for e in requires] == [
        ("Part", "HLP46", {"qty": 3}),
        ("Part", "FLT-HYD-10", {"qty": 1}),
    ]
    assert {e.src.key for e in requires} == {
        "procedures/07_pump_procedure.docx.txt#procedure"
    }
    quotes = {e.dst.key: e.evidence[0].quote for e in requires}
    assert quotes == {"HLP46": "HLP46 | 3", "FLT-HYD-10": "FLT HYD 10 | 1"}
    parts = {n.ref.key: n for n in nodes_of(result, "Part")}
    assert set(parts) == {"HLP46", "FLT-HYD-10"}
    assert all(not n.canonical and n.props == {} for n in parts.values())
    assert parts["FLT-HYD-10"].evidence[0].quote == "FLT HYD 10 | 1"


def test_alarm_codes_become_nodes_with_the_line_that_names_them(monkeypatch, tmp_path):
    result = extract(
        monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), pump_answer()
    )
    alarms = {n.ref.key: n for n in nodes_of(result, "AlarmCode")}
    assert set(alarms) == {"E-221", "E-230"}
    assert alarms["E-221"].canonical is False and alarms["E-221"].props == {}
    quote = alarms["E-221"].evidence[0].quote
    assert "E221" in quote and quote.startswith("Do this when")
    assert alarms["E-230"].evidence[0].quote == quote  # one line names both


def test_a_work_instruction_with_no_time_gets_zero_minutes_and_a_note(
    monkeypatch, tmp_path
):
    result = extract(
        monkeypatch,
        saved(tmp_path, "08_work_instruction_gripper.html"),
        gripper_answer(),
    )
    (procedure,) = nodes_of(result, "Procedure")
    assert procedure.props["est_minutes"] == 0
    assert procedure.props["requires_loto"] is False
    assert procedure.props["id_prefix"] == "RW"
    assert any("no time" in note for note in result.notes)
    (machine,) = nodes_of(result, "Asset")
    assert machine.ref.key == "RW-01"
    assert machine.evidence[0].quote.startswith("Work instruction WI-27: replace")
    assert {n.ref.key for n in nodes_of(result, "AlarmCode")} == {"RB-551"}
    assert {n.ref.key for n in nodes_of(result, "Part")} == {"GRPFNG12"}
    assert [e.props for e in result.edges if e.rel == "REQUIRES"] == [{"qty": 2}]


def test_lockout_is_read_in_the_text_as_well_as_from_the_model(monkeypatch, tmp_path):
    path = saved(tmp_path, "07_pump_procedure.docx")
    said_no = extract(monkeypatch, path, pump_answer(requires_lockout=False))
    assert nodes_of(said_no, "Procedure")[0].props["requires_loto"] is True
    assert any("lockout" in note for note in said_no.notes)
    plain = saved(tmp_path, "08_work_instruction_gripper.html")
    said_yes = extract(monkeypatch, plain, gripper_answer(requires_lockout=True))
    assert nodes_of(said_yes, "Procedure")[0].props["requires_loto"] is True


def test_failure_modes_become_nodes_resolved_by_the_procedure(monkeypatch, tmp_path):
    answer = pump_answer(
        failure_modes=[
            FailureModeMention(
                phrase="hydraulic oil temperature high", short_name="oil overheating"
            )
        ]
    )
    result = extract(monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), answer)
    (mode,) = nodes_of(result, "FailureMode")
    assert mode.ref.key == "FM-OIL-OVERHEATING"
    assert mode.canonical is True
    assert mode.props == {
        "id": "FM-OIL-OVERHEATING",
        "name": "oil overheating",
        "mechanism": "hydraulic oil temperature high",
    }
    assert "hydraulic oil temperature high" in mode.evidence[0].quote
    (resolved,) = [e for e in result.edges if e.rel == "RESOLVED_BY"]
    assert (resolved.src.key, resolved.dst.key) == (
        "FM-OIL-OVERHEATING",
        "procedures/07_pump_procedure.docx.txt#procedure",
    )


def test_every_edge_is_allowed_by_the_ontology(monkeypatch, tmp_path):
    answer = pump_answer(
        failure_modes=[
            FailureModeMention(
                phrase="hydraulic oil temperature high", short_name="oil overheating"
            )
        ]
    )
    result = extract(monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), answer)
    types = {n.ref.key: n.ref.type for n in result.nodes}
    assert result.edges
    for edge in result.edges:
        assert is_allowed(types[edge.src.key], edge.rel, types[edge.dst.key]), edge
    assert count_by_type(result) == {
        "Procedure": 1,
        "Asset": 1,
        "Part": 2,
        "AlarmCode": 2,
        "FailureMode": 1,
    }
    assert count_by_relation(result) == {"REQUIRES": 2, "RESOLVED_BY": 1}


@pytest.mark.parametrize(
    ("name", "answer"),
    [
        ("07_pump_procedure.docx", pump_answer()),
        ("08_work_instruction_gripper.html", gripper_answer()),
    ],
)
def test_every_quote_is_a_real_line_of_the_document(
    monkeypatch, tmp_path, name, answer
):
    path = saved(tmp_path, name)
    result = extract(monkeypatch, path, answer)
    lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    seen = 0
    for item in [*result.nodes, *result.edges]:
        assert item.evidence
        for e in item.evidence:
            assert e.quote in lines and 0 < len(e.quote) <= 300
            assert (e.doc_path, e.extractor, e.confidence) == (
                result.doc_path,
                "sol-doc",
                0.8,
            )
            seen += 1
    assert seen > 0


def test_a_mention_that_is_not_in_the_text_is_left_out_with_a_note(
    monkeypatch, tmp_path
):
    answer = pump_answer(
        parts=[PartNeed(mention="HLP46", qty=3), PartNeed(mention="UNICORN 9", qty=1)],
        alarm_mentions=["E221", "E999"],
    )
    result = extract(monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), answer)
    assert {n.ref.key for n in nodes_of(result, "Part")} == {"HLP46"}
    assert {n.ref.key for n in nodes_of(result, "AlarmCode")} == {"E-221"}
    assert any("UNICORN 9" in note for note in result.notes)
    assert any("E999" in note for note in result.notes)


def test_a_part_with_no_quantity_counts_as_one_and_is_noted(monkeypatch, tmp_path):
    answer = pump_answer(parts=[PartNeed(mention="FLT HYD 10", qty=None)])
    result = extract(monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), answer)
    assert [e.props for e in result.edges if e.rel == "REQUIRES"] == [{"qty": 1}]
    assert any("FLT HYD 10" in note and "1 is assumed" in note for note in result.notes)


def test_a_machine_the_table_does_not_know_gets_no_asset_and_a_placeholder_prefix(
    monkeypatch, tmp_path
):
    result = extract(
        monkeypatch,
        saved(tmp_path, "07_pump_procedure.docx"),
        pump_answer(machine_mentions=["mystery rig"]),
    )
    assert nodes_of(result, "Asset") == []
    assert nodes_of(result, "Procedure")[0].props["id_prefix"] == "DOC"
    assert any("mystery rig" in note for note in result.notes)


def test_a_title_that_is_not_in_the_text_falls_back_to_the_first_line(
    monkeypatch, tmp_path
):
    result = extract(
        monkeypatch,
        saved(tmp_path, "07_pump_procedure.docx"),
        pump_answer(title="Changing the oil"),
    )
    (procedure,) = nodes_of(result, "Procedure")
    assert procedure.props["title"] == "Changing the oil"
    assert procedure.evidence[0].quote == (
        "Procedure: change the hydraulic oil and filter on PR-2"
    )


def test_a_document_that_describes_nothing_has_a_chunk_and_no_procedure(
    monkeypatch, tmp_path
):
    answer = DocExtraction(
        title=None,
        machine_mentions=[],
        steps=[],
        requires_lockout=False,
        minutes=None,
        parts=[],
        alarm_mentions=[],
        failure_modes=[],
    )
    result = extract(monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), answer)
    assert (result.nodes, result.edges) == ([], [])
    assert len(result.chunks) == 1
    assert any("no steps" in note for note in result.notes)


def test_an_extraction_reads_back_from_json_equal(monkeypatch, tmp_path):
    result = extract(
        monkeypatch, saved(tmp_path, "07_pump_procedure.docx"), pump_answer()
    )
    assert Extraction.model_validate_json(result.model_dump_json()) == result


# --- Routing --------------------------------------------------------------------


def test_the_two_test_files_route_to_procedure_through_the_upload(tmp_path):
    from app.api import uploads

    for name in ("07_pump_procedure.docx", "08_work_instruction_gripper.html"):
        upload = uploads.save_upload(name, (TESTSET / name).read_bytes(), tmp_path)
        assert upload.path.parent.name == "procedures"
        assert router.classify(upload.path) == "procedure"
        assert router.extractor_for(upload.path) is extract_document


@pytest.mark.parametrize(
    ("line", "markdown", "named"),
    [
        ("Procedure: change the oil", False, True),
        ("Work instruction WI-27", False, True),
        ("Robot cell restart checklist", False, True),
        ("SOP 14", False, True),
        ("Instructions for the clamp", False, True),
        ("# Procedure: change the oil", True, True),
        ("# Procedure: change the oil", False, False),
        ("Shift handover 20 Oct", False, False),
        ("Leak tester note", False, False),
        ("", False, False),
        ("proceduralist", False, False),
    ],
)
def test_names_procedure(line, markdown, named):
    assert names_procedure(line, markdown) is named


def test_the_title_of_a_document_never_matches_a_numbered_step():
    assert re.search(r"\d", "1. Stop the cell")
    assert names_procedure("1. Stop the cell", False) is False
