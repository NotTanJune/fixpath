"""Support code for the troubleshooting crew tests.

build_crew_kg starts from the small graph in ask_stubs and adds the rows the crew needs:
safety text from the manuals, a part with no stock, and an open conflict for one past
incident. FakeDecider answers the crew's questions from a script, and the other helpers
build the scripted answers and model replies.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ask_stubs import HP_MANUAL, CNC_MANUAL, build_kg, unit
from llm_stubs import SETTINGS, SOL, StubChatModel

from app import llm, store
from app.ask.types import GraphFacts, ManualPassages, Passage
from app.decide import Answer
from app.ontology import Evidence

HP_LOTO = "HP-LOTO"
HP_WARNING = "HP-WARN"
CNC_LOTO = "CNC-LOTO"
CONFLICT_KEY = "WO-1001"
CONFLICT_INCIDENT = "INC-2026-001"
NO_STOCK_PART = "RELIEF-SPRING-1"
TOOL_CLAMP = "FM-TOOL-CLAMP"

LOTO_TEXT = {
    HP_LOTO: (
        HP_MANUAL,
        2,
        "1.2 Lockout and tagout\nSwitch off the main isolator and fit your lock and tag. "
        "The accumulator keeps stored hydraulic energy, so bleed it to 0 bar.",
        7,
    ),
    HP_WARNING: (
        HP_MANUAL,
        5,
        "4.1 Replace main cylinder seal kit\nPlant ref: PROC-HP-04 | Lockout required: yes\n"
        "WARNING: Apply lockout and tagout before starting. Verify zero energy.",
        8,
    ),
    CNC_LOTO: (
        CNC_MANUAL,
        2,
        "1.2 Lockout and tagout\nSwitch off the main isolator. Release the compressed air "
        "to the tool changer and wait until the spindle has stopped.",
        9,
    ),
}


def build_crew_kg(path: Path) -> Path:
    build_kg(path)
    conn = store.connect(path)
    try:
        _extend(conn)
    finally:
        conn.close()
    return path


def _extend(conn) -> None:
    for ord_, (chunk_id, (doc, page, text, index)) in enumerate(
        LOTO_TEXT.items(), start=50
    ):
        store.add_chunk(conn, chunk_id, doc, page, ord_, text, unit(index))
    store.upsert_node(
        conn,
        NO_STOCK_PART,
        "Part",
        "Relief valve spring",
        {
            "part_no": NO_STOCK_PART,
            "description": "Relief valve spring",
            "qty_on_hand": 0,
            "bin": "D-01",
            "lead_time_days": 21,
            "stock_as_of": "2026-10-15",
        },
    )
    store.upsert_edge(conn, "PROC-HP-04", "REQUIRES", NO_STOCK_PART, {"qty": 1})
    # the seal kit has an alias and fits the press cylinder
    store.upsert_node(
        conn, "SEAL-KIT-50", "Part", "Main cylinder seal kit", {"aliases": ["SK-50"]}
    )
    store.upsert_edge(conn, "SEAL-KIT-50", "FITS", "HP-01-MAINCYL")
    # a second alarm of the press, with a cause and a fix of its own and no parts
    store.upsert_node(
        conn,
        "FM-OIL-HOT",
        "FailureMode",
        "hydraulic oil overheating",
        {"id": "FM-OIL-HOT", "name": "hydraulic oil overheating", "mechanism": "wear"},
    )
    store.upsert_edge(conn, "FM-OIL-HOT", "AFFECTS", "HP-01-HYDPUMP")
    store.upsert_edge(
        conn, "E-221", "INDICATES", "FM-OIL-HOT", {"likelihood": 0.8, "support": 3}
    )
    store.upsert_node(
        conn,
        "PROC-HP-05",
        "Procedure",
        "Replace the hydraulic pump",
        {
            "id": "PROC-HP-05",
            "title": "Replace the hydraulic pump",
            "steps": ["Drain the tank."],
            "requires_loto": False,
            "est_minutes": 90,
            "manual_ref": "4.2",
        },
    )
    store.upsert_edge(conn, "FM-OIL-HOT", "RESOLVED_BY", "PROC-HP-05")
    # a fourth cause of E-217 on the press, so the answer must stop at three
    store.upsert_node(
        conn,
        "FM-OIL-CONTAM",
        "FailureMode",
        "hydraulic oil contamination",
        {
            "id": "FM-OIL-CONTAM",
            "name": "hydraulic oil contamination",
            "mechanism": "wear",
        },
    )
    store.upsert_edge(conn, "FM-OIL-CONTAM", "AFFECTS", "HP-01-HYDPUMP")
    store.upsert_edge(
        conn, "E-217", "INDICATES", "FM-OIL-CONTAM", {"likelihood": 0.1, "support": 0}
    )
    # a procedure that no cause of the press is resolved by
    store.upsert_node(
        conn,
        "PROC-CNC-01",
        "Procedure",
        "Replace the spindle bearing",
        {
            "id": "PROC-CNC-01",
            "title": "Replace the spindle bearing",
            "steps": ["Strip the spindle."],
            "requires_loto": False,
            "est_minutes": 300,
            "manual_ref": "5.1",
        },
    )
    store.upsert_edge(conn, "FM-SPINDLE-WEAR", "RESOLVED_BY", "PROC-CNC-01")
    store.add_evidence(
        conn,
        Evidence(
            fact_kind="node",
            fact_id=CONFLICT_INCIDENT,
            doc_id="DOC-cccccccc",
            chunk_id=None,
            page=None,
            quote=f"19/04/2026,press,pressure loss,Repl seals,SEALKIT50,R. Tan,240,{CONFLICT_KEY}",
            extractor="csv-parser",
            confidence=0.9,
        ),
    )
    store.queue_review(
        conn,
        "conflict",
        {
            "differences": {"downtime_min": [240, 250]},
            "doc_paths": ["maintenance/export.csv"],
            "key": CONFLICT_KEY,
            "reason": "a repeated record differs",
        },
    )
    store.queue_review(conn, "merge", {"key": "other"})  # not a conflict
    # the two machines that have no manual and no history, so the plant has five
    for asset, name, kind, model, vendor, alias in (
        (
            "LT-01",
            "Leak tester",
            "Pressure-decay leak tester",
            "VT-LD20",
            "Veltra Test Systems",
            "LT1",
        ),
        ("RW-01", "Robot workcell", "Robot cell", "OR-6", "Orbis Robotics", "RC-1"),
    ):
        store.upsert_node(
            conn,
            asset,
            "Asset",
            name,
            {
                "id": asset,
                "name": name,
                "machine_type": kind,
                "vendor": vendor,
                "model": model,
                "aliases": [alias],
            },
        )
        store.upsert_edge(conn, "LINE-01", "HAS_MACHINE", asset)


def add_tool_clamp(path: Path) -> Path:
    """Add a cause that no alarm points to, for a question that names no alarm.
    It is a separate step so the other tests keep their counts."""
    conn = store.connect(path)
    try:
        store.upsert_node(
            conn,
            TOOL_CLAMP,
            "FailureMode",
            "tool clamp not holding",
            {"id": TOOL_CLAMP, "name": "tool clamp not holding", "mechanism": "wear"},
        )
        store.upsert_edge(conn, TOOL_CLAMP, "AFFECTS", "CNC-01-SPINDLE")
        store.upsert_node(
            conn,
            "PROC-CNC-02",
            "Procedure",
            "Replace the tool clamp",
            {
                "id": "PROC-CNC-02",
                "title": "Replace the tool clamp",
                "steps": ["Remove the drawbar."],
                "requires_loto": True,
                "est_minutes": 120,
                "manual_ref": "5.2",
            },
        )
        store.upsert_edge(conn, TOOL_CLAMP, "RESOLVED_BY", "PROC-CNC-02")
        store.upsert_node(
            conn,
            "CLAMP-KIT-1",
            "Part",
            "Tool clamp kit",
            {
                "part_no": "CLAMP-KIT-1",
                "description": "Tool clamp kit",
                "qty_on_hand": 2,
                "bin": "E-04",
                "lead_time_days": 7,
                "stock_as_of": "2026-10-15",
            },
        )
        store.upsert_edge(conn, "PROC-CNC-02", "REQUIRES", "CLAMP-KIT-1", {"qty": 1})
    finally:
        conn.close()
    return path


def open_crew_kg(tmp_path: Path):
    from app.ask.tools import connect_ro

    return connect_ro(build_crew_kg(tmp_path / "kg.sqlite"))


# --- the decider ---


def choice(key: str, p: float, rest: dict[str, float] | None = None) -> Answer:
    """A choice answer: this key with probability p, and the other options as given."""
    return Answer(
        "choice",
        choice=key,
        probabilities={key: p, **(rest or {})},
        confidence=p,
        backend="fake",
    )


def noul(p: float) -> Answer:
    return Answer("noul", p=p, probabilities={"yes": p, "no": 1 - p}, backend="fake")


def refusal() -> Answer:
    return Answer("refusal", backend="fake")


def triage_answers(
    machine: Answer | None = None,
    alarm: Answer | None = None,
    root_cause: Answer | None = None,
    intent: Answer | None = None,
    safety: Answer | None = None,
) -> dict[str, Answer]:
    """The five triage answers. The default is a sure troubleshoot for HP-01 and E-217."""
    return {
        "machine": machine or choice("HP-01", 0.97),
        "alarm": alarm or choice("E-217", 0.95),
        "root_cause": root_cause or choice("FM-SEAL-WEAR", 0.9),
        "intent": intent or choice("troubleshoot", 0.95),
        "safety_critical": safety or noul(0.1),
    }


Script = Callable[[str, Any, dict], dict[str, Answer] | BaseException]


class RaisingModel(StubChatModel):
    """A chat model whose every call raises an error."""

    error: Any = None

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        raise self.error or RuntimeError("the provider is down")


class FakeDecider:
    """A decider that answers from a script, or raises the error the script gives.
    It records every question it is asked."""

    name = "fake"
    model = "fake"

    def __init__(self, script: Script | dict[str, Any]):
        if callable(script):
            self.script = script
        else:
            self.script = lambda purpose, state, questions: script[purpose]
        self.calls: list[tuple[str, Any, dict]] = []

    def ask(self, state, questions, purpose):
        self.calls.append((purpose, state, questions))
        result = self.script(purpose, state, questions)
        if isinstance(result, BaseException):
            raise result
        return result

    def purposes(self) -> list[str]:
        return [purpose for purpose, _, _ in self.calls]


# --- Sol ---


def sol_model(summary: str, claims: list[tuple[str, list[str]]]) -> StubChatModel:
    """A stub model whose answer is a written synthesis."""
    reply = {
        "summary": summary,
        "claims": [{"text": text, "citations": ids} for text, ids in claims],
    }
    return StubChatModel(
        model=SOL, reply=json.dumps(reply), callbacks=[llm.CostTracker(SETTINGS)]
    )


# --- facts and passages for the E-217 case ---


def hp_facts(conn) -> GraphFacts:
    """What the graph agent finds for HP-01 and E-217 on the crew graph."""
    from app.ask import tools

    causes = tools.alarm_causes(conn, "HP-01", "E-217")
    fixes = tools.fixes_for(conn, "FM-SEAL-WEAR") + tools.fixes_for(
        conn, "FM-RELIEF-MISADJUSTED"
    )
    parts = [p for fix in fixes for p in tools.parts_for(conn, fix.procedure_id)]
    breakdowns = tools.similar_breakdowns(
        conn, "HP-01", "E-217", "FM-SEAL-WEAR", "oil on the floor"
    )
    return GraphFacts(
        asset_id="HP-01",
        alarm_code="E-217",
        causes=causes,
        fixes=fixes,
        parts=parts,
        breakdowns=breakdowns,
        notes=["graph note"],
    )


def hp_passages(conn) -> ManualPassages:
    rows = conn.execute(
        "SELECT c.id, c.page, c.text, d.path FROM chunks c JOIN documents d ON d.id = c.doc_id"
        " WHERE c.id IN ('HP-3', 'HP-5') ORDER BY c.id"
    ).fetchall()
    return ManualPassages(
        queries=["seal leak"],
        passages=[
            Passage(
                chunk_id=r["id"],
                doc_path=r["path"],
                page=r["page"],
                text=r["text"],
                score=0.03,
                matched_by=["keyword"],
            )
            for r in rows
        ],
        notes=["manual note"],
    )
