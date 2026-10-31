"""Groups reports of the same breakdown into single incidents.

The maintenance export, the handovers and the notes each describe breakdowns in their
own words, so one breakdown can show up in several places. This file finds which
reports belong together. Repeated rows in one document merge on their own. Reports
from different documents on nearby days go to the decider, which says whether they
are the same breakdown. Strong matches are joined, unsure ones go to a person, and a
join is refused if it would mix clearly different incidents.

cluster is the main function. It returns the final incidents with their links, a log
of every decision and the items for a person to review. Incidents already in the
graph take part and keep their ids.
"""

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.decide import Decider
from app.ingest import questions
from app.ingest.incident_records import (
    DraftGroup,
    Existing,
    Member,
    RankedEdge,
    as_date,
    candidate_pairs,
    first_present,
    member_of_existing,
    member_of_group,
    summary,
    union,
)
from app.ingest.types import (
    EvidenceDraft,
    MergeDecision,
    ResolvedNode,
    ReviewItem,
    is_missing,
)

INCIDENT_ID = re.compile(r"INC-(\d{4})-(\d+)")


@dataclass
class Clustered:
    """The outcome of grouping: the incidents, their links, the decisions and the
    items for review."""

    nodes: list[ResolvedNode] = field(default_factory=list)
    edges: list[RankedEdge] = field(default_factory=list)
    merges: list[MergeDecision] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)


CANNOT_LINK = "cannot link"
CONTRADICTED = "contradicted by a direct answer"


@dataclass
class _Refusal:
    """Why two groups that looked alike could not be joined. A contradicting pair is a
    pair, one from each group, that the decider judged to be different."""

    reason: str
    contradicted_by: "_Pair | None" = None


@dataclass
class _Pair:
    first: Member
    second: Member
    verdict: questions.Verdict
    refusal: _Refusal | None = None

    @property
    def key(self) -> tuple[tuple[str, str], tuple[str, str]]:
        """The two records' identifiers in sorted order."""
        low, high = sorted((self.first.ident, self.second.ident))
        return (low, high)


class _Clusters:
    """Tracks which records have been joined into the same group, and refuses a join
    that would break a rule."""

    def __init__(self, members: list[Member], pairs: list[_Pair]) -> None:
        self.parent = {m.ident: m.ident for m in members}
        self.members = {m.ident: [m.ident] for m in members}
        self.docs = {m.ident: set(m.docs) for m in members}
        self.orders = {m.ident: {m.work_order} - {None} for m in members}
        self.existing = {m.ident: int(m.existing_id is not None) for m in members}
        # A pair with no answer, because of a refusal or an error, tells us nothing.
        self.answered = {p.key: p for p in pairs if p.verdict.p is not None}

    def find(self, ident: tuple[str, str]) -> tuple[str, str]:
        while self.parent[ident] != ident:
            self.parent[ident] = self.parent[self.parent[ident]]
            ident = self.parent[ident]
        return ident

    def contradiction(self, ra: tuple[str, str], rb: tuple[str, str]) -> _Pair | None:
        """A pair across the two groups that the decider judged to be different. The
        least similar pair is returned if there are several."""
        found = [
            pair
            for pair in self.answered.values()
            if {self.find(pair.first.ident), self.find(pair.second.ident)} == {ra, rb}
            and questions.merge_band(pair.verdict.p, "incident") != "merge"
        ]
        return min(found, key=lambda p: (p.verdict.p, p.key), default=None)

    def try_union(self, a: Member, b: Member) -> _Refusal | None:
        """Join the groups of the two records unless a rule or a direct answer forbids
        it. Returns the reason for refusing, or None if joined."""
        ra, rb = self.find(a.ident), self.find(b.ident)
        if ra == rb:
            return None
        if (
            self.docs[ra] & self.docs[rb]
            or len(self.orders[ra] | self.orders[rb]) > 1
            or self.existing[ra] + self.existing[rb] > 1
        ):
            return _Refusal(CANNOT_LINK)
        contradicting = self.contradiction(ra, rb)
        if contradicting is not None:
            return _Refusal(CONTRADICTED, contradicting)
        self.parent[rb] = ra
        self.members[ra] += self.members.pop(rb)
        self.docs[ra] |= self.docs[rb]
        self.orders[ra] |= self.orders[rb]
        self.existing[ra] += self.existing[rb]
        return None


def _pair_review(
    pair: _Pair,
    reason: str,
    p: float | None,
    contradicted_by: _Pair | None = None,
) -> ReviewItem:
    docs = list(dict.fromkeys([pair.first.doc_path, pair.second.doc_path]))
    payload: dict[str, Any] = {
        "reason": reason,
        "records": [list(pair.first.ident), list(pair.second.ident)],
    }
    if p is not None:
        payload["p"] = p
    if contradicted_by is not None:
        payload["contradicted_by"] = {
            "records": [list(i) for i in contradicted_by.key],
            "p": contradicted_by.verdict.p,
        }
    return ReviewItem(kind="merge", doc_paths=docs, payload=payload, confidence=p)


def _decide_pairs(pairs: list[_Pair], clusters: _Clusters, result: Clustered) -> None:
    """Join the strong matches, strongest first, then log every pair and queue the
    unsure ones for a person."""
    mergeable = sorted(
        (
            p
            for p in pairs
            if p.verdict.p is not None
            and questions.merge_band(p.verdict.p, "incident") == "merge"
        ),
        key=lambda p: (-p.verdict.p, p.first.ident, p.second.ident),
    )
    for pair in mergeable:
        pair.refusal = clusters.try_union(pair.first, pair.second)
    for pair in pairs:
        p, verdict = pair.verdict.p, pair.verdict
        if p is None:
            decision = "unresolved"
            reason = verdict.reason or questions.NO_DECISION
            result.review.append(_pair_review(pair, reason, None))
        else:
            band = questions.merge_band(p, "incident")
            if band == "merge" and pair.refusal is not None:
                decision = "queue"
                refusal = pair.refusal
                result.review.append(
                    _pair_review(pair, refusal.reason, p, refusal.contradicted_by)
                )
            elif band == "merge":
                decision = "merge"
            elif band == "queue":
                decision = "queue"
                result.review.append(_pair_review(pair, "unsure match", p))
            else:
                decision = "new"
        result.merges.append(
            MergeDecision(
                doc_path=pair.first.doc_path,
                type="Incident",
                mention=pair.first.key,
                candidate=f"{pair.second.doc_path}::{pair.second.key}",
                p=p,
                decision=decision,
                backend=verdict.backend,
            )
        )


def _build(
    members: list[Member], incident_id: str
) -> tuple[ResolvedNode, list[RankedEdge], ReviewItem | None]:
    """Build one incident from a group of records, listed best source first."""
    props = first_present([m.props for m in members])
    props = {"id": incident_id, **{k: v for k, v in props.items() if k != "id"}}
    evidence: list[EvidenceDraft] = []
    sources: list[str] = []
    for member in members:
        union(evidence, member.evidence)
        for doc in member.docs:
            if doc and doc not in sources:
                sources.append(doc)
    label = next(
        (m.label for m in members if not is_missing(m.label) and m.label != m.key),
        props.get("symptom_text") or incident_id,
    )
    node = ResolvedNode(
        id=incident_id,
        type="Incident",
        label=label,
        props=props,
        evidence=evidence,
        sources=sources,
    )

    machines = {
        m.docs[0]: next(e.dst for e in m.edges if e.rel == "OCCURRED_ON")
        for m in members
        if any(e.rel == "OCCURRED_ON" for e in m.edges)
    }
    machine = next(
        (
            next(e.dst for e in m.edges if e.rel == "OCCURRED_ON")
            for m in members
            if any(e.rel == "OCCURRED_ON" for e in m.edges)
        ),
        None,
    )
    edges = [
        RankedEdge(incident_id, e.rel, e.dst, e.props, list(e.evidence), m.rank, m.seq)
        for m in members
        for e in m.edges
        if e.rel != "OCCURRED_ON" or e.dst == machine
    ]
    conflict = None
    if len(set(machines.values())) > 1:
        conflict = ReviewItem(
            kind="conflict",
            doc_paths=sorted(machines),
            payload={
                "reason": "records name different machines",
                "incident": incident_id,
                "machines": machines,
            },
        )
    return node, edges, conflict


def _undated(members: list[Member]) -> list[ReviewItem]:
    """Review items for records with no date, since an id needs the year."""
    return [
        ReviewItem(
            kind="unresolved",
            doc_paths=[member.doc_path],
            payload={
                "type": "Incident",
                "mention": member.key,
                "reason": "no date",
                "dropped_edges": len(member.edges),
            },
        )
        for member in members
    ]


def _ordered_clusters(
    members: list[Member], clusters: _Clusters, result: Clustered
) -> list[list[Member]]:
    """The groups that can become incidents, earliest first. Groups with no date are
    reported and left out."""
    by_root: dict[tuple[str, str], list[Member]] = defaultdict(list)
    for member in members:
        by_root[clusters.find(member.ident)].append(member)
    keyed = []
    for group in by_root.values():
        group.sort(key=lambda m: (m.order, m.doc_path, m.key))
        dates = [m.date for m in group if m.date is not None]
        if not dates and not any(m.existing_id for m in group):
            result.review += _undated(group)
            continue
        key = (
            min(dates) if dates else dt.date.max,
            group[0].rank,
            min(m.doc_path for m in group),
            min(m.key for m in group),
        )
        keyed.append((key, group))
    keyed.sort(key=lambda item: item[0])
    return [group for _, group in keyed]


def cluster(
    groups: list[DraftGroup], existing: list[Existing], decider: Decider
) -> Clustered:
    """Group the drafts and the existing incidents into final incidents."""
    result = Clustered()
    members = [
        member_of_existing(old, seq=index - len(existing))
        for index, old in enumerate(existing)
    ]
    for group in groups:
        member, conflict = member_of_group(group)
        members.append(member)
        if len(group.nodes) > 1:
            result.merges.append(
                MergeDecision(
                    doc_path=group.doc_path,
                    type="Incident",
                    mention=group.key,
                    candidate=group.key,
                    p=1.0,
                    decision="rule",
                )
            )
        if conflict:
            result.review.append(conflict)

    pairs = []
    for first, second in candidate_pairs(members):
        verdict = questions.ask(
            decider,
            questions.incident_state(summary(first), summary(second)),
            "same_incident",
            questions.SAME_INCIDENT,
            questions.PURPOSES["Incident"],
        )
        pairs.append(_Pair(first, second, verdict))
    clusters = _Clusters(members, pairs)
    _decide_pairs(pairs, clusters, result)

    # New numbers continue after the highest one already used in each year.
    taken: dict[str, int] = defaultdict(int)
    for old in existing:
        found = INCIDENT_ID.fullmatch(old.node.id)
        if found:
            taken[found.group(1)] = max(taken[found.group(1)], int(found.group(2)))
    machine_conflicts = []
    for group in _ordered_clusters(members, clusters, result):
        incident_id = next((m.existing_id for m in group if m.existing_id), None)
        if incident_id is None:
            date = as_date(first_present([m.props for m in group]).get("date"))
            date = date or min(m.date for m in group if m.date is not None)
            year = f"{date.year:04d}"
            taken[year] += 1
            incident_id = f"INC-{year}-{taken[year]:03d}"
        node, edges, conflict = _build(group, incident_id)
        result.nodes.append(node)
        result.edges += edges
        if conflict:
            machine_conflicts.append(conflict)
    result.review += machine_conflicts
    return result
