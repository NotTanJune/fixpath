"""Builds the progress messages sent while one file is ingested.

Each function turns the result of a stage into a small summary that the interface can
show. chunked describes the pieces a file was cut into, facts the candidate facts,
and matched what the file joined to the graph and what it added.
"""

from collections.abc import Collection, Iterable

from app.ingest.resolve_mentions import MENTION_TYPES
from app.ingest.types import Extraction, Resolution

TEXT_CHARS = 200  # how much of a chunk the event carries


def chunked(extraction: Extraction) -> dict:
    """The pieces of text found in a file, each cut to its first 200 characters."""
    return {
        "doc_path": extraction.doc_path,
        "chunks": [
            {
                "id": chunk.id,
                "page": chunk.page,
                "ord": chunk.ord,
                "text": chunk.text[:TEXT_CHARS],
            }
            for chunk in extraction.chunks
        ],
    }


def facts(extraction: Extraction) -> dict:
    """The facts proposed by the file, before they are matched to the graph."""
    return {
        "nodes": [
            {"id": node.ref.key, "type": node.ref.type, "label": node.label}
            for node in extraction.nodes
        ],
        "edges": [
            {
                "id": f"{edge.src.key}|{edge.rel}|{edge.dst.key}",
                "src": edge.src.key,
                "rel": edge.rel,
                "dst": edge.dst.key,
            }
            for edge in extraction.edges
        ],
    }


def matched(
    extractions: Iterable[Extraction],
    known_ids: Collection[str],
    resolution: Resolution,
) -> dict:
    """What the file joined and what it added. The first list holds facts that matched
    something the graph already had. The second holds things that are new."""
    merged: list[dict] = []

    def add(source: str, target: str) -> None:
        pair = {"from": source, "to": target}
        if pair not in merged:
            merged.append(pair)

    extractions = list(extractions)
    for extraction in extractions:
        refs = [node.ref for node in extraction.nodes]
        for edge in extraction.edges:
            refs += [edge.src, edge.dst]
        for ref in refs:
            if ref.type != "Incident" and ref.key in known_ids:
                add(ref.key, ref.key)
    for merge in resolution.merges:
        if (
            merge.decision in ("rule", "merge")
            and merge.type in MENTION_TYPES
            and merge.candidate in known_ids
        ):
            add(merge.mention, merge.candidate)
    return {
        "merged": merged,
        "new": sorted({n.id for n in resolution.nodes if n.id not in known_ids}),
    }
