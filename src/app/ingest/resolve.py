"""Combines everything the readers found into one consistent set of nodes and edges.

Different files describe the same machines, parts and alarms in different words.
resolve merges the duplicates, matches loose mentions to real items, groups the
draft incidents, and merges identical links. It works in three steps: merge items
that share an id, match mentions to items, then group incidents. When sources
disagree, the more trusted source wins (see rank).
"""

from collections import defaultdict
from dataclasses import dataclass, field

from app.decide import Decider
from app.ingest import incidents
from app.ingest.incident_records import DraftEdge, DraftGroup, Existing, RankedEdge
from app.ingest.merge_props import IDENTITY_PROP, Contribution, build_node
from app.ingest.normalize import AliasTable, part_key
from app.ingest.procedure_ids import mint_procedure_ids
from app.ingest.resolve_mentions import Mention, MentionResolver
from app.ingest.types import (
    CandidateEdge,
    EvidenceDraft,
    Extraction,
    NodeRef,
    Resolution,
    ResolvedEdge,
    ResolvedNode,
    ReviewItem,
)

# How much each kind of source is trusted. A lower number wins.
MANUAL, MAINTENANCE_CSV, LOOSE_NOTE, INVENTORY, HANDOVER, LOG = 1, 2, 3, 4, 5, 6
UNRANKED = 7
# Types whose ids a manual makes up, so the same id in two manuals may mean two things.
MANUAL_MINTED = frozenset({"Component", "FailureMode"})


def rank(extraction: Extraction) -> int:
    """Return how much an extraction's source is trusted. A lower number wins."""
    if extraction.doc_type == "manual":
        return MANUAL
    if extraction.extractor == "csv-parser":
        return MAINTENANCE_CSV
    return {
        "maintenance_note": LOOSE_NOTE,
        "procedure": LOOSE_NOTE,
        "inventory": INVENTORY,
        "handover": HANDOVER,
        "log": LOG,
    }.get(extraction.doc_type, UNRANKED)


@dataclass
class Registry:
    """The nodes and edges already in the graph, used when adding a single file."""

    nodes: list[ResolvedNode] = field(default_factory=list)
    edges: list[ResolvedEdge] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "Registry":
        return cls()


class _Resolver:
    """Holds the working state of one resolve run."""

    def __init__(self, aliases: AliasTable, decider: Decider, registry: Registry):
        self.registry = registry
        self.groups: dict[tuple[str, str], list[Contribution]] = defaultdict(list)
        self.known: dict[tuple[str, str], ResolvedNode] = {}
        self.part_keys: dict[str, set[str]] = defaultdict(set)
        self.mentioner = MentionResolver(aliases, decider, self.known, self.part_keys)
        self.pending: dict[tuple[str, str], list[Contribution]] = defaultdict(list)
        self.review: list[ReviewItem] = []
        # The review item for each manual that lost an id to another manual.
        self.losers: dict[tuple[str, str], dict[str, ReviewItem]] = defaultdict(dict)
        self.draft_groups: dict[tuple[str, str], DraftGroup] = {}
        self.plain_edges: list[RankedEdge] = []

    # -- pass 1: canonical nodes --

    def collect_canonical(
        self, extractions: list[Extraction], ranks: list[int]
    ) -> None:
        for node in self.registry.nodes:
            if node.type != "Incident":
                self.groups[node.type, node.id].append(
                    Contribution(
                        0, -1, list(node.sources), node.label, node.props, node.evidence
                    )
                )
        seq = 0
        for extraction, ranking in zip(extractions, ranks, strict=True):
            for node in extraction.nodes:
                seq += 1
                if node.canonical and node.ref.type != "Incident":
                    self.groups[node.ref.type, node.ref.key].append(
                        Contribution(
                            ranking,
                            seq,
                            [extraction.doc_path],
                            node.label,
                            node.props,
                            list(node.evidence),
                        )
                    )
            seq += len(extraction.edges)
        self.build_known()

    def build_known(self) -> None:
        """Build the final nodes. If a second manual reuses an id, it is left out and reported."""
        for (node_type, node_id), items in sorted(self.groups.items()):
            items.sort(key=lambda c: c.order)
            if node_type in MANUAL_MINTED:
                owners = set(items[0].docs)
                kept = [i for i in items if set(i.docs) & owners or not i.docs]
                for other in (i for i in items if i not in kept):
                    item = self.conflict(node_type, node_id, items[0], other)
                    doc = item.payload["other"]
                    if doc in self.losers[node_type, node_id]:
                        continue
                    self.losers[node_type, node_id][doc] = item
                    self.review.append(item)
                self.groups[node_type, node_id] = items = kept
            self.set_known(node_type, node_id, build_node(node_type, node_id, items))

    @staticmethod
    def conflict(
        node_type: str, node_id: str, kept: Contribution, other: Contribution
    ) -> ReviewItem:
        return ReviewItem(
            kind="conflict",
            doc_paths=[*kept.docs, *other.docs],
            payload={
                "reason": "the same id in two manuals",
                "type": node_type,
                "id": node_id,
                "kept": kept.docs[0] if kept.docs else "",
                "other": other.docs[0] if other.docs else "",
                "dropped_edges": 0,
            },
        )

    def set_known(self, node_type: str, node_id: str, node: ResolvedNode) -> None:
        self.known[node_type, node_id] = node
        if node_type == "Part":
            for text in [node_id, *node.props.get("aliases", [])]:
                if part_key(str(text)):
                    self.part_keys[part_key(str(text))].add(node_id)

    # -- pass 2: mentions --

    def mention(
        self,
        node_type: str,
        text: str,
        doc_path: str,
        evidence: list[EvidenceDraft],
        ranking: int,
        seq: int,
    ) -> Mention:
        """Resolve a mention once, from its first occurrence."""
        found, created = self.mentioner.mention(
            node_type, text, doc_path, evidence, ranking, seq
        )
        if created is not None and found.id is not None:
            self.groups[found.type, found.id].append(created)
        return found

    def endpoint(
        self,
        ref: NodeRef,
        doc_path: str,
        evidence: list[EvidenceDraft],
        ranking: int,
        seq: int,
    ) -> str | None:
        """Return the real id for one end of an edge, or None if it cannot be matched.

        A part name on an edge is always treated as a mention, even if it looks like an id.
        """
        if ref.type != "Part" and (ref.type, ref.key) in self.known:
            return ref.key
        return self.mention(ref.type, ref.key, doc_path, evidence, ranking, seq).id

    def read(self, extraction: Extraction, ranking: int, seq: int) -> int:
        """Resolve the mentions in one extraction, nodes first and then edges."""
        doc = extraction.doc_path
        for node in extraction.nodes:
            seq += 1
            if node.canonical:
                continue
            if node.ref.type == "Incident":
                key = (doc, node.ref.key)
                if key not in self.draft_groups:
                    self.draft_groups[key] = DraftGroup(
                        doc, extraction.doc_type, node.ref.key, ranking, seq, []
                    )
                self.draft_groups[key].nodes.append(node)
                continue
            mention = self.mention(
                node.ref.type, node.ref.key, doc, node.evidence, ranking, seq
            )
            if mention.id is None:
                mention.dropped_nodes += 1
                continue
            # The node adds what it knows to the item it matched, but its raw key is not
            # used as an id.
            props = {
                k: v
                for k, v in node.props.items()
                if k != IDENTITY_PROP.get(node.ref.type)
            }
            self.pending[node.ref.type, mention.id].append(
                Contribution(ranking, seq, [doc], "", props, list(node.evidence))
            )
        for edge in extraction.edges:
            seq += 1
            self.read_edge(edge, extraction, ranking, seq)
        return seq

    def read_edge(
        self, edge: CandidateEdge, extraction: Extraction, ranking: int, seq: int
    ) -> None:
        doc = extraction.doc_path
        for ref in (edge.src, edge.dst):
            lost = self.losers.get((ref.type, ref.key), {}).get(doc)
            if lost is not None:
                # This manual lost the id, so its links to that id are dropped too.
                lost.payload["dropped_edges"] += 1
                return
        draft = (
            self.draft_groups.get((doc, edge.src.key))
            if edge.src.type == "Incident"
            else None
        )
        dst = self.endpoint(edge.dst, doc, edge.evidence, ranking, seq)
        src = None
        if draft is not None:
            if edge.rel == "OCCURRED_ON":
                # Keep the raw machine text for the model, even if it did not match.
                draft.machine_text = draft.machine_text or edge.dst.key
        else:
            src = self.endpoint(edge.src, doc, edge.evidence, ranking, seq)
        dropped = [edge.dst] if dst is None else []
        if draft is None and src is None:
            dropped.append(edge.src)
        for ref in dropped:
            self.mentioner.mentions[ref.type, ref.key].dropped_edges += 1
        if dropped:
            return
        if draft is not None:
            draft.edges.append(
                DraftEdge(edge.rel, dst, dict(edge.props), list(edge.evidence))
            )
        else:
            self.plain_edges.append(
                RankedEdge(
                    src,  # type: ignore[arg-type]
                    edge.rel,
                    dst,
                    dict(edge.props),
                    list(edge.evidence),
                    ranking,
                    seq,
                )
            )

    # -- the result --

    def final_nodes(self) -> list[ResolvedNode]:
        """Return the final nodes, including mentions that matched and aliases they added."""
        nodes: dict[tuple[str, str], ResolvedNode] = {}
        keys = sorted({*self.groups, *self.pending})
        for key in keys:
            items = [*self.groups.get(key, []), *self.pending.get(key, [])]
            node = build_node(*key, sorted(items, key=lambda c: c.order))
            for text in self.mentioner.added_aliases.get(key, []):
                current = list(node.props.get("aliases", []))
                if text.casefold() not in {str(a).casefold() for a in current}:
                    node.props = {**node.props, "aliases": [*current, text]}
            nodes[key] = node
        return list(nodes.values())


def _merge_edges(edges: list[RankedEdge]) -> list[ResolvedEdge]:
    """Merge identical edges. Properties come from the most trusted one, evidence from all."""
    merged: dict[tuple[str, str, str], ResolvedEdge] = {}
    for edge in sorted(edges, key=lambda e: (e.rank, e.seq)):
        found = merged.get((edge.src, edge.rel, edge.dst))
        if found is None:
            merged[edge.src, edge.rel, edge.dst] = ResolvedEdge(
                src=edge.src,
                rel=edge.rel,
                dst=edge.dst,
                props=dict(edge.props),
                evidence=list(edge.evidence),
            )
        else:
            found.evidence += [e for e in edge.evidence if e not in found.evidence]
    return sorted(merged.values(), key=lambda e: (e.src, e.rel, e.dst))


def resolve(
    extractions: list[Extraction],
    aliases: AliasTable,
    decider: Decider,
    registry: Registry | None = None,
) -> Resolution:
    """Combine all extractions into one set of nodes and edges.

    The extractions are in processing order, which settles ties between equally
    trusted sources. With a registry, the existing graph comes first, so new values
    only fill gaps.
    """
    registry = registry or Registry.empty()
    extractions = mint_procedure_ids(
        extractions, [n.id for n in registry.nodes if n.type == "Procedure"]
    )
    run = _Resolver(aliases, decider, registry)
    ranks = [rank(extraction) for extraction in extractions]

    run.collect_canonical(extractions, ranks)
    seq = 0
    for extraction, ranking in zip(extractions, ranks, strict=True):
        seq = run.read(extraction, ranking, seq)

    existing = [
        Existing(node, [e for e in registry.edges if e.src == node.id])
        for node in registry.nodes
        if node.type == "Incident"
    ]
    clustered = incidents.cluster(list(run.draft_groups.values()), existing, decider)

    edges = [
        RankedEdge(e.src, e.rel, e.dst, e.props, e.evidence, 0, -1)
        for e in registry.edges
    ]
    edges += run.plain_edges + clustered.edges
    nodes = [*run.final_nodes(), *clustered.nodes]
    return Resolution(
        nodes=sorted(nodes, key=lambda n: (n.id, n.type)),
        edges=_merge_edges(edges),
        merges=[*run.mentioner.merges, *clustered.merges],
        review=[*run.review, *run.mentioner.reviews(), *clustered.review],
    )
