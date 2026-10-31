"""Builds the answer card from the state of a finished run.

The models choose, phrase and check, but every fact on the card (causes, fix steps,
parts, stock, past breakdowns and the lockout warning) is copied from what the graph
tools returned. If the graph agent failed, this code reads the graph itself, so the
card still shows causes, fixes and parts.

build_card is the entry point. rank_causes puts the cause that matches the technician's
symptom first. The card also carries the safety block and only those claims and
summaries the verifier kept.
"""

import json
import re
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from app.ask import tools
from app.ask.safety import safety_block
from app.ask.triage import GATES
from app.ask.types import (
    AnswerCard,
    Breakdown,
    Cause,
    GraphFacts,
    PartNeed,
    PartStock,
    PathStep,
    Triage,
)
from app.ask.verify import failed_note, is_kept, partition

RETRIEVAL = ("act", "confirm")
SUMMARY_ID = "summary"
GRAPH_FAILED_NOTE = "the graph lookup failed; the card shows the manual passages only"
GRAPH_FILLED_NOTE = (
    "the graph lookup failed; the causes, fix and parts were read from the graph"
)
BY_CODE = "filled by code after the graph agent failed"
MAX_CAUSES = 3
MAX_OTHER_FIXES = 2
MAX_BREAKDOWNS = 3
MAX_HISTORY_BREAKDOWNS = 5
MAX_STOCK = 5
HISTORY = "history"
UNCHECKED_NOTE = (
    "the summary could not be checked against its sources, so it is not shown"
)


def rank_causes(causes: list[Cause], triage: Triage) -> list[Cause]:
    """Move the cause that matches the symptom to the front, when triage is confident."""
    mode, confidence = triage.failure_mode_id, triage.confidence.get("root_cause")
    if not mode or confidence is None or confidence < GATES["confirm"]:
        return causes
    alarm = (triage.alarm_code or "").strip().upper()
    match = next(
        (
            c
            for c in causes
            if c.failure_mode_id == mode
            and (not alarm or not c.path or c.path[0].src == alarm)
        ),
        None,
    )
    if match is None or match is causes[0]:
        return causes
    marked = match.model_copy(update={"matches_symptom": True})
    return [marked, *(c for c in causes if c is not match)]


def _add_path(path: list[PathStep], steps: list[PathStep]) -> None:
    path.extend(step for step in steps if step not in path)


def _stock_note(part: PartNeed) -> str:
    name = f"{part.part_no} ({part.description})"
    if part.qty_on_hand is None:
        return f"{name}: stock on hand is not known"
    note = f"{name}: not enough in stock: {part.qty_on_hand} on hand"
    if part.lead_time_days is not None:
        note += f", lead time {part.lead_time_days} days"
    return note


def _conflict_notes(conn, breakdowns: list[Breakdown]) -> list[str]:
    """A note for each breakdown whose source records disagree and await review."""
    conflicts = []
    for row in conn.execute(
        "SELECT payload FROM review_queue WHERE kind = 'conflict' AND status = 'open'"
    ):
        payload = json.loads(row["payload"])
        if payload.get("key"):
            conflicts.append(payload)
    notes = []
    for breakdown in breakdowns:
        quotes = [
            r["quote"]
            for r in conn.execute(
                "SELECT quote FROM evidence WHERE fact_kind = 'node' AND fact_id = ?",
                (breakdown.incident_id,),
            )
        ]
        for payload in conflicts:
            pattern = re.compile(rf"(?<![\w-]){re.escape(payload['key'])}(?![\w-])")
            if any(pattern.search(quote) for quote in quotes):
                detail = "; ".join(
                    f"{field}: {' vs '.join(str(v) for v in values)}"
                    for field, values in payload.get("differences", {}).items()
                )
                note = f"{breakdown.incident_id}: sources disagree on this record"
                notes.append(f"{note} ({detail})" if detail else note)
                break
    return notes


def _stock(conn, question: str) -> list[PartStock]:
    """The stock of the parts the question names."""
    found = (
        tools.part_stock(conn, part_no) for part_no in tools.find_parts(conn, question)
    )
    return [stock for stock in found if stock is not None][:MAX_STOCK]


def _causes(facts: GraphFacts, triage: Triage) -> list:
    """The top causes for the alarm in question, or for every alarm if none is known."""
    alarm = (facts.alarm_code or triage.alarm_code or "").strip().upper()
    kept = [c for c in facts.causes if not alarm or (c.path and c.path[0].src == alarm)]
    return kept[:MAX_CAUSES]


def _fixes(conn, facts: GraphFacts, causes: list) -> list:
    """The fixes for the card's causes, top cause first, each procedure once. Fixes the
    graph agent did not fetch are read from the graph here."""
    available = list(facts.fixes)
    covered = {f.path[0].src for f in available if f.path}
    for cause in causes:
        if cause.failure_mode_id not in covered:
            available += tools.fixes_for(conn, cause.failure_mode_id)
            covered.add(cause.failure_mode_id)
    fixes: list = []
    for cause in causes:
        for fix in available:
            if (
                fix.path
                and fix.path[0].src == cause.failure_mode_id
                and all(fix.procedure_id != f.procedure_id for f in fixes)
            ):
                fixes.append(fix)
    return fixes


def _read_by_code(
    conn, facts: GraphFacts, triage: Triage, trace: list[str]
) -> tuple[GraphFacts, bool]:
    """Read the facts from the graph directly after the graph agent failed. Also say
    whether it is still unknown if the repair needs lockout."""
    machine, alarm, mode = triage.asset_id, triage.alarm_code, triage.failure_mode_id
    if machine is None or (alarm is None and mode is None):
        missing = "machine" if machine is None else "alarm"
        trace.append(f"{BY_CODE}: not possible, triage knows no {missing}")
        return facts, True
    try:
        if alarm is None:  # no alarm: use the root cause triage picked
            found = tools.cause_on(conn, machine, mode)
            causes, source = [found] if found else [], f"cause_on({machine}, {mode})"
        else:
            causes, source = (
                tools.alarm_causes(conn, machine, alarm),
                (f"alarm_causes({machine}, {alarm})"),
            )
        breakdowns = tools.similar_breakdowns(
            conn, machine, alarm, mode, None, MAX_BREAKDOWNS
        )
    except sqlite3.Error as error:
        trace.append(f"{BY_CODE}: the read failed with {type(error).__name__}: {error}")
        return facts, True
    causes = rank_causes(causes, triage)
    trace.append(f"{BY_CODE}: {source} gave {len(causes)} causes")
    trace.append(
        f"{BY_CODE}: similar_breakdowns({machine}) gave {len(breakdowns)} breakdowns"
    )
    notes = [
        GRAPH_FILLED_NOTE if note == GRAPH_FAILED_NOTE else note for note in facts.notes
    ]
    filled = facts.model_copy(
        update={
            "asset_id": machine,
            "alarm_code": alarm,
            "causes": causes,
            "breakdowns": breakdowns,
            "notes": notes,
        }
    )
    return filled, not causes


def build_card(
    state: Mapping[str, Any], kg_path: Path, trace: list[str] | None = None
) -> AnswerCard:
    """Build the answer card. Notes on what code filled in are added to trace."""
    triage, question = state["triage"], state["question"]
    trace = trace if trace is not None else []
    with closing(tools.connect_ro(kg_path)) as conn:
        stock = [] if triage.status == "out_of_scope" else _stock(conn, question)
        if triage.status not in RETRIEVAL:
            safety = safety_block(GraphFacts(asset_id=triage.asset_id), triage, conn)
            return AnswerCard(
                question=question, triage=triage, safety=safety, stock=stock
            )

        facts, passages = state["facts"], state["passages"]
        failed = bool(state.get("graph_failed"))
        unknown = False
        if failed:
            facts, unknown = _read_by_code(conn, facts, triage, trace)
        synthesis, judged = state["synthesis"], state.get("judged", [])
        causes = _causes(facts, triage)
        fixes = _fixes(conn, facts, causes)
        top = causes[0].failure_mode_id if causes else None
        fix = next((f for f in fixes if f.path[0].src == top), None)
        others = [f for f in fixes if f is not fix][:MAX_OTHER_FIXES]
        parts: list[PartNeed] = []
        if fix:
            parts = [
                p for p in facts.parts if p.path and p.path[0].src == fix.procedure_id
            ]
            parts = parts or tools.parts_for(conn, fix.procedure_id)
        breakdowns, history = facts.breakdowns[:MAX_BREAKDOWNS], None
        machine = facts.asset_id or triage.asset_id
        if triage.intent == HISTORY and machine:
            alarm = facts.alarm_code or triage.alarm_code
            breakdowns = tools.similar_breakdowns(
                conn,
                machine,
                alarm,
                triage.failure_mode_id,
                None,
                MAX_HISTORY_BREAKDOWNS,
            )
            history = tools.alarm_history(conn, machine, alarm) if alarm else None
        on_card = [f for f in (fix, *others) if f is not None]
        if failed and not unknown:
            shown = ", ".join(f.procedure_id for f in on_card) or "no fix"
            trace.append(f"{BY_CODE}: fixes and parts read from the graph ({shown})")
        facts_on_card = facts.model_copy(update={"fixes": on_card})
        safety = safety_block(facts_on_card, triage, conn, need_unknown=unknown)

        summary_claim = next((c for c in judged if c.id == SUMMARY_ID), None)
        kept, unverified = partition([c for c in judged if c.id != SUMMARY_ID])
        wanted = {cid for claim in (*kept, *unverified) for cid in claim.citation_ids}
        citations = {cid: c for cid, c in synthesis.citations.items() if cid in wanted}
        path: list[PathStep] = []
        for group in (causes, on_card, parts, breakdowns, [history] if history else []):
            for item in group:
                _add_path(path, item.path)
        notes = [_stock_note(p) for p in parts if not p.enough]
        notes += _conflict_notes(conn, breakdowns)
        for note in (*facts.notes, *passages.notes, *synthesis.notes):
            if note not in notes:
                notes.append(note)
        shown = (
            synthesis.summary
            if summary_claim is not None and is_kept(summary_claim)
            else None
        )
        if synthesis.summary and shown is None:
            judged_by_verifier = summary_claim and summary_claim.verdict is not None
            notes.append(
                failed_note(summary_claim, "the summary")
                if judged_by_verifier
                else UNCHECKED_NOTE
            )
        return AnswerCard(
            question=question,
            triage=triage,
            summary=shown or None,
            causes=causes,
            fix=fix,
            other_fixes=others,
            parts=parts,
            breakdowns=breakdowns,
            history=history,
            stock=stock,
            safety=safety,
            claims=kept,
            unverified=unverified,
            citations=citations,
            path=path,
            notes=notes,
        )
