"""Support code for the ask tests.

build_kg writes a small graph into a temporary database: three machines, a few kinds of
documents, manual chunks, and past incidents. open_kg opens it read-only. The file also
holds a fake embedder, which turns a known query into a fixed vector, and a scripted chat
model that replies from a list and records the tools it was given.
"""

import datetime as dt
import json
import sqlite3
from pathlib import Path

import numpy as np
from langchain_core.messages import AIMessage
from llm_stubs import SETTINGS, TERRA, StubChatModel
from pydantic import Field

from app import llm, store
from app.embed import DIMENSIONS
from app.ontology import Evidence

HP_MANUAL, CNC_MANUAL = "DOC-aaaaaaaa", "DOC-bbbbbbbb"
NOTE, LOG, INVENTORY = "DOC-cccccccc", "DOC-dddddddd", "DOC-eeeeeeee"
OLD_INVENTORY = "DOC-ffffffff"
PATHS = {
    HP_MANUAL: ("manuals/hp.pdf", "manual"),
    CNC_MANUAL: ("manuals/cnc.pdf", "manual"),
    NOTE: ("notes/note.txt", "maintenance_note"),
    LOG: ("logs/hp.log", "log"),
    INVENTORY: ("inventory/spares.xlsx", "inventory"),
    OLD_INVENTORY: ("inventory/spares_old.csv", "inventory"),
}

# chunk id -> (document, page, text, vector index)
CHUNKS = {
    "HP-1": (HP_MANUAL, 1, "Press overview. Rated force 250 t. Daily checks.", 0),
    "HP-3": (
        HP_MANUAL,
        3,
        "Alarm E-217 main cylinder pressure low. Check the seals for wear.",
        1,
    ),
    "HP-5": (
        HP_MANUAL,
        5,
        "Replace main cylinder seal kit. Lock out the main isolator first.",
        2,
    ),
    "CNC-2": (
        CNC_MANUAL,
        2,
        "Spindle alarm E-214 over temperature. Check the bearing.",
        3,
    ),
    "CNC-4": (CNC_MANUAL, 4, "Alarm E-217 on the lathe is a coolant fault.", 4),
    "NOTE-1": (NOTE, None, "E-217 seal replaced again, oil on the floor.", 1),
}


def unit(index: int) -> np.ndarray:
    vector = np.zeros(DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector


class FakeEmbedder:
    """Turns a known query into a fixed vector. An unknown query matches no chunk."""

    def __init__(self, table: dict[str, int] | None = None):
        self.table = table or {}
        self.asked: list[str] = []

    def embed_query(self, text: str) -> np.ndarray:
        self.asked.append(text)
        return unit(self.table.get(text, 100))


def _evidence(
    conn, kind, fact_id, doc, chunk=None, page=None, quote="q", ex="sol-manual"
):
    store.add_evidence(
        conn,
        Evidence(
            fact_kind=kind,
            fact_id=fact_id,
            doc_id=doc,
            chunk_id=chunk,
            page=page,
            quote=quote,
            extractor=ex,
            confidence=0.9,
        ),
    )


def build_kg(path: Path) -> Path:
    conn = store.connect(path)
    store.init_schema(conn)
    for doc_id, (doc_path, doc_type) in PATHS.items():
        store.upsert_document(conn, doc_id, doc_path, doc_type, "0" * 64)
    for ord_, (chunk_id, (doc, page, text, index)) in enumerate(CHUNKS.items()):
        store.add_chunk(conn, chunk_id, doc, page, ord_, text, unit(index))

    node = store.upsert_node
    node(conn, "LINE-01", "Line", "Line 1", {"id": "LINE-01", "name": "Line 1"})
    for asset, name, model, vendor, aliases in (
        (
            "HP-01",
            "Hydraulic press",
            "BH-250",
            "Bramwell Hydraulics",
            ["Press 2", "the press"],
        ),
        ("CNC-01", "Lathe", "KX-500", "Kessler Werkzeugmaschinen", ["Lathe 1"]),
        ("CNC-02", "Second lathe", "KX-500", "Kessler Werkzeugmaschinen", ["Lathe 2"]),
    ):
        node(
            conn,
            asset,
            "Asset",
            name,
            {
                "id": asset,
                "name": name,
                "machine_type": "machine",
                "vendor": vendor,
                "model": model,
                "aliases": aliases,
            },
        )
        store.upsert_edge(conn, "LINE-01", "HAS_MACHINE", asset)
    for comp, asset, name in (
        ("HP-01-MAINCYL", "HP-01", "Main cylinder and seals"),
        ("HP-01-RELIEF", "HP-01", "Pressure relief valve"),
        ("HP-01-HYDPUMP", "HP-01", "Hydraulic pump"),
        ("CNC-01-SPINDLE", "CNC-01", "Spindle"),
        ("CNC-02-SPINDLE", "CNC-02", "Spindle"),
    ):
        node(
            conn, comp, "Component", name, {"id": comp, "name": name, "asset_id": asset}
        )
        store.upsert_edge(conn, asset, "HAS_COMPONENT", comp)
    for code, text in (
        ("E-217", "Pressure low"),
        ("E-221", "Oil hot"),
        ("E-214", "Hot"),
    ):
        node(
            conn,
            code,
            "AlarmCode",
            text,
            {"code": code, "text": text, "severity": "critical"},
        )
    for fm, name, comp in (
        ("FM-SEAL-WEAR", "main cylinder seal wear", "HP-01-MAINCYL"),
        ("FM-RELIEF-MISADJUSTED", "relief valve misadjustment", "HP-01-RELIEF"),
        ("FM-PUMP-WEAR", "hydraulic pump wear", "HP-01-HYDPUMP"),
        ("FM-SPINDLE-WEAR", "spindle bearing wear", "CNC-01-SPINDLE"),
    ):
        node(
            conn, fm, "FailureMode", name, {"id": fm, "name": name, "mechanism": "wear"}
        )
        store.upsert_edge(conn, fm, "AFFECTS", comp)
    # the spindle failure mode belongs to the manual, so both lathes have it
    store.upsert_edge(conn, "FM-SPINDLE-WEAR", "AFFECTS", "CNC-02-SPINDLE")
    for fm, likelihood, support in (
        ("FM-SEAL-WEAR", 0.7, 6),
        ("FM-PUMP-WEAR", 0.4, 6),
        ("FM-RELIEF-MISADJUSTED", 0.95, 1),
        ("FM-SPINDLE-WEAR", 0.99, 9),  # a cause for CNC-01 only, so HP-01 must not see it
    ):
        store.upsert_edge(
            conn,
            "E-217",
            "INDICATES",
            fm,
            {"likelihood": likelihood, "support": support},
        )
    for proc, fm, title, loto in (
        ("PROC-HP-04", "FM-SEAL-WEAR", "Replace main cylinder seal kit", True),
        ("PROC-HP-06", "FM-RELIEF-MISADJUSTED", "Reset the relief valve", False),
    ):
        node(
            conn,
            proc,
            "Procedure",
            title,
            {
                "id": proc,
                "title": title,
                "steps": ["Stop the pump.", "Do the work."],
                "requires_loto": loto,
                "est_minutes": 180,
                "manual_ref": "4.1",
            },
        )
        store.upsert_edge(conn, fm, "RESOLVED_BY", proc)
    for part, desc, on_hand, bin_ in (
        ("SEAL-KIT-50", "Main cylinder seal kit", 3, "B-12"),
        ("OIL-HLP46-20L", "Hydraulic oil 20 L", 1, "C-03"),
        ("WIPER-CYL-50", "Cylinder wiper", None, None),
    ):
        node(
            conn,
            part,
            "Part",
            desc,
            {
                "part_no": part,
                "description": desc,
                "qty_on_hand": on_hand,
                "bin": bin_,
                "lead_time_days": 14 if on_hand is not None else None,
                "stock_as_of": dt.date(2026, 10, 15) if on_hand is not None else None,
            },
        )
    for part, qty in (("SEAL-KIT-50", 1), ("OIL-HLP46-20L", 2), ("WIPER-CYL-50", 1)):
        store.upsert_edge(conn, "PROC-HP-04", "REQUIRES", part, {"qty": qty})

    incidents = (
        # id, asset, date, shift, downtime, tech, symptom, resolution, alarm, cause, parts
        (
            "INC-2026-001",
            "HP-01",
            "2026-04-19",
            "C",
            240,
            "R. Tan",
            "press losing pressure mid-stroke, oil on the floor",
            "Replaced main cylinder seal kit",
            "E-217",
            "FM-SEAL-WEAR",
            {"SEAL-KIT-50": 1, "OIL-HLP46-20L": 1},
        ),
        (
            "INC-2026-002",
            "HP-01",
            "2026-05-02",
            "A",
            120,
            "M. Lim",
            "pressure drop, oil under press",
            "Changed seals",
            "E-217",
            "FM-SEAL-WEAR",
            {},
        ),
        (
            "INC-2026-003",
            "HP-01",
            "2026-06-10",
            None,
            None,
            None,
            "relief valve chatter",
            None,
            "E-217",
            "FM-RELIEF-MISADJUSTED",
            {},
        ),
        (
            "INC-2026-004",
            "HP-01",
            "2026-07-01",
            "B",
            60,
            "R. Tan",
            "oil leak at the ram seal",
            "Fitted a new seal",
            None,
            "FM-SEAL-WEAR",
            {},
        ),
        (
            "INC-2026-005",
            "HP-01",
            "2026-08-01",
            "A",
            30,
            "M. Lim",
            "pump noisy",
            "Topped up oil",
            "E-221",
            "FM-PUMP-WEAR",
            {},
        ),
        (
            "INC-2026-006",
            "CNC-01",
            "2026-08-15",
            "A",
            45,
            "K. Ong",
            "spindle hot",
            "Cooled it",
            "E-214",
            "FM-SPINDLE-WEAR",
            {},
        ),
        (
            "INC-2026-007",
            "CNC-02",
            "2026-09-01",
            "B",
            90,
            "K. Ong",
            "spindle alarm and a smell",
            "Changed the bearing",
            "E-217",
            "FM-SPINDLE-WEAR",
            {},
        ),
    )
    for (
        inc,
        asset,
        day,
        shift,
        down,
        tech,
        symptom,
        res,
        alarm,
        cause,
        parts,
    ) in incidents:
        node(
            conn,
            inc,
            "Incident",
            inc,
            {
                "id": inc,
                "date": day,
                "shift": shift,
                "downtime_min": down,
                "symptom_text": symptom,
                "technician": tech,
                "resolution_text": res,
            },
        )
        store.upsert_edge(conn, inc, "OCCURRED_ON", asset)
        store.upsert_edge(conn, inc, "ROOT_CAUSE", cause)
        if alarm:
            store.upsert_edge(conn, inc, "TRIGGERED_BY", alarm)
        for part, qty in parts.items():
            store.upsert_edge(conn, inc, "USED", part, {"qty": qty})
        _evidence(conn, "node", inc, NOTE, quote=f"note about {inc}", ex="sol-text")

    store.upsert_edge(
        conn,
        "HP-01",
        "RAISED",
        "E-217",
        {
            "count": 15,
            "first_seen": "2026-04-18T17:19:00+00:00",
            "last_seen": "2026-09-28T09:51:40+00:00",
        },
    )

    _evidence(conn, "node", "HP-01", HP_MANUAL, "HP-1", 1, "BH-250")
    _evidence(
        conn, "node", "HP-01", INVENTORY, quote="HP-01 | Hydraulic press", ex="inv"
    )
    _evidence(conn, "node", "CNC-01", CNC_MANUAL, "CNC-2", 2, "KX-500")
    _evidence(conn, "node", "CNC-02", CNC_MANUAL, "CNC-2", 2, "KX-500")
    _evidence(conn, "node", "E-214", CNC_MANUAL, "CNC-2", 2, "E-214")  # its alarm table
    # a note quote that comes first by id, to show a manual quote still sorts before it
    _evidence(
        conn,
        "edge",
        "E-217|INDICATES|FM-SEAL-WEAR",
        NOTE,
        quote="note says seals",
        ex="sol-text",
    )
    _evidence(
        conn,
        "edge",
        "E-217|INDICATES|FM-SEAL-WEAR",
        HP_MANUAL,
        "HP-3",
        3,
        "Check the seals",
    )
    # a note quote with a chunk and a manual quote without one: the manual still comes first
    _evidence(
        conn,
        "edge",
        "E-217|INDICATES|FM-RELIEF-MISADJUSTED",
        NOTE,
        "NOTE-1",
        None,
        "note says relief",
        ex="sol-text",
    )
    _evidence(
        conn,
        "edge",
        "E-217|INDICATES|FM-RELIEF-MISADJUSTED",
        HP_MANUAL,
        None,
        3,
        "relief valve table",
    )
    _evidence(
        conn,
        "node",
        "PROC-HP-04",
        HP_MANUAL,
        "HP-5",
        5,
        "Replace main cylinder seal kit",
    )
    _evidence(
        conn,
        "edge",
        "FM-SEAL-WEAR|RESOLVED_BY|PROC-HP-04",
        HP_MANUAL,
        "HP-5",
        5,
        "seal kit",
    )
    _evidence(
        conn,
        "edge",
        "PROC-HP-04|REQUIRES|SEAL-KIT-50",
        HP_MANUAL,
        "HP-5",
        5,
        "SEAL-KIT-50",
    )
    _evidence(
        conn,
        "node",
        "SEAL-KIT-50",
        INVENTORY,
        quote="SEAL-KIT-50 | Main cylinder seal kit | 3 | pc | B12 | 14",
        ex="inv",
    )
    # a superseded sheet: the same bin, an older count
    _evidence(
        conn,
        "node",
        "SEAL-KIT-50",
        OLD_INVENTORY,
        quote="SEALKIT50;Main cylinder seal;1;B12",
        ex="inv",
    )
    _evidence(conn, "node", "SEAL-KIT-50", HP_MANUAL, "HP-5", 5, "kit")
    _evidence(conn, "edge", "HP-01|RAISED|E-217", LOG, quote="ERR 217", ex="log-parser")
    conn.close()
    return path


def open_kg(tmp_path: Path) -> sqlite3.Connection:
    from app.ask.tools import connect_ro

    return connect_ro(build_kg(tmp_path / "kg.sqlite"))


class ScriptedModel(StubChatModel):
    """A chat model that replies from a list and records the tools it was given."""

    model: str = TERRA
    script: list[AIMessage] = Field(default_factory=list)
    bound: list[list] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bound.append(list(tools))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        index = len(self.seen)
        self.seen.append(list(messages))
        reply = self.script[min(index, len(self.script) - 1)]
        reply = reply.model_copy(
            update={
                "usage_metadata": self._usage(),
                "response_metadata": {"model_name": f"{self.model}-2026-10-01"},
            }
        )
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=reply)])


def calls(*calls_: tuple[str, dict]) -> AIMessage:
    """A model message that asks for these tool calls."""
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": f"call_{i}", "type": "tool_call"}
            for i, (name, args) in enumerate(calls_)
        ],
    )


def say(text: str = "done") -> AIMessage:
    return AIMessage(content=text)


def terra_model(**fields) -> ScriptedModel:
    return ScriptedModel(callbacks=[llm.CostTracker(SETTINGS)], **fields)


def plan_model(queries: list[str]) -> StubChatModel:
    """A stub model whose answer is a search plan."""
    return StubChatModel(
        model=TERRA,
        reply=json.dumps({"queries": queries}),
        callbacks=[llm.CostTracker(SETTINGS)],
    )
