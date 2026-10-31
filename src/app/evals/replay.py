"""Rebuilds the inputs that layer 2 needs by replaying the extractors on the raw files.

It recovers the incident records, the first mention of each reference, the aliases a
candidate had when a decision was made, and the questions the resolver asked. With the
saved model answers in place, replaying costs nothing.

replay_extractions() runs the extractors. records_from() and first_mentions() read their
output. resolver_questions() records what the resolver asks the decider.
"""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.globals import get_llm_cache

from app import llm_cache
from app.config import RAW_DIR, Settings
from app.decide import Decider
from app.evals.labels import Record
from app.evals.truth import Graph
from app.ingest import parsers, questions, router
from app.ingest.load import raw_files
from app.ingest.normalize import AliasTable
from app.ingest.resolve import resolve
from app.ingest.types import Extraction, is_missing


def _first_value(values: list[Any]) -> Any:
    """The first real value, or else the first value that is not None. A placeholder
    such as NA is kept when nothing better exists, since it may be a technician's short
    form."""
    real = [v for v in values if not is_missing(v)]
    if real:
        return real[0]
    return next((v for v in values if v is not None), None)


def records_from(
    extractions: list[Extraction], aliases: AliasTable
) -> dict[tuple[str, str], Record]:
    """The incident records of the extractions, by (document path, key). Draft nodes with
    one key form one record. The machine is looked up through the alias table, or is
    None."""
    found: dict[tuple[str, str], Record] = {}
    for extraction in extractions:
        drafts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in extraction.nodes:
            if node.ref.type == "Incident" and not node.canonical:
                drafts[node.ref.key].append(node.props)
        for key, props in drafts.items():
            machine = None
            for edge in extraction.edges:
                if (
                    edge.src.type == "Incident"
                    and edge.src.key == key
                    and edge.rel == "OCCURRED_ON"
                ):
                    text = edge.dst.key
                    machine = aliases.asset_for(text) or (
                        text if text in aliases.tags else None
                    )
                    break

            def value(name: str) -> Any:
                return _first_value([p.get(name) for p in props])

            found[extraction.doc_path, key] = Record(
                doc_path=extraction.doc_path,
                key=key,
                date=value("date"),
                machine=machine,
                technician=value("technician"),
                downtime_min=value("downtime_min"),
            )
    return found


def replay_extractions(
    raw_dir: Path = RAW_DIR, settings: Settings | None = None
) -> tuple[list[Extraction], AliasTable]:
    """Run the extractors over the raw folder in processing order, as ingestion does,
    and return the extractions with the machine alias table. It turns on the default
    model cache if none is set, so a note or manual is never sent to the model. A cache
    that is already set is left alone."""
    if get_llm_cache() is None:
        llm_cache.enable_cache()
    files = raw_files(Path(raw_dir))
    workbook = next((p for p in files if p.suffix.lower() in (".xlsx", ".xlsm")), None)
    aliases = (
        parsers.build_alias_table(workbook, settings) if workbook else AliasTable([])
    )
    extractions = []
    for path in router.ordered(files):
        extractor = router.extractor_for(path)
        if extractor is not None:
            extractions.append(extractor(path, aliases, settings=settings))
    return extractions, aliases


def first_mentions(
    extractions: list[Extraction],
) -> dict[tuple[str, str], tuple[str, str]]:
    """The first occurrence of each (type, text) reference, as (document path, quote).
    The quote is the first evidence item of the node or edge, which is what the resolver
    passes to the decider. Canonical nodes and the incident end of an edge are skipped."""
    found: dict[tuple[str, str], tuple[str, str]] = {}

    def add(ref_type: str, text: str, doc_path: str, evidence: list[Any]) -> None:
        found.setdefault(
            (ref_type, text), (doc_path, evidence[0].quote if evidence else "")
        )

    for extraction in extractions:
        doc = extraction.doc_path
        for node in extraction.nodes:
            if not node.canonical and node.ref.type != "Incident":
                add(node.ref.type, node.ref.key, doc, node.evidence)
        for edge in extraction.edges:
            add(edge.dst.type, edge.dst.key, doc, edge.evidence)
            if edge.src.type != "Incident":
                add(edge.src.type, edge.src.key, doc, edge.evidence)
    return found


def aliases_at(
    graph: Graph, candidate_id: str, merges: list[dict[str, Any]], index: int
) -> list[str]:
    """The aliases a candidate had when decision number index was made. The graph holds
    the final aliases, so the texts added by this decision and later ones are removed."""
    later = {
        str(m["mention"]).casefold()
        for m in merges[index:]
        if m.get("decision") == "merge" and m.get("candidate") == candidate_id
    }
    node = graph.nodes[candidate_id]
    return [a for a in node.props.get("aliases", []) if str(a).casefold() not in later]


@dataclass(frozen=True)
class AskedEntity:
    """A same_entity question the resolver asked: the state it sent, the confidence that
    came back (None for no decision), and the backend."""

    state: dict[str, Any]
    p: float | None
    backend: str | None


class RecordingDecider:
    """Passes every question to a decider and records the same_entity ones with their
    answers, in order."""

    def __init__(self, inner: Decider):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.asked: list[AskedEntity] = []

    def ask(
        self, state: Any, questions_: dict[str, Any], purpose: str
    ) -> dict[str, Any]:
        answers = self.inner.ask(state, questions_, purpose)
        if "same_entity" in questions_:
            answer = answers.get("same_entity")
            self.asked.append(
                AskedEntity(
                    state,
                    questions.probability(answer) if answer is not None else None,
                    (answer.backend or None) if answer is not None else None,
                )
            )
        return answers


def resolver_questions(
    extractions: list[Extraction], aliases: AliasTable, decider: Decider
) -> list[AskedEntity]:
    """Every same_entity question the resolver asks when it resolves the extractions, in
    order. With a decider that only replays saved answers this is free, and a question
    that is not saved raises an error instead of costing money."""
    recorder = RecordingDecider(decider)
    resolve(extractions, aliases, recorder)
    return recorder.asked
