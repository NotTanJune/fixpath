"""Matches the ids in the built graph to the ids in the hidden truth, one to one.

The graph invents some of its own ids, so scores cannot compare ids directly. Assets,
alarm codes, parts, procedures and lines share ids on both sides. Failure modes match
on the procedures and alarm codes they connect to, with the name as a tie-breaker.
Components match by name within one machine. Incidents match by machine and date, then
by downtime and symptom. Each kind is matched greedily, best score first.

match() returns the full mapping and what is left unmatched on each side. Edges map
through their end points, and an edge with an unmatched end maps to nothing.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from rapidfuzz import fuzz, utils

from app.evals.truth import Edge, Graph, Node, Truth

BY_ID = ("Asset", "AlarmCode", "Part", "Procedure", "Line")
COMPONENT_MIN_SCORE = 60
INCIDENT_DAYS = 1
# A downtime difference of this many minutes or more counts as not close at all.
DOWNTIME_SPAN = 120
# How close two downtimes count as when one of them is unknown.
UNKNOWN_DOWNTIME = 0.5


@dataclass
class Matching:
    """ids maps each graph id to its true id. The unmatched lists are grouped by type."""

    ids: dict[str, str] = field(default_factory=dict)
    unmatched_graph: dict[str, list[str]] = field(default_factory=dict)
    unmatched_truth: dict[str, list[str]] = field(default_factory=dict)

    def truth_id(self, graph_id: str) -> str | None:
        return self.ids.get(graph_id)

    @cached_property
    def _graph_ids(self) -> dict[str, str]:
        """The true id to graph id table, built on first use."""
        return {t: g for g, t in self.ids.items()}

    def graph_id(self, truth_id: str) -> str | None:
        return self._graph_ids.get(truth_id)

    def map_edge(self, edge: Edge) -> tuple[str, str, str] | None:
        """The edge in true ids as (src, rel, dst), or None when an end has no match."""
        src, dst = self.ids.get(edge.src), self.ids.get(edge.dst)
        if src is None or dst is None:
            return None
        return (src, edge.rel, dst)


def _name_score(a: str, b: str) -> float:
    return fuzz.WRatio(a, b, processor=utils.default_process)


def _assign(candidates: list[tuple[Any, str, str]]) -> dict[str, str]:
    """Match one to one, greedily. Each candidate is (sort key, graph id, true id), and
    the smallest key goes first."""
    taken_graph: set[str] = set()
    taken_truth: set[str] = set()
    result: dict[str, str] = {}
    for _key, graph_id, truth_id in sorted(
        candidates, key=lambda c: (c[0], c[2], c[1])
    ):
        if graph_id in taken_graph or truth_id in taken_truth:
            continue
        taken_graph.add(graph_id)
        taken_truth.add(truth_id)
        result[graph_id] = truth_id
    return result


def match_by_id(truth: Truth, graph: Graph) -> dict[str, str]:
    return {
        node_id: node_id
        for node_type in BY_ID
        for node_id in graph.of_type(node_type)
        if node_id in truth.of_type(node_type)
    }


def _anchors(side: Truth | Graph, failure_mode: str) -> set[tuple[str, str]]:
    found = {
        ("procedure", e.dst)
        for e in side.edges
        if e.src == failure_mode and e.rel == "RESOLVED_BY"
    }
    found |= {
        ("alarm", e.src)
        for e in side.edges
        if e.dst == failure_mode and e.rel == "INDICATES"
    }
    return found


def match_failure_modes(truth: Truth, graph: Graph) -> dict[str, str]:
    candidates = []
    for graph_id, graph_node in graph.of_type("FailureMode").items():
        graph_anchors = _anchors(graph, graph_id)
        for truth_id, truth_node in truth.of_type("FailureMode").items():
            truth_anchors = _anchors(truth, truth_id)
            union = graph_anchors | truth_anchors
            overlap = len(graph_anchors & truth_anchors) / len(union) if union else 0.0
            if overlap == 0:
                continue
            name = _name_score(_name(graph_node), _name(truth_node))
            candidates.append(((-overlap, -name), graph_id, truth_id))
    return _assign(candidates)


def _name(node: Node) -> str:
    return str(node.props.get("name") or node.label or node.id)


def match_components(truth: Truth, graph: Graph) -> dict[str, str]:
    candidates = []
    for graph_id, graph_node in graph.of_type("Component").items():
        for truth_id, truth_node in truth.of_type("Component").items():
            if graph_node.props.get("asset_id") != truth_node.props.get("asset_id"):
                continue
            score = _name_score(_name(graph_node), _name(truth_node))
            if score >= COMPONENT_MIN_SCORE:
                candidates.append((-score, graph_id, truth_id))
    return _assign(candidates)


def as_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def _closeness(a: Any, b: Any) -> float:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return UNKNOWN_DOWNTIME
    return 1 - min(abs(a - b), DOWNTIME_SPAN) / DOWNTIME_SPAN


def match_incidents(truth: Truth, graph: Graph) -> dict[str, str]:
    by_machine: dict[str, list[tuple[str, dt.date, Node]]] = defaultdict(list)
    for truth_id, truth_node in truth.of_type("Incident").items():
        machine, day = truth.machine_of(truth_id), as_date(truth_node.props.get("date"))
        if machine is not None and day is not None:
            by_machine[machine].append((truth_id, day, truth_node))
    candidates = []
    for graph_id, graph_node in graph.of_type("Incident").items():
        machine, day = graph.machine_of(graph_id), as_date(graph_node.props.get("date"))
        if machine is None or day is None:
            continue
        for truth_id, truth_day, truth_node in by_machine.get(machine, []):
            gap = abs((day - truth_day).days)
            if gap > INCIDENT_DAYS:
                continue
            symptom = (
                _name_score(
                    str(graph_node.props.get("symptom_text") or ""),
                    str(truth_node.props.get("symptom_text") or ""),
                )
                / 100
            )
            downtime = _closeness(
                graph_node.props.get("downtime_min"),
                truth_node.props.get("downtime_min"),
            )
            score = (symptom + downtime) / 2
            candidates.append(((-score, gap), graph_id, truth_id))
    return _assign(candidates)


def match(truth: Truth, graph: Graph) -> Matching:
    """The one-to-one mapping for every node type, with what is left over."""
    ids = {
        **match_by_id(truth, graph),
        **match_failure_modes(truth, graph),
        **match_components(truth, graph),
        **match_incidents(truth, graph),
    }
    matched_truth = set(ids.values())
    result = Matching(ids=ids)
    for node_type in sorted(
        {n.type for n in [*graph.nodes.values(), *truth.nodes.values()]}
    ):
        result.unmatched_graph[node_type] = sorted(
            i for i in graph.of_type(node_type) if i not in ids
        )
        result.unmatched_truth[node_type] = sorted(
            i for i in truth.of_type(node_type) if i not in matched_truth
        )
    return result
