"""Combines what several sources say about one thing into a single node.

merge_props merges the properties from each source in priority order. build_node
uses it to make the final node, and also picks its label, evidence and sources.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.ingest.types import EvidenceDraft, ResolvedNode, is_missing

# The property that holds the id for each node type.
IDENTITY_PROP = {
    "Line": "id",
    "Asset": "id",
    "Component": "id",
    "AlarmCode": "code",
    "FailureMode": "id",
    "Procedure": "id",
    "Part": "part_no",
    "Incident": "id",
}
LIST_UNION = frozenset({"aliases"})
STOCK_PROPS = ("qty_on_hand", "stock_as_of")


def _day(value: Any) -> dt.date:
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    return dt.date.min


def _union_text(lists: list[list[Any]]) -> list[Any]:
    """Join lists in first-seen order, dropping repeats that differ only in case."""
    seen: set[str] = set()
    merged: list[Any] = []
    for values in lists:
        for value in values:
            key = str(value).casefold()
            if key not in seen:
                seen.add(key)
                merged.append(value)
    return merged


def merge_props(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge property dicts that are in priority order.

    The first value that is present wins. Aliases are joined. The stock count and its
    date always come together from the newest count, so they never mix sources.
    """
    names: dict[str, None] = {}
    for props in items:
        names.update(dict.fromkeys(props))
    counted = [p for p in items if not is_missing(p.get("qty_on_hand"))]
    winner = None
    for props in counted:
        newer = winner is None or _day(props.get("stock_as_of")) > _day(
            winner.get("stock_as_of")
        )
        if newer:
            winner = props
    merged: dict[str, Any] = {}
    for name in names:
        if name in LIST_UNION:
            values = _union_text(
                [p[name] for p in items if isinstance(p.get(name), list)]
            )
            if values:
                merged[name] = values
            continue
        pool = [winner] if winner is not None and name in STOCK_PROPS else items
        value = next((p[name] for p in pool if not is_missing(p.get(name))), None)
        if value is not None:
            merged[name] = value
    return merged


@dataclass
class Contribution:
    """What one source adds to a node."""

    rank: int
    seq: int
    docs: list[str]
    label: str
    props: dict[str, Any]
    evidence: list[EvidenceDraft]

    @property
    def order(self) -> tuple[int, int]:
        return (self.rank, self.seq)


def build_node(
    node_type: str, node_id: str, contributions: list[Contribution]
) -> ResolvedNode:
    """Build the final node from its contributions, which are in priority order."""
    props = merge_props([c.props for c in contributions])
    identity = IDENTITY_PROP.get(node_type)
    if identity:
        props = {identity: node_id, **{k: v for k, v in props.items() if k != identity}}
    label = next(
        (
            c.label
            for c in contributions
            if not is_missing(c.label) and c.label != node_id
        ),
        node_id,
    )
    evidence: list[EvidenceDraft] = []
    sources: list[str] = []
    for item in contributions:
        for found in item.evidence:
            if found not in evidence:
                evidence.append(found)
        for doc in item.docs:
            if doc and doc not in sources:
                sources.append(doc)
    return ResolvedNode(
        id=node_id,
        type=node_type,
        label=label,
        props=props,
        evidence=evidence,
        sources=sources,
    )
