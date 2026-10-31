"""Tests for the PDF manual reader.

No test calls a model. Each test supplies a canned model answer written from the real
manuals. What is tested is the text chunker (chunk sizes and page numbers) and the work
done after the answer: ids, edges, lockout flags and quotes taken from the page text."""

import re
import threading

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
from pypdf import PdfReader

from app import llm
from app.config import DATA_DIR, RAW_DIR, Settings
from app.ingest import router
from app.ingest.extract_manual import extract_manual, lockout_in
from app.ingest.manual_chunks import MAX_CHUNK, MIN_CHUNK, chunk_text
from app.ingest.manual_schema import (
    MANUAL_SYSTEM,
    AlarmItem,
    CauseItem,
    ComponentItem,
    FailureModeItem,
    ManualExtraction,
    PartRequirement,
    ProcedureItem,
    SparePartItem,
)
from app.ingest.normalize import chunk_id, document_id
from app.ingest.types import Extraction
from app.ontology import props_model, validate_id

MANUALS = RAW_DIR / "manuals"
BH = MANUALS / "BH-250 service manual.pdf"
KX = MANUALS / "Kessler_KX-500_Operator_Maintenance_Manual_rev3.pdf"
ALL_MANUALS = sorted(MANUALS.glob("*.pdf"))


def page_texts(path) -> dict[int, str]:
    """The text of each page, read straight with pypdf."""
    reader = PdfReader(path)
    return {n: page.extract_text() for n, page in enumerate(reader.pages, start=1)}


# --- Section chunking -----------------------------------------------------------


@pytest.mark.parametrize("path", ALL_MANUALS, ids=lambda p: p.name)
def test_every_real_manual_chunks_within_the_size_bounds_with_pages(path):
    pages = page_texts(path)
    chunks = chunk_text(pages)
    assert chunks
    for page, text in chunks:
        assert MIN_CHUNK <= len(text) <= MAX_CHUNK, (page, len(text), text[:60])
        assert page in pages


@pytest.mark.parametrize("path", ALL_MANUALS, ids=lambda p: p.name)
def test_chunks_keep_reading_order_and_lose_no_content(path):
    pages = page_texts(path)
    chunks = chunk_text(pages)
    assert [page for page, _ in chunks] == sorted(page for page, _ in chunks)
    joined = " ".join(text for _, text in chunks)
    squash = lambda t: re.sub(r"\s+", " ", t)
    for page, text in pages.items():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if re.fullmatch(r"Page \d+", lines[-1]):
            lines = lines[:-2]  # the running footer and the page number
        body = lines
        # every non-footer line is somewhere in the chunks
        for line in body:
            assert squash(line) in squash(joined), (page, line)


def test_the_running_footer_and_page_number_are_not_in_the_chunks():
    for _, text in chunk_text(page_texts(BH)):
        assert "Service Manual, Revision 3" not in text
        assert not re.search(r"^Page \d+$", text, re.MULTILINE)


def test_chunks_start_at_numbered_headings_and_section_titles():
    starts = [text.splitlines()[0] for _, text in chunk_text(page_texts(BH))]
    assert "4.2 Change hydraulic oil and filter" in starts
    assert "4.3 Reset and test relief valve" in starts
    assert "3 Troubleshooting table" in starts
    assert "5 Spare parts list" in starts


def test_a_chunk_records_the_page_of_its_first_line():
    chunks = chunk_text(page_texts(BH))
    page_of = {text.splitlines()[0]: page for page, text in chunks}
    assert page_of["3 Troubleshooting table"] == 4
    assert page_of["4.2 Change hydraulic oil and filter"] == 5
    assert page_of["4.3 Reset and test relief valve"] == 6
    assert page_of["5 Spare parts list"] == 7


def test_a_long_section_is_split_at_line_boundaries_into_bounded_pieces():
    lines = [
        f"line {n:03d} " + "x" * 80 for n in range(60)
    ]  # 60 lines of 91 characters
    pages = {1: "2 Alarm table\n" + "\n".join(lines)}
    chunks = chunk_text(pages)
    assert len(chunks) >= 5
    assert all(MIN_CHUNK <= len(text) <= MAX_CHUNK for _, text in chunks)
    assert "\n".join(text for _, text in chunks).splitlines() == [
        "2 Alarm table",
        *lines,
    ]


def test_a_small_section_merges_into_the_next_one_on_its_page():
    body = "a sentence of about forty characters.\n" * 12  # 450 characters
    pages = {1: f"1 Safety\n1.1 General safety\n{body}1.2 Lockout\n{body}"}
    chunks = chunk_text(pages)
    assert len(chunks) == 2  # the bare "1 Safety" title is too small to stand alone
    assert chunks[0][1].startswith("1 Safety\n1.1 General safety")
    assert chunks[1][1].startswith("1.2 Lockout")


def test_a_title_page_too_small_to_stand_alone_joins_the_next_page_and_its_page():
    body = "a sentence of about forty characters.\n" * 12
    pages = {1: "Maker\nX-1\nManual", 2: f"1 Safety\n{body}"}
    ((page, text),) = chunk_text(pages)
    assert page == 2  # most of the joined text is on page 2
    assert text.startswith("Maker")
    assert "1 Safety" in text


def test_a_joined_chunk_takes_the_page_that_holds_most_of_its_text():
    body = "a sentence of about forty characters.\n" * 12  # 450 characters
    small = "Maker\nX-1\nManual\n"
    assert chunk_text({1: f"{small}", 2: f"1 Safety\n{body}"})[0][0] == 2
    # the larger part is on page 1: the small start of page 2 joins it
    assert chunk_text({1: f"1 Safety\n{body}", 2: "1.1 Tail\nx\n"})[0][0] == 1


def chars_by_page(pages: dict[int, str]) -> dict[int, set[str]]:
    """The body lines of each page, as the chunker sees them."""
    result = {}
    for number, text in pages.items():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if re.fullmatch(r"Page \d+", lines[-1]):
            lines = lines[:-2]
        result[number] = set(lines)
    return result


@pytest.mark.parametrize("path", ALL_MANUALS, ids=lambda p: p.name)
def test_no_chunk_page_holds_less_than_half_of_the_chunk_text(path):
    pages = page_texts(path)
    on_page = chars_by_page(pages)
    for page, text in chunk_text(pages):
        lines = text.splitlines()
        here = sum(len(line) for line in lines if line in on_page[page])
        assert here * 2 >= sum(len(line) for line in lines), (page, text[:60])


def test_a_step_line_is_not_taken_for_a_heading():
    body = "a sentence of about forty characters.\n" * 12
    pages = {1: f"4.1 Replace it\n{body}1. Stop the machine.\n2. Start it.\n{body}"}
    chunks = chunk_text(pages)
    assert len(chunks) == 1


# --- The model call -------------------------------------------------------------


def canned_bh() -> ManualExtraction:
    """What a model would answer for the BH-250 manual."""
    pump, cyl, valve = (
        "Hydraulic pump",
        "Main cylinder and seals",
        "Pressure relief valve",
    )
    seals, oil, relief = (
        "worn main cylinder seals",
        "contaminated hydraulic oil",
        "relief valve out of adjustment",
    )

    def alarm(code, message, severity, causes, ref):
        return AlarmItem(
            code=code,
            message=message,
            severity=severity,
            causes=[CauseItem(phrase=p, rank=r) for p, r in causes],
            procedure_ref=ref,
        )

    def need(part_no, description, qty):
        return PartRequirement(part_no=part_no, description=description, qty=qty)

    def spare(part_no, description, *fits):
        return SparePartItem(part_no=part_no, description=description, fits=list(fits))

    return ManualExtraction(
        vendor="Bramwell Hydraulics",
        model="BH-250",
        components=[
            ComponentItem(name=pump, code="PUMP"),
            ComponentItem(name=cyl, code="CYL"),
            ComponentItem(name=valve, code="VALVE"),
        ],
        failure_modes=[
            FailureModeItem(
                phrase=seals,
                short_name="Cylinder seal wear",
                component_name=cyl,
                procedure_refs=["PROC-HP-04"],
            ),
            FailureModeItem(
                phrase=oil,
                short_name="Contaminated oil",
                component_name=pump,
                procedure_refs=["PROC-HP-05"],
            ),
            FailureModeItem(
                phrase=relief,
                short_name="Relief valve drift",
                component_name=valve,
                procedure_refs=["PROC-HP-06"],
            ),
        ],
        alarms=[
            alarm("E-200", "Pump start", "info", [], None),
            alarm(
                "E-217",
                "Main cylinder pressure loss",
                "critical",
                [(seals, "most likely"), (relief, "possible")],
                "PROC-HP-04",
            ),
            alarm(
                "E-221",
                "Hydraulic oil temperature high",
                "warning",
                [(oil, "most likely"), (relief, "possible")],
                "PROC-HP-05",
            ),
            alarm(
                "E-230",
                "Pump pressure unstable",
                "warning",
                [(oil, "most likely")],
                "PROC-HP-05",
            ),
            alarm(
                "E-245",
                "Oil level low",
                "warning",
                [(seals, "most likely")],
                "PROC-HP-04",
            ),
            alarm(
                "E-252",
                "Relief valve pressure out of range",
                "critical",
                [(relief, "most likely")],
                "PROC-HP-06",
            ),
        ],
        procedures=[
            ProcedureItem(
                plant_ref="PROC-HP-04",
                title="Replace main cylinder seal kit",
                requires_loto=True,
                minutes=180,
                steps=[
                    "Lower the ram onto blocks, stop the pump and apply lockout and tagout on the main isolator."
                ],
                parts=[
                    need("SEAL-KIT-50", "SK-50 seal kit", 1),
                    need("OIL-HLP46-20L", "Hydraulic oil HLP 46, 20 litre pail", 1),
                ],
            ),
            ProcedureItem(
                plant_ref="PROC-HP-05",
                title="Change hydraulic oil and filter",
                requires_loto=True,
                minutes=120,
                steps=[
                    "Stop the pump, apply lockout and tagout, and bleed the accumulator to 0 bar."
                ],
                parts=[
                    need("OIL-HLP46-20L", "Hydraulic oil HLP 46, 20 litre pail", 3),
                    need("FLT-HYD-10", "Hydraulic return filter 10 micron", 1),
                ],
            ),
            ProcedureItem(
                plant_ref="PROC-HP-06",
                title="Reset and test relief valve",
                requires_loto=True,
                minutes=60,
                steps=[
                    "Stop the pump, apply lockout and tagout, and bleed the accumulator to 0 bar."
                ],
                parts=[need("SEAL-RV-25", "Relief valve seal kit", 1)],
            ),
        ],
        spare_parts=[
            spare("COUPLING-PMP-28", "Pump coupling 28 mm", pump),
            spare("FLT-HYD-10", "Hydraulic return filter 10 micron", pump),
            spare("GAUGE-PRS-250", "Pressure gauge 0 to 250 bar", valve),
            spare("HOSE-HYD-12", "Hydraulic hose 12 mm, 1 m", pump),
            spare("NUT-GLAND-50", "Cylinder gland nut 50 mm", cyl),
            spare("O-RING-NBR-12", "O-ring NBR 12 mm, pack of 10", valve),
            spare("OIL-HLP46-20L", "Hydraulic oil HLP 46, 20 litre pail", pump, cyl),
            spare("SEAL-KIT-50", "Main cylinder seal kit, 50 mm rod", cyl),
            spare("SEAL-RV-25", "Relief valve seal kit", valve),
            spare("SENS-PT100-01", "Temperature sensor PT100", pump),
            spare("SWITCH-LIM-01", "Limit switch", cyl),
            spare("WIPER-CYL-50", "Cylinder rod wiper 50 mm", cyl),
        ],
    )


def canned_kx() -> ManualExtraction:
    """A short answer for the KX-500 manual, which two machines share."""
    spindle, coolant = "Spindle", "Coolant system"
    return ManualExtraction(
        vendor="Kessler Werkzeugmaschinen",
        model="KX-500",
        components=[
            ComponentItem(name=spindle, code="SPINDLE"),
            ComponentItem(name=coolant, code="COOLANT"),
        ],
        failure_modes=[
            FailureModeItem(
                phrase="worn spindle bearings",
                short_name="spindle bearing wear",
                component_name=spindle,
                procedure_refs=["PROC-CNC-01", "PROC-CNC-02"],
            ),
            FailureModeItem(
                phrase="blocked coolant filter",
                short_name="blocked coolant filter",
                component_name=coolant,
                procedure_refs=["PROC-CNC-03"],
            ),
        ],
        alarms=[
            AlarmItem(
                code="SP104",
                message="Spindle temperature high",
                severity="critical",
                causes=[
                    CauseItem(phrase="worn spindle bearings", rank="most likely"),
                    CauseItem(phrase="blocked coolant filter", rank="possible"),
                ],
                procedure_ref="PROC-CNC-01",
            )
        ],
        procedures=[
            ProcedureItem(
                plant_ref="PROC-CNC-01",
                title="Replace spindle bearing set",
                requires_loto=True,
                minutes=240,
                steps=["Stop the machine."],
                parts=[
                    PartRequirement(
                        part_no="BRG-7014-ACD",
                        description="Spindle angular contact bearing 7014 ACD",
                        qty=2,
                    )
                ],
            ),
            ProcedureItem(
                plant_ref="PROC-CNC-02",
                title="Check spindle vibration from the HMI",
                requires_loto=False,
                minutes=20,
                steps=["Open Diagnostics."],
                parts=[],
            ),
            ProcedureItem(
                plant_ref="PROC-CNC-03",
                title="Clean coolant filter and flush lines",
                requires_loto=True,
                minutes=45,
                steps=["Stop the coolant pump."],
                parts=[
                    PartRequirement(
                        part_no="FLT-CL-25",
                        description="Coolant filter element 25 micron",
                        qty=1,
                    )
                ],
            ),
        ],
        spare_parts=[
            SparePartItem(
                part_no="BRG-7014-ACD",
                description="Spindle angular contact bearing 7014 ACD",
                fits=[spindle],
            ),
            SparePartItem(
                part_no="FLT-CL-25",
                description="Coolant filter element 25 micron",
                fits=[coolant],
            ),
        ],
    )


@pytest.fixture
def bh(monkeypatch) -> Extraction:
    monkeypatch.setattr(llm, "structured", FakeModel(canned_bh()))
    return extract_manual(BH, ALIASES)


@pytest.fixture
def kx(monkeypatch) -> Extraction:
    monkeypatch.setattr(llm, "structured", FakeModel(canned_kx()))
    return extract_manual(KX, ALIASES)


def test_the_settings_of_the_caller_go_to_the_model_call(monkeypatch):
    fake = FakeModel(canned_bh())
    monkeypatch.setattr(llm, "structured", fake)
    mine = Settings(budget_usd=3.0)
    extract_manual(BH, ALIASES, settings=mine)
    assert fake.calls[0]["settings"] is mine


def node(extraction: Extraction, node_type: str, key: str):
    found = [n for n in nodes_of(extraction, node_type) if n.ref.key == key]
    assert len(found) == 1, (node_type, key, len(found))
    return found[0]


def edges(extraction: Extraction, rel: str):
    return [(e.src.key, e.dst.key) for e in extraction.edges if e.rel == rel]


def test_one_sol_call_over_the_full_text_with_page_markers(fake_model):
    fake_model.answer = canned_bh()
    extract_manual(BH, ALIASES)
    assert len(fake_model.calls) == 1
    call = fake_model.calls[0]
    assert call["role"] == "sol"
    assert call["schema"] is ManualExtraction
    assert call["system"] == MANUAL_SYSTEM
    assert call["purpose"] == "extract manual"
    user = call["user"]
    for page in range(1, 8):
        assert f"[page {page}]" in user
    assert "E-217\nMain cylinder pressure loss" in user
    assert "Plant ref: PROC-HP-06 | Lockout required: yes | Time: 60 min" in user
    assert "Bramwell Hydraulics BH-250 Service Manual, Revision 3" not in user


def test_the_schema_has_no_quote_fields_because_quotes_are_found_in_the_text():
    def names(schema):
        found = set(schema.model_fields)
        for field in schema.model_fields.values():
            args = getattr(field.annotation, "__args__", ())
            found |= {n for a in args if hasattr(a, "model_fields") for n in names(a)}
        return found

    assert not {n for n in names(ManualExtraction) if "quote" in n or "evidence" in n}


def test_the_prompt_asks_for_exact_phrases_and_no_quotes():
    text = MANUAL_SYSTEM.lower()
    assert "exactly as" in text
    assert "most likely" in text and "possible" in text


# --- The document and the assets ------------------------------------------------


def test_the_extraction_is_a_manual_read_by_sol_manual(bh):
    assert bh.doc_type == "manual"
    assert bh.extractor == "sol-manual"
    assert bh.doc_path == "manuals/BH-250 service manual.pdf"
    assert bh.notes == []


def test_the_assets_come_from_the_alias_table_by_model(bh, kx):
    assert [n.ref.key for n in nodes_of(bh, "Asset")] == ["HP-01"]
    assert [n.ref.key for n in nodes_of(kx, "Asset")] == ["CNC-01", "CNC-02"]
    hp = node(bh, "Asset", "HP-01")
    assert hp.canonical
    assert hp.props["vendor"] == "Bramwell Hydraulics"
    assert hp.props["model"] == "BH-250"
    assert hp.evidence[0].quote == "BH-250"
    assert hp.evidence[0].page == 1


def with_loto(canned: ManualExtraction, flags: dict[str, bool]) -> ManualExtraction:
    """The canned answer with the model's requires_loto set for the named procedures."""
    procedures = [
        p.model_copy(update={"requires_loto": flags.get(p.plant_ref, p.requires_loto)})
        for p in canned.procedures
    ]
    return canned.model_copy(update={"procedures": procedures})


def test_a_model_that_misses_a_lockout_the_procedure_text_requires_is_overruled(
    monkeypatch,
):
    # the procedure line reads "Lockout required: yes"; the model said no
    canned = with_loto(canned_bh(), {"PROC-HP-04": False})
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert node(result, "Procedure", "PROC-HP-04").props["requires_loto"] is True
    (note,) = [n for n in result.notes if "PROC-HP-04" in n]
    assert "lockout required: yes" in note and "model said no" in note


def test_a_model_that_adds_a_lockout_the_text_does_not_require_still_gets_one(
    monkeypatch,
):
    # the procedure line reads "Lockout required: no"; the model said yes, and a
    # disagreement always takes the safe side
    canned = with_loto(canned_kx(), {"PROC-CNC-02": True})
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(KX, ALIASES)
    assert node(result, "Procedure", "PROC-CNC-02").props["requires_loto"] is True
    (note,) = [n for n in result.notes if "PROC-CNC-02" in n]
    assert "lockout required: no" in note and "model said yes" in note


def test_the_lockout_flag_is_read_from_the_procedure_line():
    line = "Plant ref: PROC-HP-04 | Lockout required: yes | Time: 180 min"
    assert lockout_in(line) is True
    assert (
        lockout_in("Plant ref: PROC-CNC-02 | Lockout required: No | Time: 20 min")
        is False
    )
    assert lockout_in("Plant ref: PROC-X | Time: 20 min") is None
    assert lockout_in("Lockout required: maybe") is None


def test_a_model_that_agrees_with_the_procedure_text_adds_no_note(monkeypatch):
    monkeypatch.setattr(llm, "structured", FakeModel(canned_kx()))
    result = extract_manual(KX, ALIASES)
    assert node(result, "Procedure", "PROC-CNC-02").props["requires_loto"] is False
    assert node(result, "Procedure", "PROC-CNC-01").props["requires_loto"] is True
    assert not any("lockout" in n.lower() for n in result.notes)


def test_an_unknown_model_gives_no_assets_and_a_note(monkeypatch):
    canned = canned_bh().model_copy(update={"model": "ZZ-999"})
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert nodes_of(result, "Asset") == []
    assert nodes_of(result, "Component") == []
    assert any("ZZ-999" in note for note in result.notes)
    assert len(nodes_of(result, "AlarmCode")) == 6  # the rest is still extracted


# --- Ids ------------------------------------------------------------------------


def test_component_ids_are_asset_dash_code_for_every_asset_of_the_model(bh, kx):
    assert sorted(n.ref.key for n in nodes_of(bh, "Component")) == [
        "HP-01-CYL",
        "HP-01-PUMP",
        "HP-01-VALVE",
    ]
    assert sorted(n.ref.key for n in nodes_of(kx, "Component")) == [
        "CNC-01-COOLANT",
        "CNC-01-SPINDLE",
        "CNC-02-COOLANT",
        "CNC-02-SPINDLE",
    ]
    cyl = node(bh, "Component", "HP-01-CYL")
    assert cyl.canonical
    assert cyl.label == "Main cylinder and seals"
    assert cyl.props["asset_id"] == "HP-01"
    assert all(validate_id("Component", n.ref.key) for n in nodes_of(kx, "Component"))


def test_failure_mode_ids_are_fm_plus_the_upper_case_slug_of_the_short_name(bh, kx):
    assert sorted(n.ref.key for n in nodes_of(bh, "FailureMode")) == [
        "FM-CONTAMINATED-OIL",
        "FM-CYLINDER-SEAL-WEAR",
        "FM-RELIEF-VALVE-DRIFT",
    ]
    assert "FM-SPINDLE-BEARING-WEAR" in {n.ref.key for n in nodes_of(kx, "FailureMode")}
    seal = node(bh, "FailureMode", "FM-CYLINDER-SEAL-WEAR")
    assert seal.canonical
    assert seal.label == "Cylinder seal wear"
    assert seal.props["mechanism"] == "worn main cylinder seals"


def test_the_slug_drops_punctuation_and_collapses_runs_to_one_dash(monkeypatch):
    canned = canned_bh()
    canned.failure_modes[0].short_name = "  Seal / wear (main) - cylinder! "
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert "FM-SEAL-WEAR-MAIN-CYLINDER" in {
        n.ref.key for n in nodes_of(result, "FailureMode")
    }


def extract_with_timeout(monkeypatch, canned, seconds=5.0) -> Extraction:
    """Run the manual reader on the BH manual with a canned answer. It runs in a
    background thread, so a loop that never ends fails the test instead of hanging."""
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    box: dict[str, Extraction] = {}
    worker = threading.Thread(
        target=lambda: box.update(result=extract_manual(BH, ALIASES)), daemon=True
    )
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "extract_manual did not finish"
    return box["result"]


@pytest.mark.parametrize(
    "codes",
    [
        ["SPINDLE", "SPINDLE2", "SPINDLE"],
        ["CYL2", "CYL", "CYL"],
        ["PUMP", "PUMP", "PUMP"],
        ["", "", ""],
        ["!!!", "CYL", "CYL"],
    ],
    ids=lambda codes: "-".join(codes) or "empty",
)
def test_colliding_component_codes_get_the_first_free_suffix(monkeypatch, codes):
    canned = canned_bh()
    for item, code in zip(canned.components, codes, strict=True):
        item.code = code
    result = extract_with_timeout(monkeypatch, canned)
    ids = [n.ref.key for n in nodes_of(result, "Component")]
    assert len(ids) == 3 and len(set(ids)) == 3
    for key in ids:
        code = key.split("-", 2)[-1]
        assert 0 < len(code) <= 8, key


def test_a_taken_suffix_is_skipped_for_the_next_free_one(monkeypatch):
    canned = canned_bh()
    codes = ["SPINDLE", "SPINDLE1", "SPINDLE"]
    for item, code in zip(canned.components, codes, strict=True):
        item.code = code
    result = extract_with_timeout(monkeypatch, canned)
    codes = [n.ref.key.split("-", 2)[-1] for n in nodes_of(result, "Component")]
    assert codes == ["SPINDLE", "SPINDLE1", "SPINDLE2"]


def test_procedures_use_their_plant_reference_as_the_canonical_id(bh):
    assert sorted(n.ref.key for n in nodes_of(bh, "Procedure")) == [
        "PROC-HP-04",
        "PROC-HP-05",
        "PROC-HP-06",
    ]
    proc = node(bh, "Procedure", "PROC-HP-04")
    assert proc.canonical
    assert proc.label == "Replace main cylinder seal kit"
    assert proc.props["requires_loto"] is True
    assert proc.props["est_minutes"] == 180
    assert proc.props["manual_ref"] == "4.1"
    assert proc.props["steps"][0].startswith("Lower the ram")


def test_alarm_codes_are_canonical_after_normalization(bh, kx):
    assert sorted(n.ref.key for n in nodes_of(bh, "AlarmCode")) == [
        "E-200",
        "E-217",
        "E-221",
        "E-230",
        "E-245",
        "E-252",
    ]
    assert node(kx, "AlarmCode", "SP-104").canonical  # "SP104" was normalized
    alarm = node(bh, "AlarmCode", "E-217")
    assert alarm.props == {
        "code": "E-217",
        "text": "Main cylinder pressure loss",
        "severity": "critical",
        "aliases": [],
    }


def test_an_alarm_code_that_does_not_normalize_stays_raw_and_not_canonical(monkeypatch):
    canned = canned_bh()
    canned.alarms[0].code = "pump start"
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    raw = node(result, "AlarmCode", "pump start")
    assert not raw.canonical


def test_parts_are_canonical_when_dashed_and_merged_across_lists(bh):
    parts = nodes_of(bh, "Part")
    assert len(parts) == 12
    assert all(n.canonical for n in parts)
    seal = node(bh, "Part", "SEAL-KIT-50")
    assert seal.props["description"] == "Main cylinder seal kit, 50 mm rod"
    assert len(seal.evidence) >= 2  # the procedure table and the spare parts list


def test_a_part_without_dashes_stays_raw(monkeypatch):
    canned = canned_bh()
    canned.spare_parts[0].part_no = "COUPLING"
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert not node(result, "Part", "COUPLING").canonical


# --- Edges ----------------------------------------------------------------------


def test_edge_counts_for_the_bh_250(bh):
    assert count_by_type(bh) == {
        "Asset": 1,
        "Component": 3,
        "FailureMode": 3,
        "AlarmCode": 6,
        "Procedure": 3,
        "Part": 12,
    }
    assert count_by_relation(bh) == {
        "HAS_COMPONENT": 3,
        "AFFECTS": 3,
        "INDICATES": 7,
        "RESOLVED_BY": 3,
        "REQUIRES": 5,
        "FITS": 13,
    }


def test_has_component_runs_from_each_asset_to_each_of_its_components(bh, kx):
    assert sorted(edges(bh, "HAS_COMPONENT")) == [
        ("HP-01", "HP-01-CYL"),
        ("HP-01", "HP-01-PUMP"),
        ("HP-01", "HP-01-VALVE"),
    ]
    assert len(edges(kx, "HAS_COMPONENT")) == 4


def test_affects_runs_from_a_failure_mode_to_its_component_on_every_asset(bh, kx):
    assert ("FM-CYLINDER-SEAL-WEAR", "HP-01-CYL") in edges(bh, "AFFECTS")
    assert sorted(edges(kx, "AFFECTS")) == [
        ("FM-BLOCKED-COOLANT-FILTER", "CNC-01-COOLANT"),
        ("FM-BLOCKED-COOLANT-FILTER", "CNC-02-COOLANT"),
        ("FM-SPINDLE-BEARING-WEAR", "CNC-01-SPINDLE"),
        ("FM-SPINDLE-BEARING-WEAR", "CNC-02-SPINDLE"),
    ]


def test_indicates_carries_the_manual_prior_and_no_support(bh):
    found = {(e.src.key, e.dst.key): e.props for e in bh.edges if e.rel == "INDICATES"}
    assert found[("E-217", "FM-CYLINDER-SEAL-WEAR")] == {
        "likelihood": 0.7,
        "support": 0,
    }
    assert found[("E-217", "FM-RELIEF-VALVE-DRIFT")] == {
        "likelihood": 0.3,
        "support": 0,
    }
    assert found[("E-252", "FM-RELIEF-VALVE-DRIFT")] == {
        "likelihood": 0.7,
        "support": 0,
    }
    assert all(src != "E-200" for src, _ in found)  # an info code has no causes


def test_resolved_by_runs_from_a_failure_mode_to_the_remedies_in_the_troubleshooting_table(
    bh, kx
):
    assert sorted(edges(bh, "RESOLVED_BY")) == [
        ("FM-CONTAMINATED-OIL", "PROC-HP-05"),
        ("FM-CYLINDER-SEAL-WEAR", "PROC-HP-04"),
        ("FM-RELIEF-VALVE-DRIFT", "PROC-HP-06"),
    ]
    # A cause is not tied to the first remedy of every alarm that names it:
    # the relief valve is a cause of E-217, but E-217 starts with the seal kit.
    assert ("FM-RELIEF-VALVE-DRIFT", "PROC-HP-04") not in edges(bh, "RESOLVED_BY")
    assert ("FM-SPINDLE-BEARING-WEAR", "PROC-CNC-02") in edges(kx, "RESOLVED_BY")


def test_a_failure_mode_with_no_remedy_takes_the_procedure_of_its_only_alarm(
    monkeypatch,
):
    canned = canned_bh()
    for mode in canned.failure_modes:
        mode.procedure_refs = []
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    # E-230 has the single cause "contaminated hydraulic oil" and PROC-HP-05; E-245 has
    # the single cause "worn main cylinder seals" and PROC-HP-04; E-252 the relief valve.
    assert sorted(edges(result, "RESOLVED_BY")) == [
        ("FM-CONTAMINATED-OIL", "PROC-HP-05"),
        ("FM-CYLINDER-SEAL-WEAR", "PROC-HP-04"),
        ("FM-RELIEF-VALVE-DRIFT", "PROC-HP-06"),
    ]


def test_requires_carries_the_quantity(bh):
    found = {
        (e.src.key, e.dst.key): e.props["qty"] for e in bh.edges if e.rel == "REQUIRES"
    }
    assert found == {
        ("PROC-HP-04", "SEAL-KIT-50"): 1,
        ("PROC-HP-04", "OIL-HLP46-20L"): 1,
        ("PROC-HP-05", "OIL-HLP46-20L"): 3,
        ("PROC-HP-05", "FLT-HYD-10"): 1,
        ("PROC-HP-06", "SEAL-RV-25"): 1,
    }


def test_fits_runs_from_each_spare_part_to_the_matching_components(bh, kx):
    found = set(edges(bh, "FITS"))
    assert ("OIL-HLP46-20L", "HP-01-PUMP") in found
    assert ("OIL-HLP46-20L", "HP-01-CYL") in found
    assert ("SEAL-RV-25", "HP-01-VALVE") in found
    assert ("SEAL-RV-25", "HP-01-CYL") not in found
    assert len(edges(kx, "FITS")) == 4  # two parts on two machines each


def test_a_name_that_matches_no_component_is_skipped_with_a_note(monkeypatch):
    canned = canned_bh()
    canned.spare_parts[0].fits = ["Gearbox"]
    canned.failure_modes[0].component_name = "Gearbox"
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert ("COUPLING-PMP-28", "HP-01-PUMP") not in edges(result, "FITS")
    assert not any(dst.endswith("Gearbox") for _, dst in edges(result, "FITS"))
    assert len([note for note in result.notes if "Gearbox" in note]) == 2


def test_a_cause_that_names_no_failure_mode_is_skipped_with_a_note(monkeypatch):
    canned = canned_bh()
    canned.alarms[1].causes[0].phrase = "gremlins"
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert ("E-217", "FM-CYLINDER-SEAL-WEAR") not in edges(result, "INDICATES")
    assert any("gremlins" in note for note in result.notes)


def test_a_remedy_that_is_not_a_procedure_of_the_manual_is_skipped_with_a_note(
    monkeypatch,
):
    canned = canned_bh()
    canned.failure_modes[0].procedure_refs = ["PROC-HP-99"]
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert ("FM-CYLINDER-SEAL-WEAR", "PROC-HP-99") not in edges(result, "RESOLVED_BY")
    assert any("PROC-HP-99" in note for note in result.notes)


# --- Props ----------------------------------------------------------------------

IDENTITY = {
    "Asset": "id",
    "Component": "id",
    "FailureMode": "id",
    "Procedure": "id",
    "Line": "id",
    "AlarmCode": "code",
    "Part": "part_no",
}


@pytest.mark.parametrize("which", ["bh", "kx"])
def test_every_canonical_node_carries_its_identity_equal_to_its_key(which, request):
    extraction = request.getfixturevalue(which)
    canonical = [n for n in extraction.nodes if n.canonical]
    assert {n.ref.type for n in canonical} >= {
        "Asset",
        "Component",
        "FailureMode",
        "Procedure",
        "AlarmCode",
        "Part",
    }
    for n in canonical:
        assert n.props[IDENTITY[n.ref.type]] == n.ref.key, (n.ref, n.props)


@pytest.mark.parametrize("which", ["bh", "kx"])
def test_canonical_nodes_validate_against_their_ontology_props(which, request):
    extraction = request.getfixturevalue(which)
    for n in extraction.nodes:
        if not n.canonical:
            continue
        if n.ref.type == "Asset":
            continue  # the inventory's Machines sheet supplies the rest on merge
        props_model(n.ref.type).model_validate(n.props)


def test_an_asset_node_carries_its_id_and_the_vendor_and_model(bh):
    asset = node(bh, "Asset", "HP-01")
    assert asset.props["id"] == "HP-01"
    assert asset.props["vendor"] == "Bramwell Hydraulics"
    assert asset.props["model"] == "BH-250"


def test_a_component_carries_the_id_and_the_asset_it_belongs_to(kx):
    tags = ALIASES.assets_for_model("KX-500")
    components = nodes_of(kx, "Component")
    assert components
    for n in components:
        assert n.props["asset_id"] in tags
        assert n.ref.key.startswith(f"{n.props['asset_id']}-")
        assert n.props["id"] == n.ref.key


def test_a_part_that_two_lists_name_carries_its_part_no(bh):
    seal = node(bh, "Part", "SEAL-KIT-50")
    assert seal.props["part_no"] == "SEAL-KIT-50"


# --- Evidence -------------------------------------------------------------------


def assert_quotes_come_from_the_pages(extraction: Extraction, path) -> None:
    pages = page_texts(path)
    seen = 0
    for item in [*extraction.nodes, *extraction.edges]:
        assert item.evidence, getattr(item, "ref", None) or item.rel
        for e in item.evidence:
            assert (e.extractor, e.confidence) == ("sol-manual", 0.9)
            assert e.doc_path == extraction.doc_path
            assert e.page in pages
            assert 0 < len(e.quote) <= 300
            assert e.quote in pages[e.page], (e.page, e.quote)
            seen += 1
    assert seen > 0


def test_every_quote_is_text_of_the_page_it_names_for_the_bh_250(bh):
    assert_quotes_come_from_the_pages(bh, BH)


def test_every_quote_is_text_of_the_page_it_names_for_the_kx_500(kx):
    assert_quotes_come_from_the_pages(kx, KX)


def test_evidence_lines_are_the_ones_with_the_code_the_plant_ref_or_the_part(bh):
    alarm = node(bh, "AlarmCode", "E-217").evidence[0]
    assert alarm.page == 3
    assert alarm.quote.splitlines()[0] == "E-217"
    assert "Main cylinder pressure loss" in alarm.quote
    proc = node(bh, "Procedure", "PROC-HP-04").evidence[0]
    assert (proc.page, proc.quote) == (
        5,
        "Plant ref: PROC-HP-04 | Lockout required: yes | Time: 180 min",
    )
    part = node(bh, "Part", "SEAL-KIT-50")
    assert {(e.page, e.quote.splitlines()[0]) for e in part.evidence} >= {
        (7, "SEAL-KIT-50")
    }
    cause = next(
        e
        for e in bh.edges
        if e.rel == "INDICATES"
        and (e.src.key, e.dst.key) == ("E-217", "FM-RELIEF-VALVE-DRIFT")
    )
    assert cause.evidence[0].page == 3
    assert "relief valve out of adjustment" in cause.evidence[0].quote


def footer_lines(path) -> set[str]:
    """The running footer of each page: the line before the page number."""
    footers = set()
    for text in page_texts(path).values():
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) >= 2 and re.fullmatch(r"Page \d+", lines[-1]):
            footers.add(lines[-2])
    return footers


@pytest.mark.parametrize(("which", "path"), [("bh", BH), ("kx", KX)])
def test_no_evidence_quote_contains_the_running_footer(which, path, request):
    extraction = request.getfixturevalue(which)
    footers = footer_lines(path)
    assert footers
    for item in [*extraction.nodes, *extraction.edges]:
        for e in item.evidence:
            assert not footers & set(e.quote.splitlines()), (e.page, e.quote)


def test_the_last_part_row_before_the_footer_ends_at_its_own_last_line(bh):
    wiper = node(bh, "Part", "WIPER-CYL-50").evidence[0]
    assert (
        wiper.quote == "WIPER-CYL-50\nCylinder rod wiper 50 mm\nMain cylinder and seals"
    )


def test_a_code_the_text_does_not_hold_has_no_evidence_and_a_note(monkeypatch):
    canned = canned_bh()
    canned.alarms[0].code = "E-299"
    monkeypatch.setattr(llm, "structured", FakeModel(canned))
    result = extract_manual(BH, ALIASES)
    assert node(result, "AlarmCode", "E-299").evidence == []
    assert any("E-299" in note for note in result.notes)


BULLETIN = DATA_DIR / "testset" / "ingest" / "06_service_bulletin_BH-250.pdf"


def bulletin_answer() -> ManualExtraction:
    """What a model reads from the one-alarm service bulletin of the test set, whose
    alarm code is in a sentence and not alone on a line as in a manual's alarm table."""
    return ManualExtraction(
        vendor="Bramwell Hydraulics",
        model="BH-250",
        components=[],
        failure_modes=[],
        alarms=[
            AlarmItem(
                code="E-217",
                message="main cylinder pressure loss",
                severity="warning",
                causes=[
                    CauseItem(
                        phrase="wear of the cylinder rod seals", rank="most likely"
                    )
                ],
                procedure_ref=None,
            )
        ],
        procedures=[],
        spare_parts=[],
    )


def test_an_alarm_code_inside_a_heading_or_sentence_gets_that_line_as_evidence(
    monkeypatch,
):
    monkeypatch.setattr(llm, "structured", FakeModel(bulletin_answer()))
    result = extract_manual(BULLETIN, ALIASES)
    alarm = node(result, "AlarmCode", "E-217")
    assert [(e.page, e.quote) for e in alarm.evidence] == [
        (
            1,
            "2 Alarm E-217\nThe controller raises E-217, main cylinder pressure loss, "
            "when the main cylinder cannot",
        )
    ]
    assert not [n for n in result.notes if "holds the alarm code" in n]


def test_a_line_that_is_only_the_code_still_comes_before_a_line_that_holds_it(bh):
    # The alarm table of a manual puts the code alone on a line, and that wins.
    assert node(bh, "AlarmCode", "E-217").evidence[0].quote.splitlines()[0] == "E-217"


# --- Chunks ---------------------------------------------------------------------


def test_the_extraction_carries_the_chunks_in_reading_order(bh):
    doc = document_id(bh.doc_path)
    assert [c.ord for c in bh.chunks] == list(range(len(bh.chunks)))
    assert len(bh.chunks) >= 8
    for c in bh.chunks:
        assert c.id == chunk_id(doc, c.page, c.ord)
        assert c.doc_path == bh.doc_path
        assert MIN_CHUNK <= len(c.text) <= MAX_CHUNK
    assert [c.page for c in bh.chunks] == sorted(c.page for c in bh.chunks)
    assert len({c.id for c in bh.chunks}) == len(bh.chunks)


def test_an_extraction_reads_back_from_json_equal(bh):
    assert Extraction.model_validate_json(bh.model_dump_json()) == bh


# --- The router -----------------------------------------------------------------


def test_the_router_registers_the_manual_extractor():
    assert router.REGISTRY[("manual", ".pdf")] is extract_manual
    assert router.extractor_for(BH) is extract_manual
