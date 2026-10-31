"""Layer 2 of the evals: how good the merge and link decisions are, and how well
calibrated their confidence is.

It scores three kinds of decision against the hidden truth: whether a mention names a
candidate part or machine, whether two records describe the same breakdown, and whether
an incident was linked to its true root cause. Each kind gets precision, recall and F1,
a sweep over possible cut-off values, a check that confidence matches how often the
decision is right, and a suggested cut-off. The suggestion is only reported. The
pipeline's own cut-offs are not changed here.

same_entity_report(), same_incident_report() and root_cause_report() score the three
kinds. evaluate() builds the whole section.
"""

from dataclasses import dataclass
from typing import Any

from app.decide import Decider
from app.evals.labels import ENTITY_FIELDS, Record, label_entity, label_incident
from app.evals.match import Matching, match
from app.evals.metrics import Scored, gate_sweep, prf, recommend, summarize
from app.evals.replay import (
    AskedEntity,
    aliases_at,
    first_mentions,
    records_from,
    resolver_questions,
)
from app.evals.truth import Graph, Truth
from app.ingest import questions
from app.ingest.normalize import AliasTable
from app.ingest.types import Extraction


@dataclass(frozen=True)
class EntityPair:
    """A logged same_entity decision: a mention and the best candidate for it, with its
    place in the merge log and the label from the truth."""

    index: int
    type: str
    doc_path: str
    mention: str
    candidate: str
    p: float | None
    label: bool | None


@dataclass(frozen=True)
class Negative:
    """A wrong candidate for a mention. Its kind is resolver if the resolver asked about
    it, or sibling if it is a similar part the resolver did not ask about."""

    index: int
    type: str
    doc_path: str
    mention: str
    candidate: str
    kind: str
    p: float | None = None
    backend: str | None = None


# The identity of a question: type, mention text, document of the mention, and candidate.
QuestionKey = tuple[str, str, str, str]


def _family(entity_id: str) -> str:
    return entity_id.split("-")[0]


def decision_index(merges: list[dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    """Where in the merge log each (type, document, mention) was decided. The first
    decision wins, since the resolver decides a mention once."""
    found: dict[tuple[str, str, str], int] = {}
    for index, m in enumerate(merges):
        found.setdefault((m["type"], m["doc_path"], m["mention"]), index)
    return found


def entity_state_for(
    graph: Graph,
    merges: list[dict[str, Any]],
    first: dict[tuple[str, str], tuple[str, str]],
    *,
    index: int,
    type: str,
    mention: str,
    doc_path: str,
    candidate: str,
) -> dict[str, Any]:
    """Rebuild the question the resolver would send for a mention and a candidate, using
    the final graph and the aliases the candidate had at that point in the log."""
    node = graph.nodes[candidate]
    return questions.entity_state(
        type=type,
        text=mention,
        quote=first.get((type, mention), (doc_path, ""))[1],
        doc_path=doc_path,
        candidate_id=node.id,
        label=node.label,
        description=str(node.props.get("description") or node.props.get("name") or ""),
        aliases=aliases_at(graph, node.id, merges, index),
        quotes=[e.quote for e in graph.node_evidence.get(node.id, ())],
    )


def question_key(asked: AskedEntity) -> QuestionKey:
    mention, candidate = asked.state["mention"], asked.state["candidate"]
    return (mention["type"], mention["text"], mention["doc_path"], candidate["id"])


def resolver_negatives(
    truth: Truth,
    asked: list[AskedEntity],
    merges: list[dict[str, Any]],
    logged: set[QuestionKey],
) -> list[Negative]:
    """The wrong candidates the resolver asked about, each with the confidence it got.
    Questions already in the logged pairs, or that the truth cannot label, are left out.
    The order is fixed so reruns match."""
    positions = decision_index(merges)
    found: dict[QuestionKey, Negative] = {}
    for item in asked:
        key = question_key(item)
        type_, text, doc_path, candidate = key
        if key in logged or key in found or type_ not in ENTITY_FIELDS:
            continue
        if label_entity(truth, type_, text, doc_path, candidate) is not False:
            continue
        index = positions.get((type_, doc_path, text), len(merges))
        found[key] = Negative(
            index, type_, doc_path, text, candidate, "resolver", item.p, item.backend
        )
    return [found[key] for key in sorted(found)]


def sibling_negatives(
    truth: Truth,
    graph: Graph,
    pairs: list[EntityPair],
    asked: set[QuestionKey],
) -> list[Negative]:
    """For each true pair, the other entities of the same family that the mention does
    not name and that the resolver did not ask about."""
    found = []
    for pair in sorted(pairs, key=lambda p: p.index):
        if pair.label is not True:
            continue
        for other in sorted(graph.of_type(pair.type)):
            if (
                other != pair.candidate
                and _family(other) == _family(pair.candidate)
                and (pair.type, pair.mention, pair.doc_path, other) not in asked
                and label_entity(truth, pair.type, pair.mention, pair.doc_path, other)
                is False
            ):
                found.append(
                    Negative(
                        pair.index,
                        pair.type,
                        pair.doc_path,
                        pair.mention,
                        other,
                        "sibling",
                    )
                )
    return found


# --- the three kinds of question --------------------------------------------------

ENTITY_TYPES = tuple(ENTITY_FIELDS)


def merge_log(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every merge decision of a pipeline report, in the order they were made."""
    return [m for f in report["files"] for m in f["merges"]]


def entity_pairs(
    truth: Truth, merges: list[dict[str, Any]]
) -> tuple[list[EntityPair], dict[str, dict[str, int]], int]:
    """The same_entity decisions that asked the decider, labeled by the truth. Also the
    rule-based decisions, counted apart, and the number of mentions with no candidate."""
    pairs: list[EntityPair] = []
    rules: dict[str, dict[str, int]] = {}
    no_candidate = 0
    for index, m in enumerate(merges):
        if m["type"] not in ENTITY_TYPES:
            continue
        if m["candidate"] is None:
            no_candidate += m["decision"] != "rule"
            continue
        label = label_entity(
            truth, m["type"], m["mention"], m["doc_path"], m["candidate"]
        )
        if m["decision"] == "rule":
            counts = rules.setdefault(
                m["type"], {"n": 0, "correct": 0, "wrong": 0, "unlabeled": 0}
            )
            counts["n"] += 1
            counts[
                "unlabeled" if label is None else "correct" if label else "wrong"
            ] += 1
            continue
        pairs.append(
            EntityPair(
                index,
                m["type"],
                m["doc_path"],
                m["mention"],
                m["candidate"],
                m["p"],
                label,
            )
        )
    return pairs, rules, no_candidate


def _entity_band(p: float | None) -> str:
    return "undecided" if p is None else questions.merge_band(p, "entity")


def _incident_band(p: float | None) -> str:
    return "undecided" if p is None else questions.merge_band(p, "incident")


def _link_band(p: float | None) -> str:
    return "undecided" if p is None else questions.link_band(p)


def same_entity_report(
    truth: Truth,
    graph: Graph,
    merges: list[dict[str, Any]],
    extractions: list[Extraction],
    aliases: AliasTable,
    decider: Decider,
    replay_decider: Decider | None = None,
) -> dict[str, Any]:
    """Score the same_entity decisions. Logged pairs keep the confidence the pipeline
    got. The negatives are the wrong candidates the resolver asked about, found by
    resolving again through replay_decider, plus similar parts it did not ask about. Those
    are asked now, so a rerun replays them from the saved decisions. Give replay_decider a
    decider that only replays, so a replay can never cost money."""
    pairs, rules, no_candidate = entity_pairs(truth, merges)
    labeled = [p for p in pairs if p.label is not None]
    unlabeled = [p for p in pairs if p.label is None]
    items = [Scored(p.p, bool(p.label)) for p in labeled]
    first = first_mentions(extractions)
    logged = {(p.type, p.mention, p.doc_path, p.candidate) for p in labeled}
    replayed = resolver_questions(extractions, aliases, replay_decider or decider)
    asked_keys = {question_key(item) for item in replayed}
    negatives = resolver_negatives(truth, replayed, merges, logged)
    siblings = sibling_negatives(truth, graph, pairs, asked_keys)
    true_of = {(p.type, p.doc_path, p.mention): p.candidate for p in labeled if p.label}
    entries = []
    for negative in [*negatives, *siblings]:
        if negative.kind == "sibling":
            state = entity_state_for(
                graph,
                merges,
                first,
                index=negative.index,
                type=negative.type,
                mention=negative.mention,
                doc_path=negative.doc_path,
                candidate=negative.candidate,
            )
            verdict = questions.ask(
                decider,
                state,
                "same_entity",
                questions.SAME_ENTITY,
                f"eval {questions.PURPOSES[negative.type]}",
            )
            p, backend = verdict.p, verdict.backend
        else:
            p, backend = negative.p, negative.backend
        items.append(Scored(p, False))
        entries.append(
            {
                "type": negative.type,
                "doc_path": negative.doc_path,
                "mention": negative.mention,
                "true_candidate": true_of.get(
                    (negative.type, negative.doc_path, negative.mention)
                ),
                "candidate": negative.candidate,
                "kind": negative.kind,
                "p": p,
                "backend": backend,
            }
        )
    report = summarize(
        items,
        gate=questions.GATES["merge_entity"],
        band=_entity_band,
        unlabeled=len(unlabeled),
    )
    report["sources"] = {
        "logged": len(labeled),
        "resolver_asked": len(negatives),
        "siblings": len(siblings),
    }
    report["no_candidate"] = no_candidate
    report["rules"] = rules
    report["negatives"] = entries
    report["unlabeled"] = [
        {
            "type": p.type,
            "doc_path": p.doc_path,
            "mention": p.mention,
            "candidate": p.candidate,
            "p": p.p,
        }
        for p in unlabeled
    ]
    return report


def same_incident_report(
    truth: Truth,
    merges: list[dict[str, Any]],
    records: dict[tuple[str, str], Record],
) -> dict[str, Any]:
    """Score the same_incident decisions. A pair is true when both records map to the
    same true incident. Two views are reported. The first treats every pair above the cut
    off as a merge. The second uses the logged decision, because veto and cannot-link
    rules can refuse a pair that scored above the cut-off. The suggestion comes from the
    second view."""
    gate = questions.GATES["merge_incident"]
    items: list[Scored] = []
    decided: list[Scored] = []
    logged: list[dict[str, Any]] = []
    unlabeled = []
    rules = 0
    for m in merges:
        if m["type"] != "Incident":
            continue
        if m["decision"] == "rule" or m["candidate"] is None:
            rules += 1
            continue
        doc, _, key = m["candidate"].partition("::")
        first, second = (
            records.get((m["doc_path"], m["mention"])),
            records.get((doc, key)),
        )
        label = label_incident(truth, first, second) if first and second else None
        if label is None:
            unlabeled.append(
                {
                    "doc_path": m["doc_path"],
                    "mention": m["mention"],
                    "candidate": m["candidate"],
                    "p": m["p"],
                }
            )
            continue
        refused = m["decision"] != "merge" and m["p"] is not None and m["p"] >= gate
        items.append(Scored(m["p"], label))
        decided.append(Scored(m["p"], label, refused))
        logged.append({**m, "label": label})
    report = summarize(
        items,
        gate=gate,
        band=_incident_band,
        unlabeled=len(unlabeled),
    )
    tp = sum(1 for m in logged if m["decision"] == "merge" and m["label"])
    fp = sum(1 for m in logged if m["decision"] == "merge" and not m["label"])
    fn = sum(1 for m in logged if m["decision"] != "merge" and m["label"])
    refused = [
        {
            "doc_path": m["doc_path"],
            "mention": m["mention"],
            "candidate": m["candidate"],
            "p": m["p"],
            "decision": m["decision"],
            "label": m["label"],
        }
        for m, item in zip(logged, decided, strict=True)
        if item.refused
    ]
    recommendation = recommend(decided)
    recommendation["basis"] = "decision_level"
    refused_false = [r["p"] for r in refused if not r["label"]]
    if refused_false:
        recommendation["refused_false_pairs"] = {
            "count": len(refused_false),
            "max_p": max(refused_false),
        }
        recommendation["note"] = (
            f"{len(refused_false)} false pair(s) scored up to p {max(refused_false)} but the "
            "veto or cannot-link rules refused the merge, so the gate is not what stops them"
        )
    report["decision_level"] = {
        "at_gate": {"gate": gate, **prf(tp, fp, fn)},
        "sweep": gate_sweep(decided),
        "refused": refused,
        "recommendation": recommendation,
    }
    report["p_only_recommendation"] = report["recommendation"]
    report["recommendation"] = recommendation
    report["rules"] = {"decisions": rules}
    report["unlabeled"] = unlabeled
    return report


def root_cause_report(truth: Truth, graph: Graph, matching: Matching) -> dict[str, Any]:
    """Score the root-cause links. A link is correct when its failure mode equals the
    true cause of the matched incident. A matched incident with no link counts as a miss.
    Links on an unmatched incident are not scored."""
    items: list[Scored] = []
    unlabeled = 0
    linked: set[str] = set()
    links = 0
    for edge in graph.edges:
        if edge.rel != "ROOT_CAUSE":
            continue
        links += 1
        linked.add(edge.src)
        truth_incident_id = matching.truth_id(edge.src)
        expected = truth.root_cause_of(truth_incident_id) if truth_incident_id else None
        if expected is None:
            unlabeled += 1
            continue
        evidence = [
            e for e in graph.edge_evidence.get(edge.key, ()) if e.extractor == "linker"
        ]
        p = evidence[0].confidence if evidence else None
        items.append(Scored(p, matching.truth_id(edge.dst) == expected))
    without = 0
    for incident_id in graph.of_type("Incident"):
        truth_incident_id = matching.truth_id(incident_id)
        if (
            incident_id not in linked
            and truth_incident_id is not None
            and truth.root_cause_of(truth_incident_id) is not None
        ):
            items.append(Scored(None, True))
            without += 1
    report = summarize(
        items,
        gate=questions.GATES["link"],
        band=_link_band,
        unlabeled=unlabeled,
        band_names=("link", "review", "none", "undecided"),
    )
    report["sources"] = {"links": links, "matched_incidents_without_a_link": without}
    return report


def evaluate(
    truth: Truth,
    graph: Graph,
    report: dict[str, Any],
    extractions: list[Extraction],
    aliases: AliasTable,
    decider: Decider,
    matching: Matching | None = None,
    replay_decider: Decider | None = None,
) -> dict[str, Any]:
    """The whole layer 2 section, built from the pipeline report and the replayed
    extractions."""
    merges = merge_log(report)
    matching = matching or match(truth, graph)
    return {
        "gates": dict(questions.GATES),
        "same_entity": same_entity_report(
            truth, graph, merges, extractions, aliases, decider, replay_decider
        ),
        "same_incident": same_incident_report(
            truth, merges, records_from(extractions, aliases)
        ),
        "root_cause": root_cause_report(truth, graph, matching),
    }
