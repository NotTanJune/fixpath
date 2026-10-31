"""Tests for the ontology, the list of node types and relations the graph can hold.
They cover id formats, property models and the exported file."""

import json
import subprocess
import sys
from datetime import date, datetime

import pytest
from pydantic import BaseModel, ValidationError

from app import ontology
from app.config import ROOT
from app.ontology import (
    FAMILIES,
    NODE_TYPES,
    RELATIONS,
    AlarmCodeProps,
    AssetProps,
    ComponentProps,
    DocumentProps,
    Evidence,
    FailureModeProps,
    IncidentProps,
    IndicatesProps,
    LineProps,
    PartProps,
    ProcedureProps,
    RaisedProps,
    RequiresProps,
    UsedProps,
    export_json,
    is_allowed,
    props_model,
    validate_id,
)

# --- Expected ontology, typed out by hand ----------------------------------

EXPECTED_FAMILIES = {
    "where": (
        "Where",
        "Where the problem lives: the line, its machines and their components.",
    ),
    "problem": ("Problem", "What went wrong: alarms and the root causes behind them."),
    "fix": ("Fix", "How to fix it: procedures and the spare parts they need."),
    "history": ("History", "What happened before: past breakdowns."),
    "source": ("Source", "Where each fact came from."),
}

# key: (UI label, glyph, family, description, ID pattern)
EXPECTED_NODE_TYPES = {
    "Line": (
        "Production line",
        "factory",
        "where",
        "A production line in the plant.",
        r"^LINE-\d{2}$",
    ),
    "Asset": (
        "Machine",
        "cog",
        "where",
        "A machine on the line.",
        r"^[A-Z]{2,3}-\d{2}$",
    ),
    "Component": (
        "Component",
        "bolt",
        "where",
        "A part of a machine that can fail, such as a spindle or a pump.",
        r"^[A-Z]{2,3}-\d{2}-[A-Z0-9]+$",
    ),
    "AlarmCode": (
        "Alarm",
        "siren",
        "problem",
        "A code the machine shows when something is wrong.",
        r"^[A-Z]{1,3}-\d{3}$",
    ),
    "FailureMode": (
        "Root cause",
        "unlink",
        "problem",
        "The underlying reason a machine stops.",
        r"^FM-[A-Z0-9-]+$",
    ),
    "Procedure": (
        "Fix",
        "wrench",
        "fix",
        "Steps that resolve a root cause.",
        r"^PROC-[A-Z]{2,3}-\d{2}$",
    ),
    "Part": (
        "Spare part",
        "package",
        "fix",
        "A spare part kept in stock.",
        r"^[A-Z0-9]+(-[A-Z0-9]+)+$",
    ),
    "Incident": (
        "Past breakdown",
        "history",
        "history",
        "A breakdown that happened before, and how it was fixed.",
        r"^INC-\d{4}-\d{3}$",
    ),
    "Document": (
        "Source",
        "file-text",
        "source",
        "A file from the shared folder that backs the facts.",
        r"^DOC-[0-9a-f]{8}$",
    ),
}

# key: [(property, type as exported, required)]
EXPECTED_NODE_PROPERTIES = {
    "Line": [("id", "str", True), ("name", "str", True)],
    "Asset": [
        ("id", "str", True),
        ("name", "str", True),
        ("machine_type", "str", True),
        ("vendor", "str", True),
        ("model", "str", True),
        ("aliases", "list[str]", False),
    ],
    "Component": [
        ("id", "str", True),
        ("name", "str", True),
        ("asset_id", "str", True),
    ],
    "AlarmCode": [
        ("code", "str", True),
        ("text", "str", True),
        ("severity", "Literal['info', 'warning', 'critical']", True),
        ("aliases", "list[str]", False),
    ],
    "FailureMode": [
        ("id", "str", True),
        ("name", "str", True),
        ("mechanism", "str", True),
    ],
    "Procedure": [
        ("id", "str", True),
        ("title", "str", True),
        ("steps", "list[str]", True),
        ("requires_loto", "bool", True),
        ("est_minutes", "int", True),
        ("manual_ref", "str | None", False),
    ],
    "Part": [
        ("part_no", "str", True),
        ("description", "str", True),
        ("aliases", "list[str]", False),
        ("qty_on_hand", "int | None", False),
        ("bin", "str | None", False),
        ("lead_time_days", "int | None", False),
        ("stock_as_of", "date | None", False),
        ("unit_cost_sgd", "float | None", False),
    ],
    "Incident": [
        ("id", "str", True),
        ("date", "date", True),
        ("shift", "Literal['A', 'B', 'C'] | None", False),
        ("downtime_min", "int | None", False),
        ("symptom_text", "str", True),
        ("technician", "str | None", False),
        ("resolution_text", "str | None", False),
    ],
    "Document": [
        ("id", "str", True),
        ("path", "str", True),
        (
            "doc_type",
            "Literal['log', 'handover', 'maintenance_note', 'inventory', 'manual', 'procedure']",
            True,
        ),
        ("sha256", "str", True),
    ],
}

# key: (label, from, to, [(property, type as exported, required)])
EXPECTED_RELATIONS = {
    "HAS_MACHINE": ("has machine", "Line", "Asset", []),
    "HAS_COMPONENT": ("is built from", "Asset", "Component", []),
    "RAISED": (
        "raised",
        "Asset",
        "AlarmCode",
        [
            ("count", "int", True),
            ("first_seen", "datetime", True),
            ("last_seen", "datetime", True),
        ],
    ),
    "INDICATES": (
        "usually means",
        "AlarmCode",
        "FailureMode",
        [("likelihood", "float", True), ("support", "int", True)],
    ),
    "AFFECTS": ("damages", "FailureMode", "Component", []),
    "RESOLVED_BY": ("fixed by", "FailureMode", "Procedure", []),
    "REQUIRES": ("needs", "Procedure", "Part", [("qty", "int", True)]),
    "FITS": ("fits", "Part", "Component", []),
    "OCCURRED_ON": ("happened on", "Incident", "Asset", []),
    "TRIGGERED_BY": ("started with", "Incident", "AlarmCode", []),
    "ROOT_CAUSE": ("was caused by", "Incident", "FailureMode", []),
    "USED": ("used", "Incident", "Part", [("qty", "int", True)]),
}

PROPS_MODELS = {
    "Line": LineProps,
    "Asset": AssetProps,
    "Component": ComponentProps,
    "AlarmCode": AlarmCodeProps,
    "FailureMode": FailureModeProps,
    "Procedure": ProcedureProps,
    "Part": PartProps,
    "Incident": IncidentProps,
    "Document": DocumentProps,
}


def as_dicts(properties):
    return [
        {"name": name, "type": type_name, "required": required}
        for name, type_name, required in properties
    ]


# --- Registries ---------------------------------------------------------------


def test_families_match_the_ontology_spec():
    assert list(FAMILIES) == list(EXPECTED_FAMILIES)
    for key, (label, description) in EXPECTED_FAMILIES.items():
        assert FAMILIES[key].key == key
        assert (FAMILIES[key].label, FAMILIES[key].description) == (label, description)


def test_node_types_match_the_ontology_spec():
    assert list(NODE_TYPES) == list(EXPECTED_NODE_TYPES)
    for key, expected in EXPECTED_NODE_TYPES.items():
        node_type = NODE_TYPES[key]
        assert node_type.key == key
        assert (
            node_type.label,
            node_type.glyph,
            node_type.family,
            node_type.description,
            node_type.id_pattern,
        ) == expected


def test_relations_match_the_ontology_spec():
    assert list(RELATIONS) == list(EXPECTED_RELATIONS)
    for key, (label, src, dst, _) in EXPECTED_RELATIONS.items():
        relation = RELATIONS[key]
        assert relation.key == key
        assert (relation.label, relation.src, relation.dst) == (label, src, dst)


def test_there_are_nine_node_types_and_twelve_relations():
    assert len(NODE_TYPES) == 9
    assert len(RELATIONS) == 12


def test_every_relation_connects_existing_node_types():
    for relation in RELATIONS.values():
        assert relation.src in NODE_TYPES, relation.key
        assert relation.dst in NODE_TYPES, relation.key


def test_every_node_type_has_a_glyph_and_a_known_family():
    for node_type in NODE_TYPES.values():
        assert node_type.glyph, node_type.key
        assert node_type.family in FAMILIES, node_type.key


# --- is_allowed -----------------------------------------------------------


def test_is_allowed_follows_the_relation_direction():
    assert is_allowed("Asset", "RAISED", "AlarmCode")
    assert not is_allowed("AlarmCode", "RAISED", "Asset")


def test_is_allowed_accepts_every_declared_relation():
    for key, (_, src, dst, _) in EXPECTED_RELATIONS.items():
        assert is_allowed(src, key, dst), key


def test_is_allowed_rejects_wrong_endpoints_and_unknown_names():
    assert not is_allowed("Asset", "RAISED", "FailureMode")
    assert not is_allowed("Line", "RAISED", "AlarmCode")
    assert not is_allowed("Asset", "NO_SUCH_RELATION", "AlarmCode")
    assert not is_allowed("NoSuchType", "RAISED", "AlarmCode")
    assert not is_allowed("Asset", "RAISED", "NoSuchType")


# --- validate_id ----------------------------------------------------------


@pytest.mark.parametrize(
    ("node_type", "value"),
    [
        ("Line", "LINE-03"),
        ("Asset", "CNC-01"),
        ("Component", "HP-01-PUMP"),
        ("AlarmCode", "E-217"),
        ("AlarmCode", "VT-012"),
        ("FailureMode", "FM-SEAL-WEAR"),
        ("Procedure", "PROC-HP-04"),
        ("Part", "SEAL-KIT-50"),
        ("Part", "BRG-6205-2RS"),
        ("Incident", "INC-2026-042"),
        ("Document", "DOC-0a1b2c3d"),
    ],
)
def test_valid_ids(node_type, value):
    assert validate_id(node_type, value)


@pytest.mark.parametrize("node_type", ["Asset", "AlarmCode"])
@pytest.mark.parametrize("value", ["HP01", "E217", "Press 2"])
def test_malformed_ids_fail(node_type, value):
    assert not validate_id(node_type, value)


@pytest.mark.parametrize(
    ("node_type", "value"),
    [
        ("Line", "LINE-03\n"),
        ("Line", "line-03"),
        ("Line", "LINE-3"),
        ("Line", "xLINE-03"),
        ("Line", "LINE-03x"),
        ("Line", "LINE-０３"),
        ("Asset", "CNC-01 "),
        ("Asset", "C-01"),
        ("Asset", "CNCX-01"),
        ("AlarmCode", "E-21"),
        ("AlarmCode", "E-2170"),
        ("FailureMode", "FM-"),
        ("Procedure", "PROC-HP-4"),
        ("Part", "SEAL"),
        ("Part", "seal-kit-50"),
        ("Incident", "INC-26-042"),
        ("Document", "DOC-0A1B2C3D"),
        ("Document", "DOC-0a1b2c3"),
    ],
)
def test_ids_are_matched_strictly(node_type, value):
    assert not validate_id(node_type, value)


def test_an_id_is_checked_against_its_own_type_only():
    assert not validate_id("Line", "CNC-01")
    assert not validate_id("Asset", "LINE-03")
    assert not validate_id("Asset", "E-217")
    assert not validate_id("AlarmCode", "CNC-01")


def test_validate_id_rejects_an_unknown_node_type():
    with pytest.raises(KeyError):
        validate_id("NoSuchType", "LINE-03")


# --- Property models --------------------------------------------------------


def test_props_model_returns_the_model_for_each_type():
    for key, model in PROPS_MODELS.items():
        assert props_model(key) is model
        assert issubclass(model, BaseModel)
        assert NODE_TYPES[key].props is model


def test_props_model_rejects_an_unknown_node_type():
    with pytest.raises(KeyError):
        props_model("NoSuchType")


def test_property_models_have_exactly_the_fields_in_the_ontology_spec():
    for key, expected in EXPECTED_NODE_PROPERTIES.items():
        fields = props_model(key).model_fields
        assert list(fields) == [name for name, _, _ in expected], key
        assert {n: f.is_required() for n, f in fields.items()} == {
            name: required for name, _, required in expected
        }, key


def test_property_models_accept_valid_values():
    asset = AssetProps(
        id="CNC-01",
        name="Lathe 1",
        machine_type="lathe",
        vendor="Kessler Werkzeugmaschinen",
        model="KL-200",
    )
    assert asset.aliases == []

    alarm = AlarmCodeProps(code="E-217", text="Spindle overload", severity="critical")
    assert alarm.aliases == []

    procedure = ProcedureProps(
        id="PROC-HP-04",
        title="Replace the seal",
        steps=["Isolate", "Swap the seal"],
        requires_loto=True,
        est_minutes=45,
    )
    assert procedure.manual_ref is None

    part = PartProps(part_no="SEAL-KIT-50", description="Seal kit")
    assert part.aliases == []
    assert part.qty_on_hand is None
    assert part.bin is None
    assert part.lead_time_days is None
    assert part.stock_as_of is None
    assert part.unit_cost_sgd is None

    incident = IncidentProps(
        id="INC-2026-042",
        date="2026-03-04",
        shift="B",
        downtime_min=90,
        symptom_text="Press stopped mid-cycle",
    )
    assert incident.date == date(2026, 3, 4)
    assert incident.technician is None
    assert incident.resolution_text is None

    # An incident known only from the maintenance export has no shift, and some have
    # no downtime. They are real incidents.
    sparse = IncidentProps(
        id="INC-2026-043", date="2026-03-05", symptom_text="Press stopped mid-cycle"
    )
    assert sparse.shift is None
    assert sparse.downtime_min is None

    document = DocumentProps(
        id="DOC-0a1b2c3d", path="logs/a.log", doc_type="log", sha256="0" * 64
    )
    assert document.doc_type == "log"


def test_default_alias_lists_are_not_shared_between_instances():
    first = AssetProps(id="CNC-01", name="a", machine_type="b", vendor="c", model="d")
    first.aliases.append("lathe one")
    second = AssetProps(id="CNC-02", name="a", machine_type="b", vendor="c", model="d")
    assert second.aliases == []


def test_literal_fields_reject_other_values():
    with pytest.raises(ValidationError):
        AlarmCodeProps(code="E-217", text="x", severity="loud")
    with pytest.raises(ValidationError):
        IncidentProps(
            id="INC-2026-042",
            date="2026-03-04",
            shift="D",
            downtime_min=1,
            symptom_text="x",
        )
    with pytest.raises(ValidationError):
        DocumentProps(id="DOC-0a1b2c3d", path="a", doc_type="video", sha256="0")


def test_required_fields_are_enforced():
    with pytest.raises(ValidationError):
        LineProps(id="LINE-01")
    with pytest.raises(ValidationError):
        PartProps(part_no="SEAL-KIT-50")


# --- Relation property models ------------------------------------------------


def test_relation_property_models():
    assert RELATIONS["HAS_MACHINE"].props is None
    assert RELATIONS["RAISED"].props is RaisedProps
    assert RELATIONS["INDICATES"].props is IndicatesProps
    assert RELATIONS["REQUIRES"].props is RequiresProps
    assert RELATIONS["USED"].props is UsedProps
    for key, (_, _, _, properties) in EXPECTED_RELATIONS.items():
        model = RELATIONS[key].props
        if properties:
            assert list(model.model_fields) == [name for name, _, _ in properties]
        else:
            assert model is None


def test_raised_props_take_a_count_and_two_timestamps():
    props = RaisedProps(
        count=3, first_seen="2026-03-01T08:00:00", last_seen="2026-03-04T17:30:00"
    )
    assert props.first_seen == datetime(2026, 3, 1, 8, 0)
    assert props.last_seen == datetime(2026, 3, 4, 17, 30)


@pytest.mark.parametrize("likelihood", [0, 0.0, 0.5, 1, 1.0])
def test_likelihood_inside_zero_to_one_is_accepted(likelihood):
    assert IndicatesProps(likelihood=likelihood, support=2).likelihood == likelihood


@pytest.mark.parametrize("likelihood", [-0.01, 1.01, 5])
def test_likelihood_outside_zero_to_one_is_rejected(likelihood):
    with pytest.raises(ValidationError):
        IndicatesProps(likelihood=likelihood, support=2)


def test_quantity_relations_need_qty():
    assert RequiresProps(qty=2).qty == 2
    assert UsedProps(qty=1).qty == 1
    with pytest.raises(ValidationError):
        RequiresProps()
    with pytest.raises(ValidationError):
        UsedProps()


# --- Evidence -----------------------------------------------------------------


def evidence(**overrides):
    values = {
        "fact_kind": "node",
        "fact_id": "CNC-01",
        "doc_id": "DOC-0a1b2c3d",
        "chunk_id": "chunk-1",
        "page": 3,
        "quote": "Lathe 1 stopped",
        "extractor": "test",
        "confidence": 0.9,
    }
    return values | overrides


def test_evidence_has_exactly_the_fields_in_the_ontology_spec():
    assert list(Evidence.model_fields) == [
        "fact_kind",
        "fact_id",
        "doc_id",
        "chunk_id",
        "page",
        "quote",
        "extractor",
        "confidence",
    ]


def test_evidence_accepts_a_node_or_edge_fact():
    assert Evidence(**evidence()).fact_kind == "node"
    assert Evidence(**evidence(fact_kind="edge")).fact_kind == "edge"
    with pytest.raises(ValidationError):
        Evidence(**evidence(fact_kind="path"))


def test_evidence_chunk_and_page_may_be_none_but_must_be_given():
    item = Evidence(**evidence(chunk_id=None, page=None))
    assert item.chunk_id is None
    assert item.page is None
    for missing in ("chunk_id", "page"):
        values = evidence()
        del values[missing]
        with pytest.raises(ValidationError):
            Evidence(**values)


@pytest.mark.parametrize("confidence", [0, 0.0, 0.5, 1, 1.0])
def test_evidence_confidence_inside_zero_to_one(confidence):
    assert Evidence(**evidence(confidence=confidence)).confidence == confidence


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_evidence_confidence_outside_zero_to_one(confidence):
    with pytest.raises(ValidationError):
        Evidence(**evidence(confidence=confidence))


# --- Export ---------------------------------------------------------------------


def test_export_round_trips_and_lists_everything(tmp_path):
    path = export_json(tmp_path / "ontology.json")
    assert path == tmp_path / "ontology.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == ["families", "node_types", "relations"]
    assert len(data["node_types"]) == 9
    assert len(data["relations"]) == 12
    assert len(data["families"]) == 5


def test_export_carries_the_families(tmp_path):
    data = json.loads(export_json(tmp_path / "o.json").read_text(encoding="utf-8"))
    assert data["families"] == {
        key: {"label": label, "description": description}
        for key, (label, description) in EXPECTED_FAMILIES.items()
    }


def test_export_carries_the_node_types(tmp_path):
    data = json.loads(export_json(tmp_path / "o.json").read_text(encoding="utf-8"))
    node_types = data["node_types"]
    assert list(node_types) == list(EXPECTED_NODE_TYPES)
    for key, expected in EXPECTED_NODE_TYPES.items():
        label, glyph, family, description, pattern = expected
        assert node_types[key] == {
            "label": label,
            "glyph": glyph,
            "family": family,
            "description": description,
            "id_pattern": pattern,
            "properties": as_dicts(EXPECTED_NODE_PROPERTIES[key]),
        }


def test_export_carries_the_relations(tmp_path):
    data = json.loads(export_json(tmp_path / "o.json").read_text(encoding="utf-8"))
    relations = data["relations"]
    assert list(relations) == list(EXPECTED_RELATIONS)
    for key, (label, src, dst, properties) in EXPECTED_RELATIONS.items():
        assert relations[key] == {
            "label": label,
            "from": src,
            "to": dst,
            "properties": as_dicts(properties),
        }


def test_export_format_is_stable(tmp_path):
    first = export_json(tmp_path / "first.json").read_bytes()
    second = export_json(tmp_path / "second.json").read_bytes()
    assert first == second
    text = first.decode("utf-8")
    assert text.startswith('{\n  "families": {\n    "where": {\n      "label"')
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")
    assert json.dumps(json.loads(text), indent=2, ensure_ascii=False) + "\n" == text


def test_the_committed_export_is_current(tmp_path):
    """Run `make ontology` after changing app.ontology."""
    committed = ROOT / "config/ontology.json"
    assert committed.is_file(), "config/ontology.json is missing, run make ontology"
    fresh = export_json(tmp_path / "ontology.json")
    assert committed.read_bytes() == fresh.read_bytes(), (
        "config/ontology.json is stale, run make ontology"
    )


# --- Command line ----------------------------------------------------------------


def test_cli_export_writes_the_file_and_names_it(monkeypatch, capsys, tmp_path):
    target = tmp_path / "ontology.json"
    real_export = ontology.export_json
    monkeypatch.setattr(ontology, "export_json", lambda: real_export(target))
    assert ontology.main(["export"]) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["node_types"]
    assert capsys.readouterr().out.startswith("wrote ")


def test_cli_needs_a_command(capsys):
    with pytest.raises(SystemExit) as raised:
        ontology.main([])
    assert raised.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_module_entry_point_runs_the_cli(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "app.ontology"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
