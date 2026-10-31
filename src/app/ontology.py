"""The single source for the kinds of facts the graph holds.

This file lists the node types, the links between them, their labels, colour families
and ID formats. Other modules import these tables and never repeat them.
`export_json` writes `config/ontology.json` for the web app.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Field

from app.config import ROOT

# --- Property models: one per node type ------------------------------------


class LineProps(BaseModel):
    id: str
    name: str


class AssetProps(BaseModel):
    id: str
    name: str
    machine_type: str
    vendor: str
    model: str
    aliases: list[str] = []


class ComponentProps(BaseModel):
    id: str
    name: str
    asset_id: str


class AlarmCodeProps(BaseModel):
    code: str
    text: str
    severity: Literal["info", "warning", "critical"]
    aliases: list[str] = []


class FailureModeProps(BaseModel):
    id: str
    name: str
    mechanism: str


class ProcedureProps(BaseModel):
    id: str
    title: str
    steps: list[str]
    requires_loto: bool
    est_minutes: int
    manual_ref: str | None = None


class PartProps(BaseModel):
    part_no: str
    description: str
    aliases: list[str] = []
    qty_on_hand: int | None = None
    bin: str | None = None
    lead_time_days: int | None = None
    stock_as_of: dt.date | None = None
    unit_cost_sgd: float | None = None


class IncidentProps(BaseModel):
    id: str
    date: dt.date
    shift: Literal["A", "B", "C"] | None = None
    downtime_min: int | None = None
    symptom_text: str
    technician: str | None = None
    resolution_text: str | None = None


class DocumentProps(BaseModel):
    id: str
    path: str
    doc_type: Literal[
        "log", "handover", "maintenance_note", "inventory", "manual", "procedure"
    ]
    sha256: str


# --- Property models: relations that carry properties ----------------------


class RaisedProps(BaseModel):
    count: int
    first_seen: dt.datetime
    last_seen: dt.datetime


class IndicatesProps(BaseModel):
    likelihood: float = Field(ge=0, le=1)
    support: int


class RequiresProps(BaseModel):
    qty: int


class UsedProps(BaseModel):
    qty: int


# --- Evidence -----------------------------------------------------------------


class Evidence(BaseModel):
    """Where a node or edge came from: the document, the quote and the extractor."""

    fact_kind: Literal["node", "edge"]
    fact_id: str
    doc_id: str
    chunk_id: str | None
    page: int | None
    quote: str
    extractor: str
    confidence: float = Field(ge=0, le=1)


# --- Tables -------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    key: str
    label: str
    description: str


@dataclass(frozen=True)
class NodeType:
    key: str
    label: str
    glyph: str
    family: str
    description: str
    id_pattern: str
    props: type[BaseModel]


@dataclass(frozen=True)
class Relation:
    key: str
    label: str
    src: str
    dst: str
    props: type[BaseModel] | None = None


FAMILIES: dict[str, Family] = {
    family.key: family
    for family in (
        Family(
            "where",
            "Where",
            "Where the problem lives: the line, its machines and their components.",
        ),
        Family(
            "problem",
            "Problem",
            "What went wrong: alarms and the root causes behind them.",
        ),
        Family(
            "fix",
            "Fix",
            "How to fix it: procedures and the spare parts they need.",
        ),
        Family("history", "History", "What happened before: past breakdowns."),
        Family("source", "Source", "Where each fact came from."),
    )
}

NODE_TYPES: dict[str, NodeType] = {
    node_type.key: node_type
    for node_type in (
        NodeType(
            key="Line",
            label="Production line",
            glyph="factory",
            family="where",
            description="A production line in the plant.",
            id_pattern=r"^LINE-\d{2}$",
            props=LineProps,
        ),
        NodeType(
            key="Asset",
            label="Machine",
            glyph="cog",
            family="where",
            description="A machine on the line.",
            id_pattern=r"^[A-Z]{2,3}-\d{2}$",
            props=AssetProps,
        ),
        NodeType(
            key="Component",
            label="Component",
            glyph="bolt",
            family="where",
            description=(
                "A part of a machine that can fail, such as a spindle or a pump."
            ),
            id_pattern=r"^[A-Z]{2,3}-\d{2}-[A-Z0-9]+$",
            props=ComponentProps,
        ),
        NodeType(
            key="AlarmCode",
            label="Alarm",
            glyph="siren",
            family="problem",
            description="A code the machine shows when something is wrong.",
            id_pattern=r"^[A-Z]{1,3}-\d{3}$",
            props=AlarmCodeProps,
        ),
        NodeType(
            key="FailureMode",
            label="Root cause",
            glyph="unlink",
            family="problem",
            description="The underlying reason a machine stops.",
            id_pattern=r"^FM-[A-Z0-9-]+$",
            props=FailureModeProps,
        ),
        NodeType(
            key="Procedure",
            label="Fix",
            glyph="wrench",
            family="fix",
            description="Steps that resolve a root cause.",
            id_pattern=r"^PROC-[A-Z]{2,3}-\d{2}$",
            props=ProcedureProps,
        ),
        NodeType(
            key="Part",
            label="Spare part",
            glyph="package",
            family="fix",
            description="A spare part kept in stock.",
            id_pattern=r"^[A-Z0-9]+(-[A-Z0-9]+)+$",
            props=PartProps,
        ),
        NodeType(
            key="Incident",
            label="Past breakdown",
            glyph="history",
            family="history",
            description="A breakdown that happened before, and how it was fixed.",
            id_pattern=r"^INC-\d{4}-\d{3}$",
            props=IncidentProps,
        ),
        NodeType(
            key="Document",
            label="Source",
            glyph="file-text",
            family="source",
            description="A file from the shared folder that backs the facts.",
            id_pattern=r"^DOC-[0-9a-f]{8}$",
            props=DocumentProps,
        ),
    )
}

RELATIONS: dict[str, Relation] = {
    relation.key: relation
    for relation in (
        Relation("HAS_MACHINE", "has machine", "Line", "Asset"),
        Relation("HAS_COMPONENT", "is built from", "Asset", "Component"),
        Relation("RAISED", "raised", "Asset", "AlarmCode", RaisedProps),
        Relation(
            "INDICATES", "usually means", "AlarmCode", "FailureMode", IndicatesProps
        ),
        Relation("AFFECTS", "damages", "FailureMode", "Component"),
        Relation("RESOLVED_BY", "fixed by", "FailureMode", "Procedure"),
        Relation("REQUIRES", "needs", "Procedure", "Part", RequiresProps),
        Relation("FITS", "fits", "Part", "Component"),
        Relation("OCCURRED_ON", "happened on", "Incident", "Asset"),
        Relation("TRIGGERED_BY", "started with", "Incident", "AlarmCode"),
        Relation("ROOT_CAUSE", "was caused by", "Incident", "FailureMode"),
        Relation("USED", "used", "Incident", "Part", UsedProps),
    )
}


# --- Lookups ------------------------------------------------------------------


def is_allowed(src_type: str, rel: str, dst_type: str) -> bool:
    """Say whether the relation exists and runs from the first type to the second."""
    relation = RELATIONS.get(rel)
    return relation is not None and (relation.src, relation.dst) == (src_type, dst_type)


def validate_id(node_type: str, value: str) -> bool:
    """Say whether a value is a well-formed ID for the node type."""
    pattern = NODE_TYPES[node_type].id_pattern
    return re.fullmatch(pattern, value, re.ASCII) is not None


def props_model(node_type: str) -> type[BaseModel]:
    """Return the list of properties a node type holds."""
    return NODE_TYPES[node_type].props


# --- Export -------------------------------------------------------------------


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is Literal:
        values = ", ".join(repr(value) for value in get_args(annotation))
        return f"Literal[{values}]"
    if origin in (Union, types.UnionType):
        return " | ".join(_type_name(arg) for arg in get_args(annotation))
    if origin is not None:
        arguments = ", ".join(_type_name(arg) for arg in get_args(annotation))
        return f"{origin.__name__}[{arguments}]"
    if annotation is type(None):
        return "None"
    return annotation.__name__


def _property_list(model: type[BaseModel] | None) -> list[dict[str, Any]]:
    if model is None:
        return []
    return [
        {
            "name": name,
            "type": _type_name(field.annotation),
            "required": field.is_required(),
        }
        for name, field in model.model_fields.items()
    ]


def export_json(path: Path = ROOT / "config/ontology.json") -> Path:
    """Write the families, node types and relations to a JSON file."""
    document = {
        "families": {
            key: {"label": family.label, "description": family.description}
            for key, family in FAMILIES.items()
        },
        "node_types": {
            key: {
                "label": node_type.label,
                "glyph": node_type.glyph,
                "family": node_type.family,
                "description": node_type.description,
                "id_pattern": node_type.id_pattern,
                "properties": _property_list(node_type.props),
            }
            for key, node_type in NODE_TYPES.items()
        },
        "relations": {
            key: {
                "label": relation.label,
                "from": relation.src,
                "to": relation.dst,
                "properties": _property_list(relation.props),
            }
            for key, relation in RELATIONS.items()
        },
    }
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# --- Command line ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ontology")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="write config/ontology.json")
    parser.parse_args(argv)

    path = export_json()
    print(f"wrote {os.path.relpath(path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
