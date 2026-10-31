"""Checks the resolved nodes and links against the data rules before they are loaded.

validate drops any item that breaks a rule and reports why. Items that are only
suspicious, such as an incident with no shift, are kept with a warning. Dropping a
node also drops its links, and each gets its own flag. Every flag has a plain
sentence of detail, so the review queue and the Pipeline view can show it directly.
"""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.ingest.merge_props import IDENTITY_PROP
from app.ingest.types import (
    MAX_QUOTE,
    EvidenceDraft,
    Resolution,
    ResolvedEdge,
    ResolvedNode,
)
from app.ontology import NODE_TYPES, RELATIONS, is_allowed, props_model, validate_id

# The longest downtime one incident can have, in minutes (a day).
MAX_DOWNTIME_MIN = 1440


class Flag(BaseModel):
    """One finding about a node or an edge."""

    rule: str
    severity: Literal["error", "warning"]
    subject: str
    detail: str


class Validated(BaseModel):
    """The nodes and edges that passed, with every flag that was raised."""

    nodes: list[ResolvedNode] = Field(default_factory=list)
    edges: list[ResolvedEdge] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)


def edge_subject(edge: ResolvedEdge) -> str:
    """Return the id that names a link in a flag."""
    return f"{edge.src}|{edge.rel}|{edge.dst}"


def _error(rule: str, subject: str, detail: str) -> Flag:
    return Flag(rule=rule, severity="error", subject=subject, detail=detail)


def _warning(rule: str, subject: str, detail: str) -> Flag:
    return Flag(rule=rule, severity="warning", subject=subject, detail=detail)


def _evidence_flag(subject: str, evidence: list[EvidenceDraft]) -> Flag | None:
    """Return an error if evidence is missing or has an empty or too long quote."""
    if not evidence:
        return _error("no_evidence", subject, "the item has no evidence")
    for item in evidence:
        if not item.quote.strip():
            return _error("bad_quote", subject, "an evidence quote is empty")
        if len(item.quote) > MAX_QUOTE:
            return _error(
                "bad_quote",
                subject,
                f"an evidence quote is {len(item.quote)} characters; "
                f"the limit is {MAX_QUOTE}",
            )
    return None


def _node_flag(node: ResolvedNode) -> Flag | None:
    """Return the first error that drops the node, or None if it is fine."""
    if node.type not in NODE_TYPES:
        return _error("unknown_type", node.id, f"{node.type!r} is not a node type")
    if not validate_id(node.type, node.id):
        return _error("bad_id", node.id, f"{node.id!r} is not a valid {node.type} id")
    try:
        props_model(node.type).model_validate(node.props)
    except ValidationError as error:
        return _error(
            "bad_props", node.id, f"props do not fit {node.type}: {_brief(error)}"
        )
    identity = IDENTITY_PROP.get(node.type, "id")
    if node.props.get(identity) != node.id:
        return _error(
            "identity_mismatch",
            node.id,
            f"{identity} is {node.props.get(identity)!r}, not the node id",
        )
    return _evidence_flag(node.id, node.evidence)


def _brief(error: ValidationError) -> str:
    """Return the first problem of a validation error as a short sentence."""
    first = error.errors(include_url=False, include_input=False)[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}" if where else first["msg"]


def _edge_flag(
    edge: ResolvedEdge, known: dict[str, ResolvedNode], dropped: set[str]
) -> Flag | None:
    """Return the first error that drops the link, or None if it is fine."""
    subject = edge_subject(edge)
    for end in (edge.src, edge.dst):
        if end in dropped:
            return _error("dropped_endpoint", subject, f"the node {end!r} was dropped")
        if end not in known:
            return _error(
                "missing_endpoint", subject, f"the node {end!r} is not in the graph"
            )
    src, dst = known[edge.src], known[edge.dst]
    if not is_allowed(src.type, edge.rel, dst.type):
        return _error(
            "relation_not_allowed",
            subject,
            f"{edge.rel} cannot run from {src.type} to {dst.type}",
        )
    model = RELATIONS[edge.rel].props
    if model is not None:
        try:
            model.model_validate(edge.props)
        except ValidationError as error:
            return _error(
                "bad_edge_props",
                subject,
                f"props do not fit {edge.rel}: {_brief(error)}",
            )
    return _evidence_flag(subject, edge.evidence)


def _incident_warnings(node: ResolvedNode, has_machine: bool) -> list[Flag]:
    flags = []
    if not has_machine:
        flags.append(
            _warning("incident_no_machine", node.id, "no OCCURRED_ON edge to a machine")
        )
    parsed = props_model("Incident").model_validate(node.props)
    if parsed.shift is None:
        flags.append(
            _warning("incident_no_shift", node.id, "the incident has no shift")
        )
    if parsed.downtime_min is None:
        flags.append(
            _warning("incident_no_downtime", node.id, "the incident has no downtime")
        )
    elif not 0 <= parsed.downtime_min <= MAX_DOWNTIME_MIN:
        flags.append(
            _warning(
                "downtime_range",
                node.id,
                f"downtime of {parsed.downtime_min} min is outside 0 to "
                f"{MAX_DOWNTIME_MIN}",
            )
        )
    return flags


def validate(resolution: Resolution) -> Validated:
    """Check the resolution against the data rules.

    Returns the nodes and links that passed, plus flags for everything else. The
    input is not changed.
    """
    flags: list[Flag] = []
    nodes: list[ResolvedNode] = []
    dropped: set[str] = set()
    for node in resolution.nodes:
        flag = _node_flag(node)
        if flag is None:
            nodes.append(node)
        else:
            dropped.add(node.id)
            flags.append(flag)
    known = {node.id: node for node in nodes}

    edges: list[ResolvedEdge] = []
    for edge in resolution.edges:
        flag = _edge_flag(edge, known, dropped)
        if flag is None:
            edges.append(edge)
        else:
            flags.append(flag)

    linked = {end for edge in edges for end in (edge.src, edge.dst)}
    machines = {edge.src for edge in edges if edge.rel == "OCCURRED_ON"}
    for node in nodes:
        if node.id not in linked:
            flags.append(_warning("no_edges", node.id, "the node has no edges"))
        if node.type == "Incident":
            flags += _incident_warnings(node, node.id in machines)
    return Validated(nodes=nodes, edges=edges, flags=flags)
