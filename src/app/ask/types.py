"""The data shapes the troubleshooting crew passes around.

The graph tools return causes, fixes, parts, breakdowns and alarm history. Each one
carries the source quotes behind it and the graph links it was read from, so the UI can
highlight them. GraphFacts and ManualPassages hold what the two agents found. Triage is
the result of reading the question, and AnswerCard is the final answer the technician
sees.
"""

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """A place a fact was read from: a document, and the page and chunk when there
    is one, with the quote that backs the fact."""

    doc_path: str
    page: int | None = None
    chunk_id: str | None = None
    quote: str


class PathStep(BaseModel):
    """One graph edge that a fact was read from."""

    src: str
    rel: str
    dst: str


class Cause(BaseModel):
    """A root cause that an alarm usually means on a machine.

    likelihood comes from the INDICATES edge. support and of_total are history on this
    machine: of_total is the number of past incidents on it that started with the
    alarm, and support is how many of those had this root cause, so it reads as
    "support of of_total" and never exceeds it. support_model is the support that the
    INDICATES edge holds, pooled over every machine of the manual's model.
    matches_symptom is True for a cause that the crew moved to the front because triage
    read it from the technician's symptom, ahead of the history order (see rank_causes).

    A cause read with no alarm (the cause_on tool) has no INDICATES edge: likelihood is
    then the share of the machine's incidents with this root cause, and support_model
    is 0.
    """

    asset_id: str
    failure_mode_id: str
    name: str
    likelihood: float
    support: int
    of_total: int
    support_model: int
    matches_symptom: bool = False
    citations: list[Citation] = Field(default_factory=list)
    path: list[PathStep] = Field(default_factory=list)


class Fix(BaseModel):
    procedure_id: str
    title: str
    steps: list[str]
    requires_loto: bool
    est_minutes: int
    manual_ref: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    path: list[PathStep] = Field(default_factory=list)


class PartNeed(BaseModel):
    """A part a procedure needs, with the stock on hand. enough is False when the
    stock is unknown."""

    part_no: str
    description: str
    qty: int
    qty_on_hand: int | None = None
    bin: str | None = None
    lead_time_days: int | None = None
    stock_as_of: dt.date | None = None
    enough: bool
    citations: list[Citation] = Field(default_factory=list)
    path: list[PathStep] = Field(default_factory=list)


class Breakdown(BaseModel):
    """A past incident on the machine. parts_used maps a part number to the
    quantity used."""

    asset_id: str
    incident_id: str
    date: dt.date
    shift: Literal["A", "B", "C"] | None = None
    downtime_min: int | None = None
    technician: str | None = None
    symptom_text: str
    resolution_text: str | None = None
    root_cause_id: str | None = None
    parts_used: dict[str, int] = Field(default_factory=dict)
    citations: list[Citation] = Field(default_factory=list)
    path: list[PathStep] = Field(default_factory=list)


class AlarmHistory(BaseModel):
    """How often a machine raised an alarm, from the RAISED edge, and how many past
    incidents started with it."""

    asset_id: str
    code: str
    count: int
    first_seen: dt.datetime | None = None
    last_seen: dt.datetime | None = None
    incidents: int
    citations: list[Citation] = Field(default_factory=list)
    path: list[PathStep] = Field(default_factory=list)


class ToolCall(BaseModel):
    """One graph tool call. filled_by says who chose it: the agent, or the code that
    completes the facts after the agent is done."""

    name: str
    args: dict
    filled_by: Literal["agent", "code"]
    result_count: int


class Hints(BaseModel):
    """What triage picked. Any id may be None."""

    asset_id: str | None = None
    alarm_code: str | None = None
    failure_mode_id: str | None = None
    intent: str = "troubleshoot"


class GraphFacts(BaseModel):
    """What the graph agent found. Every item came from a tool's return value."""

    asset_id: str | None = None
    alarm_code: str | None = None
    causes: list[Cause] = Field(default_factory=list)
    fixes: list[Fix] = Field(default_factory=list)
    parts: list[PartNeed] = Field(default_factory=list)
    breakdowns: list[Breakdown] = Field(default_factory=list)
    history: list[AlarmHistory] = Field(default_factory=list)
    path: list[PathStep] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Passage(BaseModel):
    """A manual chunk found by search. score is the fused rank score, and matched_by
    names the methods that found it."""

    chunk_id: str
    doc_path: str
    page: int | None = None
    text: str
    score: float
    matched_by: list[Literal["keyword", "vector"]]


class ManualPassages(BaseModel):
    queries: list[str] = Field(default_factory=list)
    passages: list[Passage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --- the troubleshooting crew -----------------------------------------------------


class Chip(BaseModel):
    """One tap in the UI: an option of a field, with the probability triage gave it.
    p is None for a guess that came from the text and not from a decision."""

    field: str
    value: str
    label: str
    p: float | None = None


class Triage(BaseModel):
    """What triage made of the question.

    status says what the crew does next: act and confirm run retrieval, clarify and
    out_of_scope stop. confidence holds, per asked field (machine, alarm, root_cause,
    intent, safety_critical), the probability that was compared with the gates, or
    None when the decider gave no decision. safety_critical is the probability of yes.
    """

    asset_id: str | None = None
    alarm_code: str | None = None
    failure_mode_id: str | None = None
    intent: str = "troubleshoot"
    safety_critical: float | None = None
    confidence: dict[str, float | None] = Field(default_factory=dict)
    status: Literal["act", "confirm", "clarify", "out_of_scope"]
    chips: list[Chip] = Field(default_factory=list)
    reason: str | None = None


class Claim(BaseModel):
    """One statement of the synthesizer, with the ids (C1, C2, ...) of the citations
    that back it. The verifier fills verdict and p; reason says why a claim could not
    be judged."""

    id: str
    text: str
    citation_ids: list[str]
    verdict: Literal["supported", "partly", "unsupported"] | None = None
    p: float | None = None
    support: float | None = None
    reason: str | None = None


class SafetyBlock(BaseModel):
    """The lockout warning. Code builds it, so no model can drop it."""

    required: bool
    reason: str | None = None
    procedure_ids: list[str] = Field(default_factory=list)
    text: str = ""
    citations: list[Citation] = Field(default_factory=list)


class NodeTrace(BaseModel):
    """One node of the crew: when it ran, which model or backend answered, whether the
    answer came from a cache, and what it cost. backend and cache_hit are None for a
    node that called no model. notes are for developers (what a node dropped or why it
    failed), not for the technician."""

    name: str
    started_at: dt.datetime
    ended_at: dt.datetime
    seconds: float
    backend: str | None = None
    cache_hit: bool | None = None
    cost_usd: float = 0.0
    notes: list[str] = Field(default_factory=list)


class PartStock(BaseModel):
    """The stock of a part the question names: what is on hand, where, how long a
    reorder takes, which machines it fits, and the current stock sheet that says so."""

    part_no: str
    description: str
    qty_on_hand: int | None = None
    bin: str | None = None
    lead_time_days: int | None = None
    stock_as_of: dt.date | None = None
    fits: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class AnswerCard(BaseModel):
    """The answer the technician sees. Every fact on it (causes, steps, parts, stock,
    bins, past breakdowns, the lockout warning) was copied from the graph by code;
    only summary and the claims are model text."""

    question: str
    triage: Triage
    summary: str | None = None
    causes: list[Cause] = Field(default_factory=list)
    fix: Fix | None = None
    other_fixes: list[Fix] = Field(default_factory=list)
    parts: list[PartNeed] = Field(default_factory=list)
    breakdowns: list[Breakdown] = Field(default_factory=list)
    history: AlarmHistory | None = None
    stock: list[PartStock] = Field(default_factory=list)
    safety: SafetyBlock = Field(default_factory=lambda: SafetyBlock(required=False))
    claims: list[Claim] = Field(default_factory=list)
    unverified: list[Claim] = Field(default_factory=list)
    citations: dict[str, Citation] = Field(default_factory=dict)
    path: list[PathStep] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    trace: list[NodeTrace] = Field(default_factory=list)
