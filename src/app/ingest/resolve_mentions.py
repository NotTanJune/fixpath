"""Matches loose names for a machine, part or alarm to the real item they mean.

MentionResolver handles each distinct name once. It tries a simple rule first. If
that fails for a machine or part, it finds similar-looking items and asks the model
whether they are the same, then acts on the most likely answer.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz, process, utils

from app.decide import Decider
from app.ingest import questions
from app.ingest.merge_props import Contribution, build_node
from app.ingest.normalize import AliasTable, normalize_alarm_code, part_key
from app.ingest.types import EvidenceDraft, MergeDecision, ResolvedNode, ReviewItem

MENTION_TYPES = frozenset({"Asset", "Part", "AlarmCode"})
FUZZY_TYPES = frozenset({"Asset", "Part"})

FUZZY_LIMIT = 3
FUZZY_CUTOFF = 70


@dataclass
class Mention:
    """A loose name from a file, what it matched, and why a person may need to look.

    The counts show what was dropped because the name stayed unmatched.
    """

    type: str
    text: str
    doc_path: str
    quote: str
    id: str | None = None
    kind: str | None = None
    reason: str | None = None
    candidate: str | None = None
    p: float | None = None
    dropped_edges: int = 0
    dropped_nodes: int = 0
    errors: list[dict[str, str | None]] = field(default_factory=list)

    def review(self) -> ReviewItem | None:
        if self.kind is None:
            return None
        payload: dict[str, Any] = {
            "type": self.type,
            "mention": self.text,
            "reason": self.reason,
            "candidate": self.candidate,
            "p": self.p,
            "dropped_edges": self.dropped_edges,
            "dropped_nodes": self.dropped_nodes,
        }
        if self.errors:
            payload["errors"] = list(self.errors)
        return ReviewItem(
            kind=self.kind,  # type: ignore[arg-type]
            doc_paths=[self.doc_path],
            payload=payload,
            confidence=self.p if self.kind == "merge" else None,
        )


class MentionResolver:
    """Matches names against the nodes already known and logs every decision.

    The caller fills the known nodes first. The resolver adds to them when a name
    creates a node or an alias, and keeps the new aliases for the caller to apply.
    """

    def __init__(
        self,
        aliases: AliasTable,
        decider: Decider,
        known: dict[tuple[str, str], ResolvedNode],
        part_keys: dict[str, set[str]],
    ) -> None:
        self.aliases = aliases
        self.decider = decider
        self.known = known
        self.part_keys = part_keys
        self.mentions: dict[tuple[str, str], Mention] = {}
        self.merges: list[MergeDecision] = []
        self.added_aliases: dict[tuple[str, str], list[str]] = defaultdict(list)

    def mention(
        self,
        node_type: str,
        text: str,
        doc_path: str,
        evidence: list[EvidenceDraft],
        ranking: int,
        seq: int,
    ) -> tuple[Mention, Contribution | None]:
        """Resolve a name once, from its first occurrence.

        The second value is a new alarm node, made when no manual lists the code. It
        is None otherwise.
        """
        found = self.mentions.get((node_type, text))
        if found is not None:
            return found, None
        quote = evidence[0].quote if evidence else ""
        found = self.mentions[node_type, text] = Mention(
            node_type, text, doc_path, quote
        )
        return found, self.decide(found, evidence, ranking, seq)

    def reviews(self) -> list[ReviewItem]:
        items = (mention.review() for mention in self.mentions.values())
        return [item for item in items if item is not None]

    def record(
        self,
        mention: Mention,
        decision: str,
        candidate: str | None = None,
        p: float | None = None,
        backend: str | None = None,
    ) -> None:
        self.merges.append(
            MergeDecision(
                doc_path=mention.doc_path,
                type=mention.type,
                mention=mention.text,
                candidate=candidate,
                p=p,
                decision=decision,  # type: ignore[arg-type]
                backend=backend,
            )
        )

    def by_rule(self, mention: Mention) -> str | None:
        """Return the id a simple fixed rule gives for this name, or None."""
        text = mention.text
        if mention.type == "Asset":
            tag = self.aliases.asset_for(text)
            return tag if tag and ("Asset", tag) in self.known else None
        if mention.type == "AlarmCode":
            return normalize_alarm_code(text)
        if mention.type == "Part":
            if ("Part", text) in self.known:
                return text
            ids = self.part_keys.get(part_key(text), set()) if part_key(text) else set()
            return next(iter(ids)) if len(ids) == 1 else None
        return None

    def decide(
        self,
        mention: Mention,
        evidence: list[EvidenceDraft],
        ranking: int,
        seq: int,
    ) -> Contribution | None:
        if mention.type not in MENTION_TYPES:
            mention.kind, mention.reason = "unresolved", f"not a known {mention.type}"
            self.record(mention, "unresolved")
            return None
        ruled = self.by_rule(mention)
        if ruled is not None:
            mention.id = ruled
            self.record(mention, "rule", ruled, 1.0)
            if (mention.type, ruled) not in self.known:
                # A valid alarm code that no manual lists gets a node of its own.
                created = Contribution(
                    ranking, seq, [mention.doc_path], ruled, {}, list(evidence)
                )
                self.known[mention.type, ruled] = build_node(
                    mention.type, ruled, [created]
                )
                return created
            return None
        if mention.type in FUZZY_TYPES:
            self.ask_candidates(mention)
        else:
            mention.kind, mention.reason = "unresolved", "not a valid code"
            self.record(mention, "unresolved")
        return None

    def fuzzy_candidates(self, mention: Mention) -> list[str]:
        """Return a few ids that look like the name, best first.

        Each item is compared by its id, label, description, name and aliases.
        """
        choices: dict[int, str] = {}
        owners: list[str] = []
        for (node_type, node_id), node in sorted(self.known.items()):
            if node_type != mention.type:
                continue
            props = node.props
            variants = [
                node_id,
                node.label,
                props.get("description"),
                props.get("name"),
                *props.get("aliases", []),
            ]
            for text in dict.fromkeys(
                v for v in variants if isinstance(v, str) and v.strip()
            ):
                choices[len(owners)] = text
                owners.append(node_id)
        found = process.extract(
            mention.text,
            choices,
            scorer=fuzz.WRatio,
            processor=utils.default_process,
            limit=FUZZY_LIMIT,
            score_cutoff=FUZZY_CUTOFF,
        )
        best: dict[str, float] = {}
        for _text, score, index in found:
            best.setdefault(owners[index], score)
        return list(best)

    def ask_candidates(self, mention: Mention) -> None:
        """Ask the model about each similar item. The most likely answer decides."""
        candidates = self.fuzzy_candidates(mention)
        if not candidates:
            mention.kind, mention.reason = "unresolved", "no candidate"
            self.record(mention, "unresolved")
            return
        verdicts = []
        for candidate_id in candidates:
            node = self.known[mention.type, candidate_id]
            state = questions.entity_state(
                type=mention.type,
                text=mention.text,
                quote=mention.quote,
                doc_path=mention.doc_path,
                candidate_id=candidate_id,
                label=node.label,
                description=str(
                    node.props.get("description") or node.props.get("name") or ""
                ),
                aliases=list(node.props.get("aliases", [])),
                quotes=[e.quote for e in node.evidence],
            )
            verdict = questions.ask(
                self.decider,
                state,
                "same_entity",
                questions.SAME_ENTITY,
                questions.PURPOSES[mention.type],
            )
            verdicts.append((candidate_id, verdict))
            if (verdict.reason or "").startswith(questions.DECIDER_ERROR):
                mention.errors.append(
                    {"candidate": candidate_id, "reason": verdict.reason}
                )
        decided = [(cid, v) for cid, v in verdicts if v.p is not None]
        if not decided:
            first_id, first = verdicts[0]
            mention.kind, mention.reason = "merge", first.reason
            mention.candidate = first_id
            self.record(mention, "unresolved", first_id, None, first.backend)
            return
        best_id, best = max(decided, key=lambda item: item[1].p)
        mention.candidate, mention.p = best_id, best.p
        band = questions.merge_band(best.p, "entity")
        if band == "merge":
            mention.id = best_id
            if mention.errors:
                # Another candidate decided, but a person should still see the failure.
                mention.kind = "merge"
                mention.reason = "a candidate failed to decide"
            self.add_alias(mention.type, best_id, mention.text)
            self.record(mention, "merge", best_id, best.p, best.backend)
        elif band == "queue":
            mention.kind, mention.reason = "merge", "unsure match"
            self.record(mention, "queue", best_id, best.p, best.backend)
        else:
            mention.kind, mention.reason = "unresolved", "no match"
            self.record(mention, "unresolved", best_id, best.p, best.backend)

    def add_alias(self, node_type: str, node_id: str, text: str) -> None:
        """Record the name as an alias of the item it merged into, for later names to use."""
        self.added_aliases[node_type, node_id].append(text)
        node = self.known[node_type, node_id]
        current = list(node.props.get("aliases", []))
        if text.casefold() not in {str(a).casefold() for a in current}:
            node.props = {**node.props, "aliases": [*current, text]}
        if node_type == "Part" and part_key(text):
            self.part_keys[part_key(text)].add(node_id)
