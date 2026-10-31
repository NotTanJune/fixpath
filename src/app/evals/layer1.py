"""Layer 1 of the evals: how much of the hidden truth the knowledge graph recovered.

It scores precision, recall and F1 for each node type and each relation, how accurate
the properties are on matched nodes and edges, and, for each raw file, whether the graph
credits that file with the right things. A ratio with nothing behind it is None, never a
made-up number.

evaluate() builds the whole section. node_metrics() and edge_metrics() score the graph
structure, property_accuracy() scores the values, and document_metrics() scores each
kind of document.
"""

from collections import defaultdict
from typing import Any

from app.evals.match import Matching, match
from app.evals.metrics import prf, ratio
from app.evals.truth import Graph, Truth

# The node properties whose accuracy is reported, as (node type, property).
NODE_PROPERTIES = (
    ("Part", "qty_on_hand"),
    ("Part", "bin"),
    ("Part", "stock_as_of"),
    ("AlarmCode", "severity"),
    ("Procedure", "requires_loto"),
    ("Procedure", "est_minutes"),
    ("Incident", "date"),
    ("Incident", "shift"),
    ("Incident", "downtime_min"),
)
# The edge properties whose accuracy is reported, as (relation, property).
EDGE_PROPERTIES = (("RAISED", "count"), ("USED", "qty"), ("INDICATES", "support"))
# A downtime within this many minutes of the truth counts as a near hit.
DOWNTIME_BAND = 30
# The maintenance export is reported apart from the loose notes of the same type.
EXPORT_EXTRACTOR = "csv-parser"
EXPORT_DOC_TYPE = "maintenance_export"

# The node types a document is scored on, and the name of each in a report.
DOC_CATEGORIES = {
    "Asset": "assets",
    "AlarmCode": "alarm_codes",
    "Part": "parts",
    "FailureMode": "failure_modes",
    "Procedure": "procedures",
}
# Every file is scored on these. Other categories only when the truth lists them.
ALWAYS_SCORED = ("assets", "alarm_codes", "parts")


def node_metrics(truth: Truth, graph: Graph, matching: Matching) -> dict[str, Any]:
    """Precision, recall and F1 for the nodes of each type, after matching."""
    result = {}
    types = sorted({n.type for n in [*graph.nodes.values(), *truth.nodes.values()]})
    for node_type in types:
        in_graph, in_truth = graph.of_type(node_type), truth.of_type(node_type)
        tp = sum(1 for i in in_graph if i in matching.ids)
        result[node_type] = prf(tp, len(in_graph) - tp, len(in_truth) - tp)
    return result


def edge_metrics(truth: Truth, graph: Graph, matching: Matching) -> dict[str, Any]:
    """Precision, recall and F1 for the edges of each relation, after matching. An edge
    with an unmatched end counts as a false positive."""
    truth_keys = {e.key for e in truth.edges}
    found: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    predicted: dict[str, int] = defaultdict(int)
    for edge in graph.edges:
        predicted[edge.rel] += 1
        mapped = matching.map_edge(edge)
        if mapped is not None and mapped in truth_keys:
            found[edge.rel].add(mapped)
    expected: dict[str, int] = defaultdict(int)
    for edge in truth.edges:
        expected[edge.rel] += 1
    result = {}
    for rel in sorted({*predicted, *expected}):
        tp = len(found[rel])
        result[rel] = prf(tp, predicted[rel] - tp, expected[rel] - tp)
    return result


def _accuracy(pairs: list[tuple[Any, Any]], band: int | None = None) -> dict[str, Any]:
    """Score pairs of (graph value, truth value). Pairs with no truth value are skipped.
    A missing graph value counts as missing, not as wrong."""
    scored = [(g, t) for g, t in pairs if t is not None]
    missing = sum(1 for g, _ in scored if g is None)
    correct = sum(1 for g, t in scored if g is not None and g == t)
    result: dict[str, Any] = {
        "n": len(scored),
        "correct": correct,
        "wrong": len(scored) - correct - missing,
        "missing": missing,
        "accuracy": ratio(correct, len(scored)),
    }
    if band is not None:
        near = sum(
            1 for g, t in scored if isinstance(g, (int, float)) and abs(g - t) <= band
        )
        result["within_30_count"] = near
        result["within_30"] = ratio(near, len(scored))
    return result


def property_accuracy(truth: Truth, graph: Graph, matching: Matching) -> dict[str, Any]:
    """Accuracy of the listed node properties on matched nodes and of the listed edge
    properties on matched edges."""
    result = {}
    for node_type, name in NODE_PROPERTIES:
        pairs = [
            (graph.nodes[g].props.get(name), truth.nodes[t].props.get(name))
            for g, t in matching.ids.items()
            if graph.nodes[g].type == node_type
        ]
        band = DOWNTIME_BAND if name == "downtime_min" else None
        result[f"{node_type}.{name}"] = _accuracy(pairs, band)
    truth_edges = {e.key: e for e in truth.edges}
    for rel, name in EDGE_PROPERTIES:
        pairs = []
        for edge in graph.edges:
            mapped = matching.map_edge(edge)
            if edge.rel == rel and mapped in truth_edges:
                pairs.append(
                    (edge.props.get(name), truth_edges[mapped].props.get(name))
                )
        result[f"{rel}.{name}"] = _accuracy(pairs)
    result["RAISED.count"]["events"] = _raised_events(truth, graph, matching)
    return result


def _raised_events(truth: Truth, graph: Graph, matching: Matching) -> dict[str, Any]:
    """How many of the truth's alarm events the graph counted. The exact-count accuracy
    next to it is harsh on logs whose cut-off lines lose a few events for good."""
    truth_edges = {e.key: e for e in truth.edges if e.rel == "RAISED"}
    in_graph = 0
    for edge in graph.edges:
        mapped = matching.map_edge(edge)
        if edge.rel == "RAISED" and mapped in truth_edges:
            count = edge.props.get("count")
            in_graph += count if isinstance(count, (int, float)) else 0
    in_truth = sum(e.props.get("count") or 0 for e in truth_edges.values())
    return {"graph": in_graph, "truth": in_truth, "recall": ratio(in_graph, in_truth)}


def _attributed(graph: Graph, matching: Matching) -> dict[str, dict[str, set[str]]]:
    """The true ids the graph credits to each document, by category. A node with no match
    keeps its own id behind a question mark, so it never equals a true id."""
    found: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    def add(doc_path: str, node_id: str) -> None:
        node = graph.nodes.get(node_id)
        category = DOC_CATEGORIES.get(node.type) if node else None
        if category:
            found[doc_path][category].add(matching.ids.get(node_id, f"?{node_id}"))

    for node_id, items in graph.node_evidence.items():
        for item in items:
            add(item.doc_path, node_id)
    for (src, _rel, dst), items in graph.edge_evidence.items():
        for item in items:
            add(item.doc_path, src)
            add(item.doc_path, dst)
    return found


def _export_docs(graph: Graph) -> set[str]:
    """The documents whose evidence comes from the maintenance export reader."""
    found = {
        item.doc_path
        for items in [*graph.node_evidence.values(), *graph.edge_evidence.values()]
        for item in items
        if item.extractor == EXPORT_EXTRACTOR
    }
    return {path for path in found if graph.documents.get(path) == "maintenance_note"}


def document_metrics(truth: Truth, graph: Graph, matching: Matching) -> dict[str, Any]:
    """Recall and precision for each kind of document. For each raw file, the ids the
    graph credits to it are compared with the truth, and the counts are summed by type.
    The maintenance export counts as its own type."""
    attributed = _attributed(graph, matching)
    exports = _export_docs(graph)
    totals: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0, 0])
    )
    docs: dict[str, int] = defaultdict(int)
    for doc_path, entry in sorted(truth.mentions.items()):
        doc_type = graph.documents.get(doc_path, "unknown")
        if doc_path in exports:
            doc_type = EXPORT_DOC_TYPE
        docs[doc_type] += 1
        for category in DOC_CATEGORIES.values():
            expected = getattr(entry, category)
            if expected is None and category not in ALWAYS_SCORED:
                continue
            real, got = set(expected or ()), attributed[doc_path][category]
            counts = totals[doc_type][category]
            counts[0] += len(real & got)
            counts[1] += len(got - real)
            counts[2] += len(real - got)
    return {
        doc_type: {
            "docs": docs[doc_type],
            **{c: prf(*v) for c, v in sorted(cats.items())},
        }
        for doc_type, cats in sorted(totals.items())
    }


def _matching_summary(matching: Matching, graph: Graph) -> dict[str, Any]:
    return {
        node_type: {
            "matched": len(graph.of_type(node_type))
            - len(matching.unmatched_graph[node_type]),
            "unmatched_graph": matching.unmatched_graph[node_type],
            "unmatched_truth": matching.unmatched_truth[node_type],
        }
        for node_type in sorted(matching.unmatched_graph)
    }


def evaluate(
    truth: Truth, graph: Graph, matching: Matching | None = None
) -> dict[str, Any]:
    """The whole layer 1 section."""
    matching = matching or match(truth, graph)
    return {
        "nodes": node_metrics(truth, graph, matching),
        "edges": edge_metrics(truth, graph, matching),
        "properties": property_accuracy(truth, graph, matching),
        "documents": document_metrics(truth, graph, matching),
        "matching": _matching_summary(matching, graph),
    }
