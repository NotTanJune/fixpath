"""The lookups the graph agent uses to read facts from the knowledge graph.

Every function here only reads. The model picks which to call, but each result comes
from the graph, with the source quotes behind it and the graph links it was read from.
Ids are cleaned up before lookup, and an id that is not in the graph gives an empty
result rather than an error.

The main lookups are find_machine, alarm_causes, fixes_for, parts_for,
similar_breakdowns and alarm_history. find_parts and part_stock answer stock questions.
connect_ro opens the graph read-only.
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, utils

from app import store
from app.ask.types import (
    AlarmHistory,
    Breakdown,
    Cause,
    Citation,
    Fix,
    PartNeed,
    PartStock,
    PathStep,
)
from app.config import KG_PATH
from app.ingest.normalize import part_key

FUZZY_CUTOFF = 80
MAX_CITATIONS = 3
# A symptom counts as similar only above this share of a full match, so two texts that
# share one word such as "oil" do not.
SYMPTOM_FLOOR = 0.6
# Text that looks like a machine id is matched exactly or not at all. A near miss is
# another machine, not a typo.
ASSET_ID_SHAPE = re.compile(r"^[A-Z]{2,3}-?\d+$", re.IGNORECASE)
# find_parts reads phrases of up to this many words, and ignores very short part keys
# that ordinary words could spell.
MAX_PART_WORDS = 4
MIN_PART_KEY = 4
_PART_WORD = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")


# --- connection -----------------------------------------------------------------


def connect_ro(kg_path: Path | str = KG_PATH) -> sqlite3.Connection:
    """Open the knowledge graph read-only. The file must already exist."""
    uri = Path(kg_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --- helpers --------------------------------------------------------------------


def _id(value: str) -> str:
    return value.strip().upper()


def _citations(
    conn: sqlite3.Connection, kind: str, fact_id: str, doc_type: str | None = None
) -> list[Citation]:
    """The source quotes behind a fact, manuals first, then by confidence."""
    query = (
        "SELECT d.path, e.page, e.chunk_id, e.quote FROM evidence e"
        " JOIN documents d ON d.id = e.doc_id WHERE e.fact_kind = ? AND e.fact_id = ?"
    )
    params: list[Any] = [kind, fact_id]
    if doc_type is not None:
        query += " AND d.doc_type = ?"
        params.append(doc_type)
    query += " ORDER BY d.doc_type != 'manual', e.confidence DESC, e.id"
    return [
        Citation(
            doc_path=row["path"],
            page=row["page"],
            chunk_id=row["chunk_id"],
            quote=row["quote"],
        )
        for row in conn.execute(query, params)
    ]


def _merge(*lists: list[Citation]) -> list[Citation]:
    """Combine the citations without repeats, keeping only the first few."""
    merged: list[Citation] = []
    for citation in (item for items in lists for item in items):
        if citation not in merged:
            merged.append(citation)
    return merged[:MAX_CITATIONS]


def _step(src: str, rel: str, dst: str) -> PathStep:
    return PathStep(src=src, rel=rel, dst=dst)


def _incident_count(conn: sqlite3.Connection, asset_id: str, code: str) -> int:
    """The past incidents on the machine that started with the alarm."""
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM edges t JOIN edges o ON o.src = t.src"
        " WHERE t.rel = 'TRIGGERED_BY' AND t.dst = ?"
        " AND o.rel = 'OCCURRED_ON' AND o.dst = ?",
        (code, asset_id),
    ).fetchone()
    return count


def _cause_count(conn: sqlite3.Connection, asset_id: str, code: str, mode: str) -> int:
    """The past incidents on the machine with this alarm and this root cause."""
    (count,) = conn.execute(
        "SELECT COUNT(DISTINCT t.src) FROM edges t"
        " JOIN edges o ON o.src = t.src JOIN edges r ON r.src = t.src"
        " WHERE t.rel = 'TRIGGERED_BY' AND t.dst = ?"
        " AND o.rel = 'OCCURRED_ON' AND o.dst = ?"
        " AND r.rel = 'ROOT_CAUSE' AND r.dst = ?",
        (code, asset_id, mode),
    ).fetchone()
    return count


# --- find_machine ---------------------------------------------------------------


def find_machine(conn: sqlite3.Connection, text: str) -> list[dict]:
    """The machines that the text names.

    An exact id, name or alias wins. Next comes an exact model or vendor. Last comes a
    close name match, which is never used for text that looks like a machine id.
    """
    text = text.strip()
    if not text:
        return []
    exact, by_model, close = [], [], []
    rows = conn.execute("SELECT id, props FROM nodes WHERE type = 'Asset' ORDER BY id")
    for row in rows:
        props = json.loads(row["props"])
        names = [row["id"], props["name"], *props.get("aliases", [])]
        scored = [
            (
                100.0
                if name.casefold() == text.casefold()
                else fuzz.WRatio(text, name, processor=utils.default_process),
                name,
            )
            for name in names
        ]
        score, matched = max(scored, key=lambda pair: pair[0])
        item = {
            "id": row["id"],
            "name": props["name"],
            "model": props["model"],
            "matched": matched,
            "score": round(score, 1),
        }
        if score == 100.0:
            exact.append(item)
        elif text.casefold() == props["model"].casefold():
            by_model.append({**item, "matched": "model", "score": 100.0})
        elif text.casefold() == props.get("vendor", "").casefold():
            by_model.append({**item, "matched": "vendor", "score": 100.0})
        elif score >= FUZZY_CUTOFF and not ASSET_ID_SHAPE.match(text):
            close.append(item)
    return sorted(
        exact or by_model or close, key=lambda item: (-item["score"], item["id"])
    )


# --- alarm_causes ---------------------------------------------------------------


def alarm_causes(conn: sqlite3.Connection, asset_id: str, code: str) -> list[Cause]:
    """The root causes an alarm points to on a machine.

    Causes that belong to another machine's parts are left out. The history counts
    this machine's own past incidents. Causes come with the best supported first.
    """
    asset_id, code = _id(asset_id), _id(code)
    components = {n["id"] for n in store.neighbors(conn, asset_id, "HAS_COMPONENT")}
    total = _incident_count(conn, asset_id, code)
    causes = []
    for hit in store.neighbors(conn, code, "INDICATES"):
        mode = hit["id"]
        affected = [
            n["id"]
            for n in store.neighbors(conn, mode, "AFFECTS")
            if n["id"] in components
        ]
        if not affected:
            continue
        path = [_step(code, "INDICATES", mode)]
        for component in affected:
            path += [
                _step(mode, "AFFECTS", component),
                _step(asset_id, "HAS_COMPONENT", component),
            ]
        causes.append(
            Cause(
                asset_id=asset_id,
                failure_mode_id=mode,
                name=hit["props"]["name"],
                likelihood=hit["edge_props"]["likelihood"],
                support=_cause_count(conn, asset_id, code, mode),
                of_total=total,
                support_model=hit["edge_props"]["support"],
                citations=_merge(_citations(conn, "edge", hit["edge_id"])),
                path=path,
            )
        )
    return sorted(causes, key=lambda c: (-c.support, -c.likelihood, c.failure_mode_id))


def cause_on(
    conn: sqlite3.Connection, asset_id: str, failure_mode_id: str
) -> Cause | None:
    """A root cause on the machine, looked up without an alarm.

    Returns None if the cause does not affect any part of the machine. With no alarm,
    the likelihood is the share of the machine's past incidents that had this cause.
    """
    asset_id, failure_mode_id = _id(asset_id), _id(failure_mode_id)
    components = {n["id"] for n in store.neighbors(conn, asset_id, "HAS_COMPONENT")}
    affected = [
        n
        for n in store.neighbors(conn, failure_mode_id, "AFFECTS")
        if n["id"] in components
    ]
    node = conn.execute(
        "SELECT props FROM nodes WHERE id = ? AND type = 'FailureMode'",
        (failure_mode_id,),
    ).fetchone()
    if not affected or node is None:
        return None
    incidents = [
        n["id"] for n in store.neighbors(conn, asset_id, "OCCURRED_ON", direction="in")
    ]
    support = sum(
        any(c["id"] == failure_mode_id for c in store.neighbors(conn, i, "ROOT_CAUSE"))
        for i in incidents
    )
    path = []
    for component in affected:
        path += [
            _step(failure_mode_id, "AFFECTS", component["id"]),
            _step(asset_id, "HAS_COMPONENT", component["id"]),
        ]
    return Cause(
        asset_id=asset_id,
        failure_mode_id=failure_mode_id,
        name=json.loads(node["props"])["name"],
        likelihood=support / len(incidents) if incidents else 0.0,
        support=support,
        of_total=len(incidents),
        support_model=0,
        citations=_merge(
            *(_citations(conn, "edge", n["edge_id"]) for n in affected),
            _citations(conn, "node", failure_mode_id),
        ),
        path=path,
    )


# --- fixes_for ------------------------------------------------------------------


def fixes_for(conn: sqlite3.Connection, failure_mode_id: str) -> list[Fix]:
    """The procedures that fix a root cause."""
    failure_mode_id = _id(failure_mode_id)
    return [
        Fix(
            procedure_id=hit["id"],
            title=hit["props"]["title"],
            steps=hit["props"]["steps"],
            requires_loto=hit["props"]["requires_loto"],
            est_minutes=hit["props"]["est_minutes"],
            manual_ref=hit["props"].get("manual_ref"),
            citations=_merge(
                _citations(conn, "node", hit["id"]),
                _citations(conn, "edge", hit["edge_id"]),
            ),
            path=[_step(failure_mode_id, "RESOLVED_BY", hit["id"])],
        )
        for hit in store.neighbors(conn, failure_mode_id, "RESOLVED_BY")
    ]


# --- parts_for ------------------------------------------------------------------


def _stock_citations(
    conn: sqlite3.Connection, part_no: str, on_hand: int | None, bin_: str | None
) -> list[Citation]:
    """The inventory quotes that back the part's current stock.

    Inventory files can disagree, because an old sheet outlives its replacement. Only
    quotes that show the current bin and count are cited.
    """
    if on_hand is None and not bin_:
        return []
    squeeze = str.maketrans("", "", "- ")
    kept = []
    for citation in _citations(conn, "node", part_no, doc_type="inventory"):
        if (
            bin_
            and bin_.translate(squeeze).casefold()
            not in citation.quote.translate(squeeze).casefold()
        ):
            continue
        if on_hand is not None and not re.search(
            rf"(?<![\w.]){on_hand}(?![\w.])", citation.quote
        ):
            continue
        kept.append(citation)
    return kept


def parts_for(conn: sqlite3.Connection, procedure_id: str) -> list[PartNeed]:
    """The parts a procedure needs, each with its stock.

    A part counts as enough only when the stock is known and covers the quantity needed.
    """
    procedure_id = _id(procedure_id)
    needs = []
    for hit in store.neighbors(conn, procedure_id, "REQUIRES"):
        props, qty = hit["props"], hit["edge_props"]["qty"]
        on_hand = props.get("qty_on_hand")
        needs.append(
            PartNeed(
                part_no=hit["id"],
                description=props["description"],
                qty=qty,
                qty_on_hand=on_hand,
                bin=props.get("bin"),
                lead_time_days=props.get("lead_time_days"),
                stock_as_of=props.get("stock_as_of"),
                enough=on_hand is not None and on_hand >= qty,
                citations=_merge(
                    _citations(conn, "edge", hit["edge_id"]),
                    _stock_citations(conn, hit["id"], on_hand, props.get("bin")),
                ),
                path=[_step(procedure_id, "REQUIRES", hit["id"])],
            )
        )
    return needs


# --- find_parts and part_stock ------------------------------------------------------


def find_parts(conn: sqlite3.Connection, text: str) -> list[str]:
    """The ids of the parts the text names, in order, each once.

    A short phrase names a part if it matches the part's id or an alias, or if it
    matches in letters and digits alone and only one part fits.
    """
    parts = []
    for row in conn.execute(
        "SELECT id, props FROM nodes WHERE type = 'Part' ORDER BY id"
    ):
        names = [row["id"], *json.loads(row["props"]).get("aliases", [])]
        parts.append(
            (row["id"], {n.casefold() for n in names}, {part_key(n) for n in names})
        )
    words = _PART_WORD.findall(text)
    found: dict[str, None] = {}
    for start in range(len(words)):
        for size in range(1, MAX_PART_WORDS + 1):
            phrase = " ".join(words[start : start + size])
            if len(words[start : start + size]) < size:
                break
            exact = [pid for pid, names, _ in parts if phrase.casefold() in names]
            key = part_key(phrase)
            by_key = (
                [pid for pid, _, keys in parts if key in keys]
                if len(key) >= MIN_PART_KEY
                else []
            )
            for hit in exact or (by_key if len(by_key) == 1 else []):
                found.setdefault(hit)
    return list(found)


def part_stock(conn: sqlite3.Connection, part_no: str) -> PartStock | None:
    """Stock of a part: on hand, bin, lead time and the machines it fits. None if the
    part is not in the graph."""
    part_no = _id(part_no)
    row = conn.execute(
        "SELECT props FROM nodes WHERE id = ? AND type = 'Part'", (part_no,)
    ).fetchone()
    if row is None:
        return None
    props = json.loads(row["props"])
    on_hand = props.get("qty_on_hand")
    fits = sorted(
        {
            asset["id"]
            for component in store.neighbors(conn, part_no, "FITS")
            for asset in store.neighbors(
                conn, component["id"], "HAS_COMPONENT", direction="in"
            )
        }
    )
    return PartStock(
        part_no=part_no,
        description=props["description"],
        qty_on_hand=on_hand,
        bin=props.get("bin"),
        lead_time_days=props.get("lead_time_days"),
        stock_as_of=props.get("stock_as_of"),
        fits=fits,
        citations=_stock_citations(conn, part_no, on_hand, props.get("bin")),
    )


# --- similar_breakdowns -----------------------------------------------------------


def similar_breakdowns(
    conn: sqlite3.Connection,
    asset_id: str,
    code: str | None = None,
    failure_mode_id: str | None = None,
    symptom: str | None = None,
    limit: int = 5,
) -> list[Breakdown]:
    """Past incidents on the machine that look like the current one.

    An incident scores for the same alarm, the same root cause and a similar symptom.
    If any of these is given, incidents with no match are left out. The best match
    comes first, then the newest.
    """
    asset_id = _id(asset_id)
    code = _id(code) if code else None
    failure_mode_id = _id(failure_mode_id) if failure_mode_id else None
    symptom = symptom.strip() if symptom and symptom.strip() else None
    filtered = bool(code or failure_mode_id or symptom)
    ranked = []
    for incident in store.neighbors(conn, asset_id, "OCCURRED_ON", direction="in"):
        alarms = [
            n["id"] for n in store.neighbors(conn, incident["id"], "TRIGGERED_BY")
        ]
        causes = [n["id"] for n in store.neighbors(conn, incident["id"], "ROOT_CAUSE")]
        score = float(code in alarms) + float(failure_mode_id in causes)
        if symptom:
            similarity = (
                fuzz.token_set_ratio(
                    symptom,
                    incident["props"]["symptom_text"],
                    processor=utils.default_process,
                )
                / 100
            )
            score += similarity if similarity >= SYMPTOM_FLOOR else 0.0
        if filtered and score == 0:
            continue
        ranked.append(
            (score, incident["props"]["date"], incident["id"], incident, alarms, causes)
        )
    ranked.sort(key=lambda item: item[:3], reverse=True)
    return [
        _breakdown(conn, asset_id, incident, alarms, causes)
        for _, _, _, incident, alarms, causes in ranked[: max(limit, 0)]
    ]


def _breakdown(
    conn: sqlite3.Connection,
    asset_id: str,
    incident: dict,
    alarms: list[str],
    causes: list[str],
) -> Breakdown:
    incident_id, props = incident["id"], incident["props"]
    used = store.neighbors(conn, incident_id, "USED")
    path = [_step(incident_id, "OCCURRED_ON", asset_id)]
    path += [_step(incident_id, "TRIGGERED_BY", alarm) for alarm in alarms]
    path += [_step(incident_id, "ROOT_CAUSE", cause) for cause in causes]
    path += [_step(incident_id, "USED", part["id"]) for part in used]
    return Breakdown(
        asset_id=asset_id,
        incident_id=incident_id,
        date=props["date"],
        shift=props.get("shift"),
        downtime_min=props.get("downtime_min"),
        technician=props.get("technician"),
        symptom_text=props["symptom_text"],
        resolution_text=props.get("resolution_text"),
        root_cause_id=causes[0] if causes else None,
        parts_used={part["id"]: part["edge_props"]["qty"] for part in used},
        citations=_merge(_citations(conn, "node", incident_id)),
        path=path,
    )


# --- alarm_history ----------------------------------------------------------------


def machines_raising(conn: sqlite3.Connection, code: str) -> list[dict]:
    """The machines that raised the alarm, the most frequent first."""
    raisers = [
        {"id": hit["id"], "count": hit["edge_props"]["count"]}
        for hit in store.neighbors(conn, _id(code), "RAISED", direction="in")
        if hit["type"] == "Asset"
    ]
    return sorted(raisers, key=lambda item: (-item["count"], item["id"]))


def alarm_history(conn: sqlite3.Connection, asset_id: str, code: str) -> AlarmHistory:
    """How often the machine raised the alarm, and how many incidents it started."""
    asset_id, code = _id(asset_id), _id(code)
    raised = [n for n in store.neighbors(conn, asset_id, "RAISED") if n["id"] == code]
    incidents = _incident_count(conn, asset_id, code)
    if not raised:
        return AlarmHistory(asset_id=asset_id, code=code, count=0, incidents=incidents)
    edge = raised[0]
    return AlarmHistory(
        asset_id=asset_id,
        code=code,
        count=edge["edge_props"]["count"],
        first_seen=edge["edge_props"]["first_seen"],
        last_seen=edge["edge_props"]["last_seen"],
        incidents=incidents,
        citations=_merge(_citations(conn, "edge", edge["edge_id"])),
        path=[_step(asset_id, "RAISED", code)],
    )
