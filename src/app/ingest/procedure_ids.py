"""Gives an id to each procedure found in a standalone document.

A vendor manual prints its own procedure ids, but a procedure or work instruction in
its own document has none. mint_procedure_ids runs early in the resolver, once all
the ids already in use are known, and hands out the next free id for each machine
type. The same input always gets the same ids.
"""

import re
from collections.abc import Iterable

from app.ingest.types import CandidateNode, Extraction, NodeRef
from app.ontology import validate_id

EXTRACTOR = "sol-doc"
NO_PREFIX = "DOC"
LAST_NUMBER = 99
_ID = re.compile(r"PROC-([A-Z]{2,3})-(\d{2})")
_PREFIX = re.compile(r"[A-Z]{2,3}")


def _is_draft(node: CandidateNode) -> bool:
    return node.ref.type == "Procedure" and not node.canonical


def _used(ids: Iterable[str]) -> dict[str, int]:
    """Return the highest number in use for each prefix."""
    highest: dict[str, int] = {}
    for value in ids:
        found = _ID.fullmatch(value)
        if found:
            prefix, number = found.group(1), int(found.group(2))
            highest[prefix] = max(highest.get(prefix, 0), number)
    return highest


def mint_procedure_ids(
    extractions: list[Extraction], known: Iterable[str] = ()
) -> list[Extraction]:
    """Return the extractions with ids given to their draft procedures.

    Ids already in the graph or in the extractions count as taken. If a prefix runs
    out of numbers, the procedure and its links are dropped with a note. The input is
    not changed.
    """
    highest = _used(known)
    for extraction in extractions:
        own = (
            n.ref.key
            for n in extraction.nodes
            if n.ref.type == "Procedure" and n.canonical
        )
        for prefix, number in _used(own).items():
            highest[prefix] = max(highest.get(prefix, 0), number)

    result: list[Extraction] = []
    for extraction in extractions:
        if extraction.extractor != EXTRACTOR or not any(
            _is_draft(n) for n in extraction.nodes
        ):
            result.append(extraction)
            continue
        copy = extraction.model_copy(deep=True)
        minted: dict[str, str | None] = {}
        for node in copy.nodes:
            if not _is_draft(node):
                continue
            prefix = str(node.props.get("id_prefix") or "")
            if not _PREFIX.fullmatch(prefix):
                prefix = NO_PREFIX
            number = highest.get(prefix, 0) + 1
            if number > LAST_NUMBER:
                copy.notes.append(
                    f"every number of PROC-{prefix}-NN is used, so the procedure has no id"
                )
                minted[node.ref.key] = None
                continue
            highest[prefix] = number
            new_id = f"PROC-{prefix}-{number:02d}"
            assert validate_id("Procedure", new_id)
            minted[node.ref.key] = new_id
            node.props = {
                "id": new_id,
                **{k: v for k, v in node.props.items() if k not in ("id_prefix", "id")},
            }
            node.ref = NodeRef(type="Procedure", key=new_id)
            node.canonical = True

        copy.nodes = [
            n
            for n in copy.nodes
            if not (n.ref.type == "Procedure" and minted.get(n.ref.key, "") is None)
        ]
        edges = []
        for edge in copy.edges:
            for end in ("src", "dst"):
                ref = getattr(edge, end)
                if ref.type == "Procedure" and ref.key in minted:
                    new_id = minted[ref.key]
                    setattr(
                        edge,
                        end,
                        NodeRef(type="Procedure", key=new_id) if new_id else ref,
                    )
            if not any(
                getattr(edge, end).type == "Procedure"
                and getattr(edge, end).key in minted
                and minted[getattr(edge, end).key] is None
                for end in ("src", "dst")
            ):
                edges.append(edge)
        copy.edges = edges
        result.append(copy)
    return result
