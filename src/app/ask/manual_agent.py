"""The manual agent finds the manual pages that help answer a question.

A model rewrites the question as a few short search queries in the manual's own words.
Each query, and the original question, is searched, and the results are merged into one
ranked list. The best few passages come back with their page numbers.

manual_agent is the entry point.
"""

import sqlite3

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from app import store
from app.ask import search
from app.ask.types import Hints, ManualPassages, Passage
from app.config import Settings
from app.embed import Embedder
from app.llm import call_config, structured

PURPOSE = "manual agent"
TOP_K = 5
FETCH_K = 10  # passages taken per query before merging; TOP_K are kept after
MAX_QUERIES = 3

SYSTEM_PROMPT = """\
You write search queries for the service manuals of a precision-machining plant. \
The technician asks a question; a keyword search and a meaning search then run over \
the manual pages. Write one to three short queries, in the words a manual uses. Name \
the alarm code, the component and the symptom, one query for each angle, and do not \
repeat the same words in every query. Return only the queries.
"""


class SearchPlan(BaseModel):
    queries: list[str] = Field(
        description="One to three search queries in the words of the manual."
    )


def _context(question: str, hints: Hints, conn: sqlite3.Connection) -> str:
    """The message for the model: the question and what triage found."""
    lines = [f"Question: {question}"]
    asset = store.get_node(conn, hints.asset_id) if hints.asset_id else None
    if asset:
        props = asset["props"]
        lines.append(
            f"Machine: {hints.asset_id}, {props['name']} (model {props['model']})"
        )
    elif hints.asset_id:
        lines.append(f"Machine: {hints.asset_id}")
    if hints.alarm_code:
        lines.append(f"Alarm code: {hints.alarm_code}")
    mode = (
        store.get_node(conn, hints.failure_mode_id) if hints.failure_mode_id else None
    )
    if mode:
        lines.append(f"Suspected root cause: {mode['label']}")
    lines.append(f"Intent: {hints.intent}")
    return "\n".join(lines)


def _plan(
    model: BaseChatModel | None, user: str, settings: Settings | None
) -> SearchPlan:
    if model is None:
        return structured(
            "terra", SearchPlan, SYSTEM_PROMPT, user, PURPOSE, "low", settings=settings
        )
    return model.with_structured_output(SearchPlan).invoke(
        [("system", SYSTEM_PROMPT), ("user", user)],
        config=call_config(PURPOSE, settings),
    )


def _queries(plan: SearchPlan, question: str) -> list[str]:
    """The distinct queries from the plan, plus the original question if it is new."""
    chosen: list[str] = []
    for query in (q.strip() for q in plan.queries):
        if query and query.casefold() not in {c.casefold() for c in chosen}:
            chosen.append(query)
    chosen = chosen[:MAX_QUERIES]
    raw = question.strip()
    if raw and raw.casefold() not in {c.casefold() for c in chosen}:
        chosen.append(raw)
    return chosen


def manual_agent(
    question: str,
    hints: Hints,
    conn: sqlite3.Connection,
    *,
    model: BaseChatModel | None = None,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
) -> ManualPassages:
    """Search the manuals for a question and return the best passages.

    If triage named a machine, only that machine's manual is searched. If it named only
    an alarm code, only manuals that list that code are searched.
    """
    plan = _plan(model, _context(question, hints, conn), settings)
    queries = _queries(plan, question)
    notes: list[str] = []
    if hints.asset_id and not search.manual_docs(conn, hints.asset_id):
        notes.append(f"no manual is on record for machine {hints.asset_id}")
        return ManualPassages(queries=queries, notes=notes)
    found: dict[str, Passage] = {}
    methods: dict[str, set[str]] = {}
    rankings = []
    for query in queries:
        passages = search.manual_search(
            conn,
            query,
            hints.asset_id,
            k=FETCH_K,
            embedder=embedder,
            alarm_code=hints.alarm_code,
        )
        rankings.append([p.chunk_id for p in passages])
        for passage in passages:
            found.setdefault(passage.chunk_id, passage)
            methods.setdefault(passage.chunk_id, set()).update(passage.matched_by)
    top = [
        found[chunk_id].model_copy(
            update={
                "score": score,
                "matched_by": [
                    m for m in ("keyword", "vector") if m in methods[chunk_id]
                ],
            }
        )
        for chunk_id, score in search.reciprocal_rank_fusion(rankings)[:TOP_K]
    ]
    if not top:
        notes.append("no manual passage matched the search")
    return ManualPassages(queries=queries, passages=top, notes=notes)
