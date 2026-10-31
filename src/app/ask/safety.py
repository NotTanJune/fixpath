"""Builds the lockout warning that goes on an answer card.

Code writes this block, not a model, so a model cannot leave it out. It appears when a
fix needs lockout, when the question looks safety-critical, or when it is unclear
whether a hazard exists. The text comes from the machine's own manual, followed by a
fixed instruction, and cites the manual pages it used.

safety_block is the entry point.
"""

import sqlite3

from app.ask import search
from app.ask.types import Citation, Fix, GraphFacts, SafetyBlock, Triage

SAFETY_P = 0.5
HEADING = "1.2 Lockout and tagout"
FIXED_LINE = "Apply lockout and tagout before starting. Verify zero energy."
WARNING = "WARNING"
UNKNOWN_NEED = "the graph lookup failed, so the lockout need is unknown"


def _manual_chunks(conn: sqlite3.Connection, asset_id: str | None) -> list[sqlite3.Row]:
    """The machine's manual text, in reading order."""
    if asset_id is None:
        return []
    docs = sorted(search.manual_docs(conn, asset_id))
    if not docs:
        return []
    marks = ",".join("?" * len(docs))
    return conn.execute(
        "SELECT c.id, c.page, c.text, d.path FROM chunks c JOIN documents d ON d.id = c.doc_id"
        f" WHERE c.doc_id IN ({marks}) ORDER BY c.doc_id, c.page, c.ord",
        docs,
    ).fetchall()


def _paragraph(chunk: sqlite3.Row) -> str:
    """The lockout text without its heading, on one line."""
    return " ".join(chunk["text"][len(HEADING) :].split())


def _warning_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith(WARNING):
            return line.strip()
    return text.strip()


def _procedure_chunk(
    fix: Fix, chunks: list[sqlite3.Row], conn: sqlite3.Connection
) -> sqlite3.Row | None:
    """The manual text that holds the procedure's warning, or None."""
    for chunk in chunks:
        if fix.procedure_id in chunk["text"] and WARNING in chunk["text"]:
            return chunk
    for citation in fix.citations:
        if citation.chunk_id is None:
            continue
        row = conn.execute(
            "SELECT c.id, c.page, c.text, d.path FROM chunks c"
            " JOIN documents d ON d.id = c.doc_id WHERE c.id = ?",
            (citation.chunk_id,),
        ).fetchone()
        if row is not None and WARNING in row["text"]:
            return row
    return None


def safety_block(
    facts: GraphFacts,
    triage: Triage,
    conn: sqlite3.Connection,
    need_unknown: bool = False,
) -> SafetyBlock:
    """The safety block for the fixes on the card and the safety flag from triage.

    Without a known machine or a manual for it, the text is the fixed line alone.
    need_unknown forces the block when the card could not tell if lockout is needed.
    """
    lockout = [fix for fix in facts.fixes if fix.requires_loto]
    flagged = triage.safety_critical is not None and triage.safety_critical >= SAFETY_P
    machine = facts.asset_id or triage.asset_id
    unanswered = (
        machine is not None
        and triage.safety_critical is None
        and "safety_critical" in triage.confidence
        and triage.confidence["safety_critical"] is None
    )
    if not lockout and not flagged and not unanswered and not need_unknown:
        return SafetyBlock(required=False)

    reasons = []
    if lockout:
        names = ", ".join(fix.procedure_id for fix in lockout)
        reasons.append(f"{names} requires lockout and tagout")
    if flagged:
        reasons.append(
            "the question involves isolating a machine or a hazard to people"
        )
    if unanswered:
        reasons.append("the safety question could not be answered")
    if need_unknown:
        reasons.append(UNKNOWN_NEED)
    reason = "; ".join(reasons)
    reason = reason[0].upper() + reason[1:] + "."

    chunks = _manual_chunks(conn, machine)
    citations: list[Citation] = []
    paragraph = ""
    heading = next((c for c in chunks if c["text"].startswith(HEADING)), None)
    if heading is not None:
        paragraph = _paragraph(heading)
        citations.append(
            Citation(
                doc_path=heading["path"],
                page=heading["page"],
                chunk_id=heading["id"],
                quote=paragraph,
            )
        )
    for fix in lockout:
        chunk = _procedure_chunk(fix, chunks, conn)
        if chunk is not None and all(c.chunk_id != chunk["id"] for c in citations):
            citations.append(
                Citation(
                    doc_path=chunk["path"],
                    page=chunk["page"],
                    chunk_id=chunk["id"],
                    quote=_warning_line(chunk["text"]),
                )
            )
    return SafetyBlock(
        required=True,
        reason=reason,
        procedure_ids=[fix.procedure_id for fix in lockout],
        text=f"{paragraph} {FIXED_LINE}" if paragraph else FIXED_LINE,
        citations=citations,
    )
