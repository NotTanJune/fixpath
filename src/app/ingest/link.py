"""Links incidents to their likely root causes and counts how often each cause is seen.

For each incident with a machine and no known cause, the decider is asked which
failure mode of that machine caused it. A confident answer becomes a link. An unsure
answer goes to a person with the top two options. An incident with no machine also
goes to a person.

The same step counts, for each alarm and failure mode pair in the manuals, how many
incidents showed that pairing. A pairing seen in incidents but missing from every
manual is reported for review, not added.

link is the main function. It returns a new result and leaves the input unchanged.
"""

from collections import defaultdict
from typing import Any

from app.decide import Decider
from app.ingest import questions
from app.ingest.types import (
    MAX_QUOTE,
    EvidenceDraft,
    Resolution,
    ResolvedEdge,
    ResolvedNode,
    ReviewItem,
)


class _Graph:
    """Quick lookups over the resolved facts."""

    def __init__(self, resolution: Resolution) -> None:
        self.nodes = {n.id: n for n in resolution.nodes}
        self.out: dict[tuple[str, str], list[ResolvedEdge]] = defaultdict(list)
        self.into: dict[tuple[str, str], list[ResolvedEdge]] = defaultdict(list)
        for edge in resolution.edges:
            self.out[edge.src, edge.rel].append(edge)
            self.into[edge.dst, edge.rel].append(edge)

    def targets(self, src: str, rel: str) -> list[str]:
        return [e.dst for e in self.out.get((src, rel), [])]

    def name(self, node_id: str) -> str:
        node = self.nodes.get(node_id)
        if node is None:
            return node_id
        return str(node.props.get("name") or node.label or node_id)

    def failure_modes_of(self, machine: str) -> list[str]:
        """The failure modes that can damage a part of the machine."""
        components = set(self.targets(machine, "HAS_COMPONENT"))
        components |= {
            n.id
            for n in self.nodes.values()
            if n.type == "Component" and n.props.get("asset_id") == machine
        }
        found = {
            edge.src
            for node_id in components
            for edge in self.into.get((node_id, "AFFECTS"), [])
        }
        return sorted(found)

    def procedure_titles(self, failure_mode: str) -> list[str]:
        titles = []
        for proc in self.targets(failure_mode, "RESOLVED_BY"):
            node = self.nodes.get(proc)
            if node is not None:
                titles.append(str(node.props.get("title") or node.label))
        return sorted(set(titles))

    def option_text(self, failure_mode: str) -> str:
        text = self.name(failure_mode)
        titles = self.procedure_titles(failure_mode)
        return f"{text} (fixed by: {'; '.join(titles)})" if titles else text

    def summary(self, incident: ResolvedNode) -> dict[str, Any]:
        props = incident.props
        parts = [
            {"part": e.dst, "qty": e.props.get("qty")}
            for e in self.out.get((incident.id, "USED"), [])
        ]
        machine = next(iter(self.targets(incident.id, "OCCURRED_ON")), None)
        alarms = self.targets(incident.id, "TRIGGERED_BY")
        return questions.incident_summary(
            source_type="incident",
            date=props.get("date"),
            shift=props.get("shift"),
            machine=machine,
            alarm=", ".join(alarms) or None,
            symptom=props.get("symptom_text"),
            action=props.get("resolution_text"),
            parts=parts,
            technician=props.get("technician"),
            downtime_min=props.get("downtime_min"),
            quote=incident.evidence[0].quote if incident.evidence else None,
        )

    def alarm_facts(self, alarm: str) -> dict[str, Any]:
        node = self.nodes.get(alarm)
        causes = [
            {
                "failure_mode": self.name(e.dst),
                "id": e.dst,
                "likelihood": e.props.get("likelihood"),
            }
            for e in self.out.get((alarm, "INDICATES"), [])
        ]
        text = (node.props.get("text") or node.label) if node else ""
        return {"code": alarm, "text": text, "causes": causes}


def _evidence(incident: ResolvedNode, p: float) -> EvidenceDraft:
    """The evidence for a link: the quote from the incident's best source."""
    if incident.evidence:
        first = incident.evidence[0]
        doc_path, page, quote = first.doc_path, first.page, first.quote
    else:
        doc_path = incident.sources[0] if incident.sources else ""
        page = None
        quote = str(
            incident.props.get("resolution_text")
            or incident.props.get("symptom_text")
            or incident.label
        )
    return EvidenceDraft(
        doc_path=doc_path,
        page=page,
        quote=quote[:MAX_QUOTE],
        extractor="linker",
        confidence=p,
    )


def _review(
    incident: ResolvedNode, reason: str, p: float | None = None, **more: Any
) -> ReviewItem:
    return ReviewItem(
        kind="link",
        doc_paths=list(incident.sources),
        payload={"incident": incident.id, "reason": reason, **more},
        confidence=p,
    )


def _link_incident(
    incident: ResolvedNode, graph: _Graph, decider: Decider
) -> tuple[ResolvedEdge | None, ReviewItem | None]:
    machine = next(iter(graph.targets(incident.id, "OCCURRED_ON")), None)
    if machine is None:
        return None, _review(incident, "no machine")
    modes = graph.failure_modes_of(machine)
    if not modes:
        return None, _review(incident, "no failure mode for the machine")
    question = questions.root_cause_question({m: graph.option_text(m) for m in modes})
    alarms = sorted(graph.targets(incident.id, "TRIGGERED_BY"))
    state = questions.root_cause_state(
        graph.summary(incident), [graph.alarm_facts(a) for a in alarms]
    )
    verdict = questions.ask(
        decider, state, "root_cause", question, questions.PURPOSES["root_cause"]
    )
    answer = verdict.answer
    if verdict.p is None or answer is None or answer.choice not in question.options:
        return None, _review(incident, verdict.reason or questions.NO_DECISION)
    choice, p = answer.choice, verdict.p
    if choice == questions.UNKNOWN:
        return None, None
    band = questions.link_band(p)
    if band == "link":
        edge = ResolvedEdge(
            src=incident.id,
            rel="ROOT_CAUSE",
            dst=choice,
            props={},
            evidence=[_evidence(incident, p)],
        )
        return edge, None
    if band == "review":
        ranked = sorted(
            ((k, v) for k, v in answer.probabilities.items() if k in question.options),
            key=lambda item: (-item[1], item[0]),
        )[:2]
        options = [{"failure_mode": key, "p": value} for key, value in ranked]
        return None, _review(incident, "unsure root cause", p, options=options)
    return None, None


def link(resolution: Resolution, decider: Decider) -> Resolution:
    """Add root causes to the incidents and count the support for each alarm cause."""
    result = resolution.model_copy(deep=True)
    graph = _Graph(result)
    for incident in result.nodes:
        if incident.type != "Incident" or graph.targets(incident.id, "ROOT_CAUSE"):
            continue
        edge, item = _link_incident(incident, graph, decider)
        if edge is not None:
            result.edges.append(edge)
            graph.out[edge.src, edge.rel].append(edge)
        if item is not None:
            result.review.append(item)

    # Count how often each alarm and cause pair was seen in incidents.
    seen: dict[tuple[str, str], list[ResolvedNode]] = defaultdict(list)
    for incident in result.nodes:
        if incident.type != "Incident":
            continue
        for alarm in graph.targets(incident.id, "TRIGGERED_BY"):
            for mode in graph.targets(incident.id, "ROOT_CAUSE"):
                seen[alarm, mode].append(incident)
    listed = set()
    for edge in result.edges:
        if edge.rel == "INDICATES":
            listed.add((edge.src, edge.dst))
            count = len(seen.get((edge.src, edge.dst), []))
            edge.props = {**edge.props, "support": count}
    for (alarm, mode), found in sorted(seen.items()):
        if (alarm, mode) in listed:
            continue
        result.review.append(
            ReviewItem(
                kind="conflict",
                doc_paths=sorted({doc for inc in found for doc in inc.sources}),
                payload={
                    "reason": "history pair not in any manual",
                    "alarm": alarm,
                    "failure_mode": mode,
                    "count": len(found),
                },
            )
        )
    result.edges.sort(key=lambda e: (e.src, e.rel, e.dst))
    return result
