"""Tests for the generated vendor manuals, four PDFs under raw/manuals.

Every render goes to a temporary folder. The tests read each PDF as plain text, the
way the ingestion does, and take what to expect from the catalogue. Only the file
names, the cause wording and the fixed text of the manuals are written out here.
"""

import json
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import SEED
from pypdf import PdfReader
from reportlab import rl_config
from reportlab.graphics.shapes import Rect, String

from app.generator import __main__ as cli
from app.generator import catalogue as cat
from app.generator.mentions import ManualMentions
from app.generator.render_manuals import (
    VENDOR_COLOURS,
    look_of,
    render_manuals,
    seal_order_figure,
)
from app.generator.world import build_world

MODELS = ("KX-500", "BH-250", "OR-6", "VT-LD20")
FILE_NAMES = {
    "KX-500": "Kessler_KX-500_Operator_Maintenance_Manual_rev3.pdf",
    "BH-250": "BH-250 service manual.pdf",
    "OR-6": "Orbis_OR6_cell_maintenance_guide.pdf",
    "VT-LD20": "VT-LD20_manual_EN.pdf",
}
TITLES = {
    "KX-500": "Operator and Maintenance Manual",
    "BH-250": "Service Manual",
    "OR-6": "Cell Maintenance Guide",
    "VT-LD20": "Operating and Maintenance Manual",
}
MACHINES = {
    "KX-500": ["CNC-01", "CNC-02"],
    "BH-250": ["HP-01"],
    "OR-6": ["RW-01"],
    "VT-LD20": ["LT-01"],
}
PROCEDURES = {
    "KX-500": ["PROC-CNC-01", "PROC-CNC-02", "PROC-CNC-03", "PROC-CNC-04"],
    "BH-250": ["PROC-HP-04", "PROC-HP-05", "PROC-HP-06"],
    "OR-6": ["PROC-RW-01", "PROC-RW-02", "PROC-RW-03"],
    "VT-LD20": ["PROC-LT-01", "PROC-LT-02"],
}
FAILURE_MODES = {
    "KX-500": ["FM-SPINDLE-BEARING", "FM-COOLANT-BLOCK", "FM-TOOL-CLAMP"],
    "BH-250": ["FM-SEAL-WEAR", "FM-OIL-CONTAM", "FM-RELIEF-DRIFT"],
    "OR-6": ["FM-GRIPPER-WEAR", "FM-SERVO-OVERLOAD", "FM-LIGHT-CURTAIN"],
    "VT-LD20": ["FM-SENSOR-DRIFT", "FM-FIXTURE-SEAL"],
}
PHRASING = {
    "FM-SPINDLE-BEARING": "worn spindle bearings",
    "FM-COOLANT-BLOCK": "blocked coolant filter",
    "FM-TOOL-CLAMP": "weak tool clamp springs or faulty clamp sensor",
    "FM-SEAL-WEAR": "worn main cylinder seals",
    "FM-OIL-CONTAM": "contaminated hydraulic oil",
    "FM-RELIEF-DRIFT": "relief valve out of adjustment",
    "FM-GRIPPER-WEAR": "worn gripper fingers",
    "FM-SERVO-OVERLOAD": "axis 2 overload from binding or a failing drive",
    "FM-LIGHT-CURTAIN": "misaligned light curtain",
    "FM-SENSOR-DRIFT": "pressure sensor drift",
    "FM-FIXTURE-SEAL": "worn fixture seals",
}
# The words the lockout paragraph of each machine uses for the energy it stores.
STORED_ENERGY = {
    "KX-500": ["compressed air"],
    "BH-250": ["stored hydraulic energy", "accumulator"],
    "OR-6": ["drive capacitors"],
    "VT-LD20": ["compressed air"],
}
CHAPTERS = (
    "1 Safety",
    "2 Alarm table",
    "3 Troubleshooting table",
    "4 Procedures",
    "5 Spare parts list",
)
SECTION = {
    proc_id: f"4.{number}"
    for ids in PROCEDURES.values()
    for number, proc_id in enumerate(ids, start=1)
}
LOCKOUT_WARNING = (
    "WARNING: Apply lockout and tagout before starting. Verify zero energy."
)
NO_ISOLATION = "No isolation needed. Work from the HMI only."
SEAL_KIT_ROW = "SK-50 seal kit (order no. SEAL-KIT-50)"
FIGURE_LABELS = ("wiper", "rod seal", "guide ring", "piston seal")

# --- Reading the PDFs -----------------------------------------------------------------


def squash(text: str) -> str:
    """One line, one space between words: a wrapped cell reads as it was written."""
    return " ".join(text.split())


def extracted_pages(path: Path) -> list[str]:
    return [page.extract_text() for page in PdfReader(path).pages]


@dataclass(frozen=True)
class Manual:
    model: str
    pages: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.pages)


def manual_path(out: Path, model: str) -> Path:
    return out / "raw" / "manuals" / FILE_NAMES[model]


def read_manuals(out: Path) -> dict[str, Manual]:
    return {
        model: Manual(model, extracted_pages(manual_path(out, model)))
        for model in MODELS
    }


def heading_line(heading: str) -> str:
    """A pattern for a line that holds the heading and nothing else."""
    return rf"(?m)^[ \t]*{re.escape(heading)}[ \t]*$"


def between(text: str, first: str, last: str | None) -> str:
    """The text from the line that is first up to the line that is last."""
    start = re.search(heading_line(first), text)
    assert start, f"no line reads {first!r}"
    if last is None:
        return text[start.start() :]
    end = re.search(heading_line(last), text)
    assert end, f"no line reads {last!r}"
    return text[start.start() : end.start()]


def chapter(manual: Manual, number: int) -> str:
    last = CHAPTERS[number] if number < len(CHAPTERS) else None
    return between(manual.text, CHAPTERS[number - 1], last)


def procedure_heading(proc_id: str) -> str:
    title = cat.PROCEDURE_BY_ID[proc_id].props.title
    return f"{SECTION[proc_id]} {title}"


def procedure_section(manual: Manual, proc_id: str) -> str:
    ids = PROCEDURES[manual.model]
    after = ids.index(proc_id) + 1
    last = procedure_heading(ids[after]) if after < len(ids) else CHAPTERS[4]
    return between(manual.text, procedure_heading(proc_id), last)


def rows_starting_at(flat: str, keys: list[str]) -> dict[str, str]:
    """The text of each table row, in one line. A row runs from its first cell, the
    key, up to the first cell of the next row."""
    starts = [flat.index(key) for key in keys]
    assert starts == sorted(starts), "the rows are out of order"
    ends = [*starts[1:], len(flat)]
    return {key: flat[start:end] for key, start, end in zip(keys, starts, ends)}


def assert_in_order(row: str, items: list[str], where: str) -> None:
    """Each item is in the row, after the one before it."""
    position = 0
    for item in items:
        found = row.find(item, position)
        assert found >= 0, f"{where}: {item!r} is missing or out of order in {row!r}"
        position = found + len(item)


# --- What the catalogue says about a model --------------------------------------------


def alarms_of(model: str) -> list[cat.AlarmSpec]:
    return [spec for spec in cat.ALARMS if set(spec.machines) & set(MACHINES[model])]


def components_of(model: str) -> set[str]:
    return {comp.id for comp in cat.COMPONENTS if comp.asset_id in MACHINES[model]}


def parts_of(model: str) -> list[cat.PartSpec]:
    """Every part that fits a component of the model, by part number."""
    found = [spec for spec in cat.PARTS if components_of(model) & set(spec.fits)]
    return sorted(found, key=lambda spec: spec.props.part_no)


def causes_of(alarm: cat.AlarmSpec) -> list[str]:
    """The failure modes an alarm indicates, most likely first."""
    ranked = sorted(alarm.indicates, key=lambda pair: -pair[1])
    return [failure_mode for failure_mode, _ in ranked]


def cause_labels(count: int) -> list[str]:
    """The first of the causes is labelled most likely and the rest possible."""
    return ["most likely", *["possible"] * (count - 1)][:count]


def procedures_for(failure_mode: str) -> list[cat.ProcedureSpec]:
    """The procedures that resolve a failure mode: the main fix, then a quick check."""
    found = [spec for spec in cat.PROCEDURES if spec.resolves == failure_mode]
    return sorted(found, key=lambda spec: not spec.main_fix)


def reference(proc_id: str) -> str:
    """How a table cell names a procedure: its section, title and plant reference."""
    return f"{procedure_heading(proc_id)} (plant ref {proc_id})"


# --- Fixtures -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rendered(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("manuals")
    return out, render_manuals(world, random.Random(SEED + 4), out)


@pytest.fixture(scope="module")
def manuals(rendered) -> dict[str, Manual]:
    out, _ = rendered
    return read_manuals(out)


@pytest.fixture(scope="module")
def two_renders(tmp_path_factory):
    """The same render twice with seed 42, in two folders."""
    world = build_world(42)
    outs = []
    for name in ("first", "second"):
        out = tmp_path_factory.mktemp(name)
        render_manuals(world, random.Random(world.seed + 4), out)
        outs.append(out)
    return outs


@pytest.fixture(scope="module")
def command_line_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("cli")
    assert cli.main(["--seed", "42", "--out", str(out)]) == 0
    return out


# --- The files ------------------------------------------------------------------------


def test_the_four_pdfs_exist_with_the_exact_names_and_pypdf_opens_each(rendered):
    out, _ = rendered
    folder = out / "raw" / "manuals"
    assert sorted(path.name for path in folder.iterdir()) == sorted(FILE_NAMES.values())
    for model in MODELS:
        path = manual_path(out, model)
        assert path.read_bytes().startswith(b"%PDF-")
        assert len(PdfReader(path).pages) >= 1


@pytest.mark.parametrize("model", MODELS)
def test_each_manual_has_4_to_8_pages(manuals, model):
    assert 4 <= len(manuals[model].pages) <= 8


def test_the_catalogue_alarms_split_between_the_four_models():
    codes = [spec.props.code for model in MODELS for spec in alarms_of(model)]
    assert sorted(codes) == sorted(spec.props.code for spec in cat.ALARMS)


@pytest.mark.parametrize("model", MODELS)
def test_the_text_has_every_alarm_code_of_its_model_and_every_plant_reference(
    manuals, model
):
    text = manuals[model].text
    codes = [spec.props.code for spec in alarms_of(model)]
    assert codes and all(code in text for code in codes)
    assert all(proc_id in text for proc_id in PROCEDURES[model])
    assert not [
        code
        for other in MODELS
        if other != model
        for code in (spec.props.code for spec in alarms_of(other))
        if code in text
    ]


@pytest.mark.parametrize("model", MODELS)
def test_the_lockout_warning_appears_once_for_each_lockout_procedure(manuals, model):
    flat = squash(manuals[model].text)
    procedures = [cat.PROCEDURE_BY_ID[proc_id] for proc_id in PROCEDURES[model]]
    lockout = sum(spec.props.requires_loto for spec in procedures)
    assert flat.count("WARNING: Apply lockout") == lockout
    assert flat.count(LOCKOUT_WARNING) == lockout
    assert flat.count(NO_ISOLATION) == len(procedures) - lockout


def test_the_lockout_warnings_in_all_four_manuals_match_the_catalogue():
    counts = {
        model: sum(
            cat.PROCEDURE_BY_ID[proc_id].props.requires_loto
            for proc_id in PROCEDURES[model]
        )
        for model in MODELS
    }
    assert counts == {"KX-500": 3, "BH-250": 3, "OR-6": 3, "VT-LD20": 1}


# --- Repeatable output ----------------------------------------------------------------


def test_extracted_text_is_identical_across_two_renders_with_seed_42(two_renders):
    first, second = (read_manuals(out) for out in two_renders)
    assert first == second


def test_the_pdf_bytes_are_identical_across_two_renders_with_seed_42(two_renders):
    first, second = two_renders
    for model in MODELS:
        assert manual_path(first, model).read_bytes() == (
            manual_path(second, model).read_bytes()
        ), model


def test_a_render_leaves_the_reportlab_settings_as_it_found_them(
    world, tmp_path, monkeypatch
):
    # Start from a known value: an earlier render must not hide a setting that leaks.
    monkeypatch.setattr(rl_config, "invariant", 0)
    monkeypatch.setattr(rl_config, "pageCompression", 1)
    render_manuals(world, random.Random(0), tmp_path)
    assert (rl_config.invariant, rl_config.pageCompression) == (0, 1)


def test_the_bytes_do_not_depend_on_source_date_epoch(
    world, tmp_path_factory, monkeypatch
):
    """reportlab takes the file dates from SOURCE_DATE_EPOCH when it is set, even in
    invariant mode, so a render hides it."""
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    plain = tmp_path_factory.mktemp("no_epoch")
    render_manuals(world, random.Random(0), plain)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    stamped = tmp_path_factory.mktemp("epoch")
    render_manuals(world, random.Random(0), stamped)
    assert os.environ["SOURCE_DATE_EPOCH"] == "1700000000"
    for model in MODELS:
        assert manual_path(plain, model).read_bytes() == (
            manual_path(stamped, model).read_bytes()
        ), model


def test_the_manuals_draw_no_random_numbers_and_ignore_the_world_seed(
    tmp_path_factory,
):
    rng = random.Random(1)
    state = rng.getstate()
    one = tmp_path_factory.mktemp("seed_a")
    other = tmp_path_factory.mktemp("seed_b")
    render_manuals(build_world(42), rng, one)
    render_manuals(build_world(43), random.Random(2), other)
    assert rng.getstate() == state
    for model in MODELS:
        assert manual_path(one, model).read_bytes() == (
            manual_path(other, model).read_bytes()
        ), model


# --- Mentions -------------------------------------------------------------------------


def expected_mentions(model: str) -> dict[str, list[str]]:
    proc_parts = {
        part_no
        for proc_id in PROCEDURES[model]
        for part_no, _ in cat.PROCEDURE_BY_ID[proc_id].parts
    }
    fitting = {spec.props.part_no for spec in parts_of(model)}
    return {
        "assets": sorted(MACHINES[model]),
        "alarm_codes": sorted(spec.props.code for spec in alarms_of(model)),
        "failure_modes": sorted(FAILURE_MODES[model]),
        "procedures": sorted(PROCEDURES[model]),
        "parts": sorted(fitting | proc_parts),
        "incidents": [],
        "conflicts": [],
    }


def test_the_render_returns_the_mentions_of_each_manual(rendered):
    _, mentions = rendered
    assert sorted(mentions) == sorted(
        f"raw/manuals/{name}" for name in FILE_NAMES.values()
    )
    for model in MODELS:
        mention = mentions[f"raw/manuals/{FILE_NAMES[model]}"]
        assert isinstance(mention, ManualMentions)
        assert mention.model_dump() == expected_mentions(model)


def test_mentions_json_has_an_entry_for_each_manual(command_line_run):
    mentions = json.loads(
        (command_line_run / "truth" / "mentions.json").read_text(encoding="utf-8")
    )
    for model in MODELS:
        assert mentions[f"raw/manuals/{FILE_NAMES[model]}"] == expected_mentions(model)


def test_every_part_a_procedure_needs_is_in_the_spare_parts_of_its_model():
    for model in MODELS:
        fitting = {spec.props.part_no for spec in parts_of(model)}
        needed = {
            part_no
            for proc_id in PROCEDURES[model]
            for part_no, _ in cat.PROCEDURE_BY_ID[proc_id].parts
        }
        assert needed <= fitting, model


# --- The cover and the safety chapter -------------------------------------------------


@pytest.mark.parametrize("model", MODELS)
def test_the_cover_names_the_vendor_model_title_revision_and_audience(manuals, model):
    cover = squash(manuals[model].pages[0])
    vendor = next(asset.vendor for asset in cat.ASSETS if asset.model == model)
    for expected in (
        vendor,
        model,
        TITLES[model],
        "Revision 3",
        "For qualified maintenance personnel",
    ):
        assert expected in cover, expected


@pytest.mark.parametrize("model", MODELS)
def test_a_manual_names_its_model_and_no_plant_asset_tag(manuals, model):
    text = manuals[model].text
    assert model in text
    for tag in cat.ASSET_BY_ID:
        # A plant reference such as PROC-RW-01 ends in what looks like a tag.
        assert not re.search(rf"(?<![\w-]){re.escape(tag)}(?![\w-])", text), tag
    assert not [comp for comp in cat.COMPONENT_BY_ID if comp in text]
    assert not [other for other in MODELS if other != model and other in text]


@pytest.mark.parametrize("model", MODELS)
def test_the_safety_chapter_has_a_general_paragraph_and_a_lockout_paragraph(
    manuals, model
):
    safety = squash(chapter(manuals[model], 1))
    assert "lockout and tagout" in safety.lower()
    assert "zero energy" in safety
    assert "WARNING: Apply lockout" not in safety
    for phrase in STORED_ENERGY[model]:
        assert phrase in safety, phrase


@pytest.mark.parametrize("model", MODELS)
def test_the_two_wide_tables_sit_on_landscape_pages_and_every_other_page_is_portrait(
    rendered, manuals, model
):
    out, _ = rendered
    pages = manuals[model].pages
    first = next(
        n for n, page in enumerate(pages) if re.search(heading_line(CHAPTERS[1]), page)
    )
    after = next(
        n for n, page in enumerate(pages) if re.search(heading_line(CHAPTERS[3]), page)
    )
    for number, page in enumerate(PdfReader(manual_path(out, model)).pages):
        landscape = page.mediabox.width > page.mediabox.height
        assert landscape == (first <= number < after), f"page {number + 1}"


@pytest.mark.parametrize("model", MODELS)
def test_every_page_after_the_cover_ends_with_the_manual_name_and_its_page_number(
    manuals, model
):
    vendor = next(asset.vendor for asset in cat.ASSETS if asset.model == model)
    name = f"{vendor} {model} {TITLES[model]}, Revision 3"
    pages = manuals[model].pages
    assert "Page 1" not in pages[0]
    for number, text in enumerate(pages[1:], start=2):
        last_two = [line.strip() for line in text.strip().split("\n")][-2:]
        assert last_two == [name, f"Page {number}"], f"page {number}"


# --- The alarm table ------------------------------------------------------------------


@pytest.mark.parametrize("model", MODELS)
def test_each_alarm_row_gives_the_code_message_and_severity(manuals, model):
    flat = squash(chapter(manuals[model], 2))
    assert "Code Message Severity Likely causes Action" in flat
    codes = [spec.props.code for spec in alarms_of(model)]
    for code, row in rows_starting_at(flat, codes).items():
        props = cat.ALARM_BY_CODE[code].props
        assert row.startswith(f"{code} {props.text} {props.severity} "), code


@pytest.mark.parametrize("model", MODELS)
def test_the_likely_causes_follow_the_manual_phrasing_most_likely_first(manuals, model):
    flat = squash(chapter(manuals[model], 2))
    codes = [spec.props.code for spec in alarms_of(model)]
    rows = rows_starting_at(flat, codes)
    for code, row in rows.items():
        alarm = cat.ALARM_BY_CODE[code]
        causes = causes_of(alarm)
        if not causes:
            assert alarm.props.severity == "info"
            assert "info none " in row, code
            continue
        labels = cause_labels(len(causes))
        expected = [f"{PHRASING[fm]} ({label})" for fm, label in zip(causes, labels)]
        assert_in_order(row, expected, code)
    with_causes = [code for code in codes if causes_of(cat.ALARM_BY_CODE[code])]
    assert flat.count("(most likely)") == len(with_causes)
    assert flat.count("(possible)") == sum(
        len(causes_of(cat.ALARM_BY_CODE[code])) - 1 for code in with_causes
    )


@pytest.mark.parametrize("model", MODELS)
def test_the_action_names_the_procedure_section_and_its_plant_reference(manuals, model):
    flat = squash(chapter(manuals[model], 2))
    codes = [spec.props.code for spec in alarms_of(model)]
    for code, row in rows_starting_at(flat, codes).items():
        causes = causes_of(cat.ALARM_BY_CODE[code])
        if not causes:
            assert "PROC-" not in row, code
            continue
        expected = [
            reference(spec.props.id) for fm in causes for spec in procedures_for(fm)
        ]
        assert_in_order(row, expected, code)
        assert row.count("PROC-") == len(expected), code


@pytest.mark.parametrize("model", MODELS)
def test_no_line_of_a_table_cell_is_cut_by_the_column_width(manuals, model):
    """Each message, each cause with its label, each symptom and each procedure
    reference is one line of extracted text, so the tables read the same in a plain
    extraction as they do on the page."""
    alarm_lines = [line.strip() for line in chapter(manuals[model], 2).split("\n")]
    for spec in alarms_of(model):
        assert spec.props.text in alarm_lines
        causes = causes_of(spec)
        for failure_mode, label in zip(causes, cause_labels(len(causes)), strict=True):
            assert f"{PHRASING[failure_mode]} ({label})" in alarm_lines
            for proc in procedures_for(failure_mode):
                assert f"See {procedure_heading(proc.props.id)}" in alarm_lines
                assert f"(plant ref {proc.props.id})" in alarm_lines
    trouble_lines = [line.strip() for line in chapter(manuals[model], 3).split("\n")]
    for failure_mode in FAILURE_MODES[model]:
        assert PHRASING[failure_mode] in trouble_lines
        assert cat.SYMPTOMS[failure_mode][0] in trouble_lines


# --- The troubleshooting table --------------------------------------------------------


@pytest.mark.parametrize("model", MODELS)
def test_the_troubleshooting_table_has_one_row_for_each_failure_mode(manuals, model):
    flat = squash(chapter(manuals[model], 3))
    assert "Symptom Probable cause Remedy" in flat
    symptoms = [cat.SYMPTOMS[fm][0] for fm in FAILURE_MODES[model]]
    rows = rows_starting_at(flat, symptoms)
    for fm, row in zip(FAILURE_MODES[model], rows.values(), strict=True):
        expected = [
            cat.SYMPTOMS[fm][0],
            PHRASING[fm],
            *(reference(spec.props.id) for spec in procedures_for(fm)),
        ]
        assert_in_order(row, expected, fm)
        assert flat.count(PHRASING[fm]) == 1
        assert cat.SYMPTOMS[fm][1] not in flat


# --- The procedures -------------------------------------------------------------------


def procedure_cases() -> list[tuple[str, str]]:
    return [(model, proc_id) for model in MODELS for proc_id in PROCEDURES[model]]


@pytest.mark.parametrize(("model", "proc_id"), procedure_cases())
def test_a_procedure_opens_with_its_heading_and_its_plant_reference_line(
    manuals, model, proc_id
):
    props = cat.PROCEDURE_BY_ID[proc_id].props
    lines = [
        line.strip() for line in procedure_section(manuals[model], proc_id).split("\n")
    ]
    ref = (
        f"Plant ref: {proc_id} | Lockout required: "
        f"{'yes' if props.requires_loto else 'no'} | Time: {props.est_minutes} min"
    )
    assert lines[0] == procedure_heading(proc_id)
    assert lines[1] == ref


@pytest.mark.parametrize(("model", "proc_id"), procedure_cases())
def test_a_procedure_has_a_lockout_warning_or_the_no_isolation_line(
    manuals, model, proc_id
):
    props = cat.PROCEDURE_BY_ID[proc_id].props
    lines = [
        line.strip() for line in procedure_section(manuals[model], proc_id).split("\n")
    ]
    expected, other = (
        (LOCKOUT_WARNING, NO_ISOLATION)
        if props.requires_loto
        else (NO_ISOLATION, LOCKOUT_WARNING)
    )
    assert lines[2] == expected
    assert other not in lines


@pytest.mark.parametrize(("model", "proc_id"), procedure_cases())
def test_the_steps_are_numbered_and_worded_as_in_the_catalogue(manuals, model, proc_id):
    flat = squash(procedure_section(manuals[model], proc_id))
    steps = cat.PROCEDURE_BY_ID[proc_id].props.steps
    numbered = [f"{number}. {step}" for number, step in enumerate(steps, start=1)]
    assert_in_order(flat, numbered, proc_id)


@pytest.mark.parametrize(("model", "proc_id"), procedure_cases())
def test_the_parts_table_lists_the_canonical_part_numbers_with_their_quantities(
    manuals, model, proc_id
):
    flat = squash(procedure_section(manuals[model], proc_id))
    parts = cat.PROCEDURE_BY_ID[proc_id].parts
    if not parts:
        assert "Parts required: none." in flat
        assert "Order no." not in flat
        return
    assert "Order no. Description Qty" in flat
    for part_no, qty in parts:
        description = cat.PART_BY_NO[part_no].props.description
        if (model, part_no) == ("BH-250", "SEAL-KIT-50"):
            description = SEAL_KIT_ROW
        assert f"{part_no} {description} {qty}" in flat, part_no
    assert flat.count("Order no. Description Qty") == 1


@pytest.mark.parametrize("model", MODELS)
def test_each_chapter_heading_shares_its_page_with_the_text_that_follows(
    manuals, model
):
    """A heading stranded alone at the foot of a page, or a page that holds only a
    heading, shows that a block did not fit where the heading left it."""
    first_procedure = procedure_heading(PROCEDURES[model][0])
    for heading, follows in (
        (CHAPTERS[3], first_procedure),
        (CHAPTERS[4], "Part number"),
    ):
        (page,) = [
            text
            for text in manuals[model].pages
            if re.search(heading_line(heading), text)
        ]
        assert follows in page, f"{heading!r} is alone on its page"


def test_the_seal_kit_wording_is_a_catalogue_alias_and_only_the_press_manual_uses_it(
    manuals,
):
    assert "SK-50" in cat.PART_BY_NO["SEAL-KIT-50"].props.aliases
    for model in MODELS:
        assert (SEAL_KIT_ROW in squash(manuals[model].text)) == (model == "BH-250")


def test_only_the_press_manual_has_figure_4_with_the_seals_in_order(manuals):
    for model in MODELS:
        assert ("Figure 4: seal order" in manuals[model].text) == (model == "BH-250")
    section = procedure_section(manuals["BH-250"], "PROC-HP-04")
    lines = [line.strip() for line in section.split("\n")]
    positions = [
        lines.index(label) for label in (*FIGURE_LABELS, "Figure 4: seal order")
    ]
    assert positions == sorted(positions)
    step = cat.PROCEDURE_BY_ID["PROC-HP-04"].props.steps[3]
    assert "figure 4" in step
    assert squash(section).index(step) < squash(section).index("wiper")


def test_the_figure_is_four_boxes_drawn_left_to_right_with_their_labels():
    figure = seal_order_figure()
    boxes = [shape for shape in figure.contents if isinstance(shape, Rect)]
    labels = [shape for shape in figure.contents if isinstance(shape, String)]
    assert [label.text for label in labels] == list(FIGURE_LABELS)
    assert len(boxes) == 4
    assert [box.x for box in boxes] == sorted(box.x for box in boxes)
    for box, label in zip(boxes, labels, strict=True):
        assert box.x < label.x < box.x + box.width


# --- The spare parts list -------------------------------------------------------------


@pytest.mark.parametrize("model", MODELS)
def test_the_spare_parts_list_has_every_part_that_fits_a_component_of_the_model(
    manuals, model
):
    flat = squash(chapter(manuals[model], 5))
    assert "Part number Description Fits" in flat
    parts = parts_of(model)
    rows = rows_starting_at(flat, [spec.props.part_no for spec in parts])
    for spec in parts:
        fits = list(
            dict.fromkeys(
                cat.COMPONENT_BY_ID[comp].name
                for comp in spec.fits
                if comp in components_of(model)
            )
        )
        row = rows[spec.props.part_no]
        expected = [spec.props.part_no, spec.props.description, *fits]
        assert_in_order(row, expected, spec.props.part_no)
    assert "Spindle Spindle" not in flat


@pytest.mark.parametrize("model", MODELS)
def test_a_manual_names_no_part_that_fits_only_other_models(manuals, model):
    text = manuals[model].text
    mine = {spec.props.part_no for spec in parts_of(model)}
    assert not [
        spec.props.part_no
        for spec in cat.PARTS
        if spec.props.part_no not in mine and spec.props.part_no in text
    ]


def test_the_shared_sensor_is_the_only_part_that_fits_two_models():
    shared = [
        spec.props.part_no
        for spec in cat.PARTS
        if sum(bool(components_of(model) & set(spec.fits)) for model in MODELS) > 1
    ]
    assert shared == ["SENS-PT100-01"]


def test_the_shared_sensor_fits_the_spindle_in_one_manual_and_the_pump_in_the_other(
    manuals,
):
    def row_of(model: str) -> str:
        flat = squash(chapter(manuals[model], 5))
        return rows_starting_at(flat, ["SENS-PT100-01"])["SENS-PT100-01"]

    assert "Spindle" in row_of("KX-500")
    assert "Hydraulic pump" not in row_of("KX-500")
    assert "Hydraulic pump" in row_of("BH-250")
    assert "Spindle" not in row_of("BH-250")


# --- The look -------------------------------------------------------------------------


def luminance(colour) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in colour.rgb())
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def test_every_vendor_has_its_own_colour_that_carries_white_text():
    vendors = {asset.vendor for asset in cat.ASSETS}
    assert set(VENDOR_COLOURS) == vendors
    assert len(set(VENDOR_COLOURS.values())) == len(vendors)
    for vendor in vendors:
        colour = look_of(vendor).colour
        assert 1.05 / (luminance(colour) + 0.05) >= 4.5, vendor


@pytest.mark.parametrize("model", MODELS)
def test_a_manual_uses_the_standard_fonts_and_no_image(rendered, model):
    out, _ = rendered
    reader = PdfReader(manual_path(out, model))
    fonts = set()
    for page in reader.pages:
        resources = page["/Resources"]
        for font in resources["/Font"].values():
            font = font.get_object()
            fonts.add(font["/BaseFont"])
            assert "/FontDescriptor" not in font
        assert "/XObject" not in resources
    assert fonts <= {"/Helvetica", "/Helvetica-Bold", "/Helvetica-Oblique"}
    assert b"/Subtype /Image" not in manual_path(out, model).read_bytes()


@pytest.mark.parametrize("model", MODELS)
def test_the_cover_art_is_painted_before_the_first_text(rendered, model):
    out, _ = rendered
    stream = PdfReader(manual_path(out, model)).pages[0].get_contents().get_data()
    assert stream.index(b" re") < stream.index(b" Tj")
