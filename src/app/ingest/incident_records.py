"""The records used to decide whether two incident reports are the same breakdown.

A Member is one record: either a group of drafts from one document or an incident the
graph already holds. This file builds the members, writes a short summary of each for
the decider and lists the pairs worth comparing. The matching itself is in incidents.py.
"""

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from app.ingest import questions
from app.ingest.types import (
    CandidateNode,
    EvidenceDraft,
    ResolvedEdge,
    ResolvedNode,
    ReviewItem,
    is_missing,
)

WORK_ORDER = re.compile(r"WO-\d+")
# Properties dropped from an incident. The start time is only used for ordering.
DROPPED_PROPS = ("start",)
SAME_DAY_WINDOW = dt.timedelta(days=1)


@dataclass
class DraftEdge:
    """A link from an incident draft to another thing in the graph."""

    rel: str
    dst: str
    props: dict[str, Any]
    evidence: list[EvidenceDraft]


@dataclass
class RankedEdge:
    """A link with its source's rank and position, so equal links merge in priority
    order."""

    src: str
    rel: str
    dst: str
    props: dict[str, Any]
    evidence: list[EvidenceDraft]
    rank: int
    seq: int


@dataclass
class DraftGroup:
    """The drafts one document gives for one incident, with their links. Several
    drafts mean a repeated row."""

    doc_path: str
    doc_type: str
    key: str
    rank: int
    seq: int
    nodes: list[CandidateNode]
    edges: list[DraftEdge] = field(default_factory=list)
    machine_text: str | None = None


@dataclass
class Existing:
    """An incident the graph already holds, with its links."""

    node: ResolvedNode
    edges: list[ResolvedEdge]


@dataclass
class Member:
    """One record to compare: a group of drafts or an existing incident."""

    rank: int
    seq: int
    doc_path: str
    docs: tuple[str, ...]
    doc_type: str
    key: str
    existing_id: str | None
    label: str
    props: dict[str, Any]
    evidence: list[EvidenceDraft]
    edges: list[DraftEdge]
    machine_text: str | None
    date: dt.date | None

    @property
    def ident(self) -> tuple[str, str]:
        return (self.doc_path, self.key)

    @property
    def work_order(self) -> str | None:
        if self.existing_id is None and WORK_ORDER.fullmatch(self.key):
            return self.key
        return None

    @property
    def order(self) -> tuple[int, int]:
        return (self.rank, self.seq)


def as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def union(items: list[EvidenceDraft], more: list[EvidenceDraft]) -> None:
    for item in more:
        if item not in items:
            items.append(item)


def first_present(prop_dicts: list[dict[str, Any]]) -> dict[str, Any]:
    """The first real value of each property. A property with no value is left out."""
    merged: dict[str, Any] = {}
    for props in prop_dicts:
        for name, value in props.items():
            if name not in merged and not is_missing(value):
                merged[name] = value
    return merged


def member_of_group(group: DraftGroup) -> tuple[Member, ReviewItem | None]:
    """The record for a group of drafts. Repeated rows merge, and a differing date or
    downtime is flagged as a conflict."""
    props = first_present([node.props for node in group.nodes])
    for name in DROPPED_PROPS:
        props.pop(name, None)
    differences: dict[str, list[Any]] = {}
    for name in ("date", "downtime_min"):
        values: list[Any] = []
        for node in group.nodes:
            value = node.props.get(name)
            if not is_missing(value) and value not in values:
                values.append(value)
        if len(values) > 1:
            differences[name] = values
    conflict = None
    if differences:
        conflict = ReviewItem(
            kind="conflict",
            doc_paths=[group.doc_path],
            payload={
                "reason": "a repeated record differs",
                "key": group.key,
                "differences": differences,
            },
        )
    evidence: list[EvidenceDraft] = []
    for node in group.nodes:
        union(evidence, node.evidence)
    edges: dict[tuple[str, str], DraftEdge] = {}
    for edge in group.edges:
        found = edges.get((edge.rel, edge.dst))
        if found is None:
            edges[edge.rel, edge.dst] = DraftEdge(
                edge.rel, edge.dst, edge.props, list(edge.evidence)
            )
        else:
            union(found.evidence, edge.evidence)
    label = next(
        (
            n.label
            for n in group.nodes
            if not is_missing(n.label) and n.label != group.key
        ),
        group.key,
    )
    member = Member(
        rank=group.rank,
        seq=group.seq,
        doc_path=group.doc_path,
        docs=(group.doc_path,),
        doc_type=group.doc_type,
        key=group.key,
        existing_id=None,
        label=label,
        props=props,
        evidence=evidence,
        edges=list(edges.values()),
        machine_text=group.machine_text,
        date=as_date(props.get("date")),
    )
    return member, conflict


def member_of_existing(existing: Existing, seq: int) -> Member:
    node = existing.node
    props = {k: v for k, v in node.props.items() if k not in DROPPED_PROPS}
    docs = tuple(node.sources) or ("",)
    return Member(
        rank=0,
        seq=seq,
        doc_path=docs[0],
        docs=docs,
        doc_type="existing incident",
        key=node.id,
        existing_id=node.id,
        label=node.label,
        props=props,
        evidence=list(node.evidence),
        edges=[
            DraftEdge(e.rel, e.dst, e.props, list(e.evidence)) for e in existing.edges
        ],
        machine_text=None,
        date=as_date(props.get("date")),
    )


def summary(member: Member) -> dict[str, Any]:
    """The record as the decider sees it."""
    machine = next((e.dst for e in member.edges if e.rel == "OCCURRED_ON"), None)
    alarms = [e.dst for e in member.edges if e.rel == "TRIGGERED_BY"]
    parts = [
        {"part": e.dst, "qty": e.props.get("qty")}
        for e in member.edges
        if e.rel == "USED"
    ]
    props = member.props
    return questions.incident_summary(
        source_type=member.doc_type,
        date=props.get("date"),
        shift=props.get("shift"),
        machine=machine or member.machine_text,
        alarm=", ".join(alarms) or None,
        symptom=props.get("symptom_text"),
        action=props.get("resolution_text"),
        parts=parts,
        technician=props.get("technician"),
        downtime_min=props.get("downtime_min"),
        quote=member.evidence[0].quote if member.evidence else None,
    )


def candidate_pairs(members: list[Member]) -> list[tuple[Member, Member]]:
    """Pairs of records from different documents that are at most a day apart. Two
    incidents already in the graph are never paired."""
    dated = sorted(
        (m for m in members if m.date is not None),
        key=lambda m: (m.date, m.order, m.doc_path, m.key),
    )
    pairs = []
    for index, first in enumerate(dated):
        for second in dated[index + 1 :]:
            if second.date - first.date > SAME_DAY_WINDOW:
                break
            if set(first.docs) & set(second.docs):
                continue
            if first.existing_id and second.existing_id:
                continue
            pairs.append((first, second))
    return pairs
