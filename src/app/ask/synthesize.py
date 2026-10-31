"""Writes the short summary and the cited claims on the answer card.

A model gets the question, the graph facts and the manual passages, each source with an
id such as C1. It writes a plain summary and a few claims that explain why the top
cause is likely. Code keeps only claims that cite a real source. It also drops any
claim that repeats what the card already shows from the graph, such as a step or a
part number. Dropped text goes to the trace, never to the technician.

synthesize is the entry point.
"""

import re
from collections.abc import Sequence
from typing import NamedTuple

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from app.ask.types import (
    Citation,
    Claim,
    GraphFacts,
    ManualPassages,
    Triage,
)
from app.config import Settings
from app.llm import call_config, structured

PURPOSE = "synthesize"
MAX_CLAIMS = 6
MAX_SENTENCES = 3
MIN_STEP_CHARS = 12  # a shorter step is too generic to count as repeated

SYSTEM_PROMPT = """\
You write the explanation on the answer card of a maintenance assistant for a \
precision-machining plant. A technician asked a question. You are given the question, \
what was understood from it, facts from the plant's knowledge graph and passages from \
the service manual. Every source has an id in square brackets, such as [C3].

Write:
- summary: at most 3 plain sentences that say what is most likely wrong and what to \
check first.
- claims: 2 to 6 short statements that explain why the top cause is likely and what to \
check first. Each claim cites one or more source ids, and only ids you were given. \
Every claim must be something its cited sources say.

Do not write repair steps, part numbers, quantities, stock or times: the card shows \
those from the graph. Do not mention a source that you were not given.
"""


class DraftClaim(BaseModel):
    text: str = Field(description="One short statement.")
    citations: list[str] = Field(
        description="The ids of the sources that back it, such as C1."
    )


class Synthesis(BaseModel):
    summary: str = Field(description="At most 3 plain sentences.")
    claims: list[DraftClaim] = Field(
        description="2 to 6 claims, each with its source ids."
    )


class Synthesized(NamedTuple):
    """The result of synthesize: the summary, the kept claims, the numbered sources, notes
    for the technician, and a developer note for each claim or sentence left out."""

    summary: str
    claims: list[Claim]
    citations: dict[str, Citation]
    notes: list[str]
    dropped: Sequence[str] = ()  # a tuple, because a default list would be shared


# --- numbering the sources ------------------------------------------------------------


def _key(citation: Citation) -> tuple:
    if citation.chunk_id:
        return ("chunk", citation.chunk_id)
    return ("quote", citation.doc_path, citation.page, citation.quote)


def _fact_citations(facts: GraphFacts) -> list[Citation]:
    groups = (facts.causes, facts.fixes, facts.breakdowns, facts.parts, facts.history)
    return [c for group in groups for item in group for c in item.citations]


def _numbered(
    facts: GraphFacts, passages: ManualPassages
) -> tuple[dict[str, Citation], dict[tuple, str]]:
    """Number each source once. A manual passage cited by both a fact and a search hit
    keeps its full text, because claims are checked against that."""
    full = {
        p.chunk_id: Citation(
            doc_path=p.doc_path, page=p.page, chunk_id=p.chunk_id, quote=p.text
        )
        for p in passages.passages
    }
    cited: dict[str, Citation] = {}
    ids: dict[tuple, str] = {}
    held = _fact_citations(facts) + list(full.values())
    for citation in held:
        key = _key(citation)
        if key in ids:
            continue
        ids[key] = f"C{len(cited) + 1}"
        cited[ids[key]] = (
            full.get(citation.chunk_id, citation) if citation.chunk_id else citation
        )
    return cited, ids


def number_citations(
    facts: GraphFacts, passages: ManualPassages
) -> dict[str, Citation]:
    """Number every source as C1, C2 and so on, skipping repeats."""
    return _numbered(facts, passages)[0]


# --- the prompt -----------------------------------------------------------------------


def _tags(citations: list[Citation], ids: dict[tuple, str]) -> str:
    found = dict.fromkeys(ids[_key(c)] for c in citations)
    return " " + "".join(f"[{cid}]" for cid in found) if found else ""


def _prompt(
    question: str,
    triage: Triage,
    facts: GraphFacts,
    passages: ManualPassages,
    ids: dict[tuple, str],
) -> str:
    lines = [
        f"Question: {question}",
        f"Understood: machine {triage.asset_id or 'unknown'}, alarm "
        f"{triage.alarm_code or 'none'}, suspected root cause "
        f"{triage.failure_mode_id or 'unknown'}, intent {triage.intent}.",
        "",
        "Facts from the knowledge graph:",
    ]
    for rank, cause in enumerate(facts.causes, start=1):
        lines.append(
            f"- Cause {rank}: {cause.name}. The manual rates it {cause.likelihood:.0%} likely. "
            f"On this machine {cause.support} of {cause.of_total} past incidents with this "
            f"alarm had this cause.{_tags(cause.citations, ids)}"
        )
    for fix in facts.fixes:
        lines.append(f"- Fix: {fix.title}.{_tags(fix.citations, ids)}")
    for item in facts.history:
        lines.append(
            f"- Alarm {item.code} came {item.count} times and started {item.incidents} "
            f"past incidents.{_tags(item.citations, ids)}"
        )
    for past in facts.breakdowns:
        done = f" Fixed by: {past.resolution_text}." if past.resolution_text else ""
        who = f" ({past.technician})" if past.technician else ""
        lines.append(
            f"- Past breakdown {past.date}: {past.symptom_text}.{done}{who}"
            f"{_tags(past.citations, ids)}"
        )
    if not (facts.causes or facts.fixes or facts.history or facts.breakdowns):
        lines.append("- none found")
    lines += ["", "Passages from the service manual:"]
    for passage in passages.passages:
        page = f" page {passage.page}" if passage.page is not None else ""
        key = ("chunk", passage.chunk_id)
        lines.append(f"[{ids[key]}] {passage.doc_path}{page}: {passage.text}")
    if not passages.passages:
        lines.append("none found")
    return "\n".join(lines)


# --- reading Sol's answer ---------------------------------------------------------------


def _normal(text: str) -> str:
    return " ".join(text.casefold().replace("_", " ").split()).rstrip(".")


HISTORY_RESTATED = "the graph's history count, which the card shows"
_PAST = re.compile(
    r"\b(?:past|previous|prior|earlier|recorded|history|historical)\b", re.I
)
_INCIDENT = re.compile(r"\b(?:incidents?|breakdowns?)\b", re.I)
_ATTRIBUTION = re.compile(
    r"\b(?:attribut\w*|accounted|caus(?:e|ed|es|ing|al)|due to|traced to"
    r"|(?:predominantly|mostly|mainly)\s+involved)\b",
    re.I,
)


def _attributes_history(text: str) -> bool:
    """Whether the text blames past incidents on a cause. The graph already shows that."""
    return bool(
        _PAST.search(text) and _INCIDENT.search(text) and _ATTRIBUTION.search(text)
    )


def _restates(text: str, facts: GraphFacts) -> str | None:
    """What the claim repeats from the card (a part, a step or the history), if anything."""
    if _attributes_history(text):
        return HISTORY_RESTATED
    lowered = text.casefold()
    for part in facts.parts:
        if part.part_no.casefold() in lowered:
            return f"part {part.part_no}"
    normal = _normal(text)
    for fix in facts.fixes:
        for step in fix.steps:
            if len(step) >= MIN_STEP_CHARS and _normal(step) in normal:
                return f"a step of {fix.procedure_id}"
    return None


def _ids(raw: list[str]) -> list[str]:
    """Clean up source ids: split on commas, drop brackets, keep each once."""
    pieces = (piece for item in raw for piece in item.split(","))
    return list(
        dict.fromkeys(
            p.strip().strip("[]").strip().upper() for p in pieces if p.strip(" []")
        )
    )


def _summary(text: str, facts: GraphFacts, dropped: list[str]) -> str:
    """The first few summary sentences that do not repeat a part or a step."""
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        restated = _restates(sentence, facts)
        if restated:
            dropped.append(
                f'dropped a summary sentence that restates {restated}: "{sentence}"'
            )
        elif sentence and len(kept) < MAX_SENTENCES:
            kept.append(sentence)
    return " ".join(kept)


def synthesize(
    question: str,
    triage: Triage,
    facts: GraphFacts,
    passages: ManualPassages,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
) -> Synthesized:
    """Ask the model for a summary and claims, and keep the claims that hold up.

    With no sources to cite, the model is not asked.
    """
    cited, ids = _numbered(facts, passages)
    if not cited:
        return Synthesized(
            "", [], {}, ["no source was found, so no summary was written"]
        )
    user = _prompt(question, triage, facts, passages, ids)
    if model is None:
        drafted = structured(
            "sol", Synthesis, SYSTEM_PROMPT, user, PURPOSE, "low", settings=settings
        )
    else:
        drafted = model.with_structured_output(Synthesis).invoke(
            [("system", SYSTEM_PROMPT), ("user", user)],
            config=call_config(PURPOSE, settings),
        )
    notes: list[str] = []
    dropped: list[str] = []
    claims: list[Claim] = []
    for draft in drafted.claims:
        text = " ".join(draft.text.split())
        wanted = _ids(draft.citations)
        missing = [cid for cid in wanted if cid not in cited]
        if not text:
            continue
        if not wanted or missing:
            why = (
                f"cites {', '.join(missing)}, which does not exist"
                if missing
                else "cites no source"
            )
            dropped.append(f'dropped a claim that {why}: "{text}"')
            continue
        restated = _restates(text, facts)
        if restated:
            dropped.append(f'dropped a claim that restates {restated}: "{text}"')
            continue
        if len(claims) == MAX_CLAIMS:
            dropped.append(f"kept the first {MAX_CLAIMS} claims and dropped the rest")
            break
        claims.append(
            Claim(id=f"claim-{len(claims) + 1}", text=text, citation_ids=wanted)
        )
    return Synthesized(
        _summary(drafted.summary, facts, dropped), claims, cited, notes, dropped
    )
