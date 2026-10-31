"""Tests for the whole ingestion graph on a small folder of fixture files. Fake
extractors, a scripted decider, a fake embedder and a stub model client replace the
network and the real model."""

import json
from contextlib import closing
from pathlib import Path

import pytest
from langchain_core.globals import get_llm_cache

from app import llm, llm_cache, store
from app.ingest import graph, router
from app.ingest.normalize import document_id
from app.ingest.report import LAST_FILE, LATEST
from app.ingest.types import CandidateEdge, CandidateNode, Extraction, NodeRef
from llm_stubs import SETTINGS, Pair, StubChatModel, isolated, rows  # noqa: F401
from load_stubs import FakeEmbedder, chunk, ev
from resolve_stubs import ScriptedDecider, choice_answer

MANUAL = "manuals/service_manual.pdf"
LOG = "logs/HP01.log"
HANDOVER = "handovers/handover_2026-08-22_B.txt"
NOTE = "maintenance/note_from_arun.txt"
NEW_NOTE = "maintenance/note_from_weiming.txt"
SEAL = "FM-SEAL-WEAR"

FILES = {
    MANUAL: b"%PDF-1.4 fake",
    LOG: b"2026-08-22T01:00:00Z HP-01 ALARM E217 Oil pressure low\n",
    HANDOVER: b"Shift handover 22 Aug B\nHP1 stopped with E217.\n",
    NOTE: b"Arun: HP1 leaked oil under the ram, seals replaced.\n",
}


def node(doc, type_, key, label, props, extractor, quote, canonical=True):
    return CandidateNode(
        ref=NodeRef(type=type_, key=key),
        label=label,
        props=props,
        evidence=[ev(doc, quote)[0].model_copy(update={"extractor": extractor})],
        canonical=canonical,
    )


def edge(doc, src, rel, dst, extractor, quote, **props):
    return CandidateEdge(
        src=NodeRef(type=src[0], key=src[1]),
        rel=rel,
        dst=NodeRef(type=dst[0], key=dst[1]),
        props=props,
        evidence=[ev(doc, quote)[0].model_copy(update={"extractor": extractor})],
    )


def fake_manual(path, aliases, settings=None):
    doc = MANUAL
    return Extraction(
        doc_path=doc,
        doc_type="manual",
        extractor="sol-manual",
        nodes=[
            node(
                doc,
                "Component",
                "HP-01-MAINCYL",
                "Main cylinder",
                {"id": "HP-01-MAINCYL", "name": "Main cylinder", "asset_id": "HP-01"},
                "sol-manual",
                "Main cylinder",
            ),
            node(
                doc,
                "FailureMode",
                SEAL,
                "Main cylinder seal wear",
                {
                    "id": SEAL,
                    "name": "Main cylinder seal wear",
                    "mechanism": "worn seals",
                },
                "sol-manual",
                "Main cylinder seal wear",
            ),
        ],
        edges=[
            edge(
                doc,
                ("Asset", "HP-01"),
                "HAS_COMPONENT",
                ("Component", "HP-01-MAINCYL"),
                "sol-manual",
                "Main cylinder",
            ),
            edge(
                doc,
                ("FailureMode", SEAL),
                "AFFECTS",
                ("Component", "HP-01-MAINCYL"),
                "sol-manual",
                "Main cylinder seal wear",
            ),
        ],
        chunks=[chunk(doc, 0, "Main cylinder seal wear causes oil under the ram.")],
    )


def fake_log(path, aliases, settings=None):
    doc = LOG
    return Extraction(
        doc_path=doc,
        doc_type="log",
        extractor="log-parser",
        nodes=[
            node(
                doc,
                "Asset",
                "HP-01",
                "Press",
                {
                    "id": "HP-01",
                    "name": "Press",
                    "machine_type": "press",
                    "vendor": "Bramwell Hydraulics",
                    "model": "BH-250",
                },
                "log-parser",
                "HP-01 ALARM",
            ),
            node(
                doc,
                "AlarmCode",
                "E-217",
                "Oil pressure low",
                {"code": "E-217", "text": "Oil pressure low", "severity": "warning"},
                "log-parser",
                "ALARM E217 Oil pressure low",
            ),
        ],
        edges=[
            edge(
                doc,
                ("Asset", "HP-01"),
                "RAISED",
                ("AlarmCode", "E-217"),
                "log-parser",
                "HP-01 ALARM E217",
                count=1,
                first_seen="2026-08-22T01:00:00+00:00",
                last_seen="2026-08-22T01:00:00+00:00",
            ),
        ],
        chunks=[
            chunk(doc, 0, "2026-08-22T01:00:00Z HP-01 ALARM E217 Oil pressure low")
        ],
    )


MODEL_CALLS: list[str] = []


def model_call(purpose):
    """One call to the stub model client, made the way the real extractors make it."""
    MODEL_CALLS.append(purpose)
    return llm.structured("sol", Pair, "Extract.", "text", purpose, settings=SETTINGS)


def fake_handover(path, aliases, settings=None):
    model_call("fake handover")
    return Extraction(
        doc_path=HANDOVER,
        doc_type="handover",
        extractor="sol-text",
        chunks=[
            chunk(HANDOVER, 0, "Shift handover 22 Aug B"),
            chunk(HANDOVER, 1, "HP1 stopped with E217."),
        ],
        notes=["one line could not be read"],
    )


def fake_note(path, aliases, settings=None):
    purpose = "fake note " + path.name
    model_call(purpose)
    doc = f"maintenance/{path.name}"
    day = "2026-08-23" if path.name == "note_from_weiming.txt" else "2026-08-22"
    key = "WO-9" if path.name == "note_from_weiming.txt" else "WO-1"
    return Extraction(
        doc_path=doc,
        doc_type="maintenance_note",
        extractor="sol-text",
        nodes=[
            node(
                doc,
                "Incident",
                key,
                "oil under the ram",
                {
                    "date": day,
                    "shift": "B",
                    "downtime_min": 60,
                    "symptom_text": "oil under the ram",
                },
                "sol-text",
                "leaked oil under the ram",
                canonical=False,
            )
        ],
        edges=[
            edge(
                doc,
                ("Incident", key),
                "OCCURRED_ON",
                ("Asset", "HP-01"),
                "sol-text",
                "HP1 leaked oil",
            ),
            edge(
                doc,
                ("Incident", key),
                "TRIGGERED_BY",
                ("AlarmCode", "E-217"),
                "sol-text",
                "HP1 leaked oil",
            ),
        ],
        chunks=[chunk(doc, 0, path.read_text(encoding="utf-8").strip())],
    )


@pytest.fixture
def raw(tmp_path):
    root = tmp_path / "raw"
    for path, data in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


@pytest.fixture
def kg(tmp_path):
    return tmp_path / "out" / "kg.sqlite"


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    """Fake extractors in the registry, and a stub in place of the model client."""
    MODEL_CALLS.clear()
    monkeypatch.setattr(
        router,
        "REGISTRY",
        {
            ("manual", ".pdf"): fake_manual,
            ("log", ".log"): fake_log,
            ("handover", ".txt"): fake_handover,
            ("maintenance_note", ".txt"): fake_note,
        },
    )
    stub = StubChatModel(callbacks=[llm.CostTracker(SETTINGS)])
    monkeypatch.setattr(
        llm, "chat_model", lambda role, effort="medium", *, settings=None: stub
    )
    return stub


def script(purpose, state):
    """Pick the seal for every root cause question, and never merge two incidents."""
    if purpose.startswith("link"):
        return choice_answer(SEAL, {SEAL: 0.9, "unknown": 0.1})
    return 0.0


def run(raw, kg, decider=None, **options):
    options.setdefault("embedder", FakeEmbedder())
    return graph.run_full(
        raw_dir=raw,
        kg_path=kg,
        settings=SETTINGS,
        decider=decider or ScriptedDecider(script),
        **options,
    )


def query(path, sql, *params):
    with closing(store.connect(path)) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def chunks_of(raw, kg, **options):
    return list(
        graph.stream_ingest(
            raw_dir=raw,
            kg_path=kg,
            settings=SETTINGS,
            decider=ScriptedDecider(script),
            embedder=FakeEmbedder(),
            **options,
        )
    )


# --- the whole run ------------------------------------------------------------


def test_a_full_run_loads_the_graph_the_fake_extractors_describe(raw, kg):
    state = run(raw, kg)
    assert query(kg, "SELECT id, type FROM nodes ORDER BY id") == [
        {"id": "E-217", "type": "AlarmCode"},
        {"id": "FM-SEAL-WEAR", "type": "FailureMode"},
        {"id": "HP-01", "type": "Asset"},
        {"id": "HP-01-MAINCYL", "type": "Component"},
        {"id": "INC-2026-001", "type": "Incident"},
    ]
    rels = [r["id"] for r in query(kg, "SELECT id FROM edges ORDER BY id")]
    assert (
        "INC-2026-001|ROOT_CAUSE|FM-SEAL-WEAR" in rels
    )  # the scripted decider linked it
    assert "HP-01|RAISED|E-217" in rels
    assert [
        r["path"] for r in query(kg, "SELECT path FROM documents ORDER BY path")
    ] == sorted(FILES)
    assert state["report"]["totals"]["nodes"]["Incident"] == 1


def test_the_graph_runs_prepare_extract_per_file_resolve_link_validate_load_in_order(
    raw, kg
):
    nodes = [
        name
        for chunk_ in chunks_of(raw, kg)
        if chunk_["type"] == "updates"
        for name in chunk_["data"]
    ]
    assert nodes[0] == "prepare"
    assert nodes[-4:] == ["resolve", "link", "validate", "load"]
    assert nodes[1:-4] == ["extract"] * len(FILES)


def test_there_is_one_extract_call_per_raw_file(raw, kg, monkeypatch):
    seen = []
    real = router.REGISTRY[("log", ".log")]
    monkeypatch.setitem(
        router.REGISTRY,
        ("log", ".log"),
        lambda p, a, settings=None: (
            seen.append(p.name),
            real(p, a, settings=settings),
        )[1],
    )
    run(raw, kg)
    assert seen == ["HP01.log"]
    assert len(MODEL_CALLS) == 2  # the handover and the note


def test_every_extractor_gets_the_settings_of_the_run(raw, kg, monkeypatch):
    seen = []
    for key, real in list(router.REGISTRY.items()):
        monkeypatch.setitem(
            router.REGISTRY,
            key,
            lambda p, a, settings=None, real=real: (
                seen.append(settings),
                real(p, a, settings=settings),
            )[1],
        )
    run(raw, kg)
    assert seen == [SETTINGS] * len(FILES)


def test_the_resolver_gets_the_extractions_in_processing_order(raw, kg, monkeypatch):
    order = []
    real = graph.resolve
    monkeypatch.setattr(
        graph,
        "resolve",
        lambda extractions, aliases, decider, registry=None: (
            order.extend(e.doc_path for e in extractions),
            real(extractions, aliases, decider, registry),
        )[1],
    )
    run(raw, kg)
    assert order == [MANUAL, LOG, NOTE, HANDOVER]


# --- stream events -------------------------------------------------------------


def test_the_stream_carries_one_custom_event_per_file_with_the_counts(raw, kg):
    events = [c["data"] for c in chunks_of(raw, kg) if c["type"] == "custom"]
    assert sorted(e["doc_path"] for e in events) == sorted(
        [MANUAL, LOG, NOTE, HANDOVER]
    )
    log = next(e for e in events if e["doc_path"] == LOG)
    assert log == {
        "event": "extracted",
        "doc_path": LOG,
        "doc_type": "log",
        "extractor": "log-parser",
        "nodes": 2,
        "edges": 1,
        "chunks": 1,
        "notes": 0,
    }
    assert next(e for e in events if e["doc_path"] == HANDOVER)["notes"] == 1


def test_stream_chunks_are_typed_dicts(raw, kg):
    for item in chunks_of(raw, kg):
        assert set(item) >= {"type", "data"}
        assert item["type"] in ("updates", "custom")


# --- cache, decider, settings --------------------------------------------------------


def test_both_entry_points_enable_the_cache(raw, kg, monkeypatch):
    enabled = []
    real = llm_cache.enable_cache
    monkeypatch.setattr(
        llm_cache, "enable_cache", lambda *a, **k: (enabled.append(1), real(*a, **k))[1]
    )
    run(raw, kg)
    assert enabled == [1]
    (raw / NEW_NOTE).write_bytes(b"Weiming: oil again.\n")
    graph.run_file(
        raw / NEW_NOTE,
        kg_path=kg,
        raw_dir=raw,
        settings=SETTINGS,
        decider=ScriptedDecider(script),
        embedder=FakeEmbedder(),
    )
    assert enabled == [1, 1]
    assert get_llm_cache() is not None


def test_the_entry_points_use_get_decider_when_none_is_given(raw, kg, monkeypatch):
    asked = []
    decider = ScriptedDecider(script)
    monkeypatch.setattr(
        graph, "get_decider", lambda settings: (asked.append(settings), decider)[1]
    )
    graph.run_full(raw_dir=raw, kg_path=kg, settings=SETTINGS, embedder=FakeEmbedder())
    assert asked == [SETTINGS]
    assert decider.purposes() == ["link root cause"]


def test_run_full_loads_the_settings_when_none_are_given(raw, kg, monkeypatch):
    monkeypatch.setattr(graph, "load_settings", lambda: SETTINGS)
    graph.run_full(
        raw_dir=raw,
        kg_path=kg,
        decider=ScriptedDecider(script),
        embedder=FakeEmbedder(),
    )
    assert kg.exists()


def test_the_run_config_has_max_concurrency_8_a_run_name_and_the_tracing_callbacks(
    monkeypatch,
):
    handler = object()
    monkeypatch.setattr(graph, "tracing_callbacks", lambda settings: [handler])
    config = graph.run_config(SETTINGS)
    assert config["max_concurrency"] == 8
    assert config["run_name"] == "ingest"
    assert config["callbacks"] == [handler]
    monkeypatch.setattr(graph, "tracing_callbacks", lambda settings: [])
    assert "callbacks" not in graph.run_config(SETTINGS)


# --- model calls inside the graph -----------------------------------------------------


def test_every_model_call_made_inside_the_graph_is_booked_once(raw, kg):
    run(raw, kg)
    booked = rows()
    assert len(booked) == len(MODEL_CALLS) == 2
    assert sorted(r["purpose"] for r in booked) == sorted(MODEL_CALLS)
    assert all(r["model"] == llm.SOL_MODEL for r in booked)


def test_model_calls_inside_the_graph_nest_under_the_graph_run(raw, kg, monkeypatch):
    from langchain_core.callbacks import BaseCallbackHandler

    parents = {}

    class Tree(BaseCallbackHandler):
        def on_chain_start(
            self, serialized, inputs, *, run_id, parent_run_id=None, name=None, **kw
        ):
            parents[run_id] = (name, parent_run_id)

        def on_chat_model_start(
            self, serialized, messages, *, run_id, parent_run_id=None, **kw
        ):
            parents[run_id] = ("model", parent_run_id)

    monkeypatch.setattr(graph, "tracing_callbacks", lambda settings: [Tree()])
    run(raw, kg)
    models = [
        (rid, parent) for rid, (name, parent) in parents.items() if name == "model"
    ]
    assert len(models) == 2
    for _, parent in models:
        assert parent is not None
        # walk up to the root run, which is the graph named by run_name
        while parents.get(parent, (None, None))[1] is not None:
            parent = parents[parent][1]
        assert parents[parent][0] == "ingest"


# --- file mode -------------------------------------------------------------------------


def test_file_mode_merges_one_file_into_the_existing_graph(raw, kg):
    run(raw, kg)
    before = {r["id"] for r in query(kg, "SELECT id FROM nodes")}
    (raw / NEW_NOTE).write_bytes(b"Weiming: oil again under the ram.\n")
    calls = len(MODEL_CALLS)
    state = graph.run_file(
        raw / NEW_NOTE,
        kg_path=kg,
        raw_dir=raw,
        settings=SETTINGS,
        decider=ScriptedDecider(script),
        embedder=FakeEmbedder(),
    )
    after = {r["id"] for r in query(kg, "SELECT id FROM nodes")}
    assert before < after and after - before == {"INC-2026-002"}
    assert len(MODEL_CALLS) == calls + 1  # only the new file was extracted
    assert state["mode"] == "file"
    assert [
        r["path"] for r in query(kg, "SELECT path FROM documents ORDER BY path")
    ] == sorted([*FILES, NEW_NOTE])
    rels = {r["id"] for r in query(kg, "SELECT id FROM edges")}
    assert {
        "INC-2026-001|ROOT_CAUSE|FM-SEAL-WEAR",
        "INC-2026-002|ROOT_CAUSE|FM-SEAL-WEAR",
        "INC-2026-002|OCCURRED_ON|HP-01",
    } <= rels
    evidence_docs = {
        r["doc_id"]
        for r in query(kg, "SELECT doc_id FROM evidence WHERE fact_id = 'INC-2026-002'")
    }
    assert document_id(NEW_NOTE) in evidence_docs


def test_file_mode_deletes_nothing_from_the_full_run(raw, kg):
    run(raw, kg)
    before = {
        t: query(kg, f"SELECT count(*) AS n FROM {t}")[0]["n"]
        for t in ("nodes", "edges", "documents", "chunks")
    }
    (raw / NEW_NOTE).write_bytes(b"Weiming: oil again.\n")
    graph.run_file(
        raw / NEW_NOTE,
        kg_path=kg,
        raw_dir=raw,
        settings=SETTINGS,
        decider=ScriptedDecider(script),
        embedder=FakeEmbedder(),
    )
    for table, count in before.items():
        assert query(kg, f"SELECT count(*) AS n FROM {table}")[0]["n"] >= count


def test_file_mode_without_a_database_says_to_run_a_full_ingest_first(raw, kg):
    (raw / NEW_NOTE).write_bytes(b"Weiming: oil again.\n")
    with pytest.raises(FileNotFoundError, match="full ingest"):
        graph.run_file(
            raw / NEW_NOTE,
            kg_path=kg,
            raw_dir=raw,
            settings=SETTINGS,
            decider=ScriptedDecider(script),
            embedder=FakeEmbedder(),
        )


# --- the report ------------------------------------------------------------------------


def test_the_run_writes_the_report_next_to_the_database(raw, kg):
    state = run(raw, kg)
    path = kg.parent / "pipeline" / "latest.json"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    report = json.loads(text)
    assert (
        text == json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    assert report["totals"] == state["report"]["totals"]
    assert sorted(f["doc_path"] for f in report["files"]) == sorted(
        [MANUAL, LOG, NOTE, HANDOVER]
    )
    assert report["run"]["mode"] == "full"
    assert report["totals"]["cost"]["calls"] == 2  # the two stub model calls
    assert report["totals"]["cost"]["usd"] > 0


def test_a_report_path_can_be_given(raw, kg, tmp_path):
    target = tmp_path / "elsewhere" / "report.json"
    run(raw, kg, report_path=target)
    assert target.is_file()
    assert not (kg.parent / "pipeline").exists()


# --- edge cases -------------------------------------------------------------------------


def test_a_file_with_no_extractor_is_skipped_with_an_event_and_still_gets_a_document_row(
    raw, kg, monkeypatch
):
    monkeypatch.delitem(router.REGISTRY, ("log", ".log"))
    events = [c["data"] for c in chunks_of(raw, kg) if c["type"] == "custom"]
    skipped = [e for e in events if e["event"] == "skipped"]
    assert [e["doc_path"] for e in skipped] == [LOG]
    assert query(kg, "SELECT path FROM documents WHERE path = ?", LOG) == [
        {"path": LOG}
    ]


def test_an_empty_raw_folder_runs_to_the_end_and_loads_an_empty_graph(tmp_path, kg):
    empty = tmp_path / "empty_raw"
    empty.mkdir()
    state = run(empty, kg)
    assert state["report"]["files"] == []
    assert query(kg, "SELECT count(*) AS n FROM nodes") == [{"n": 0}]


def test_the_report_of_a_file_run_lists_only_that_file(raw, kg):
    run(raw, kg)
    (raw / NEW_NOTE).write_bytes(b"Weiming: oil again.\n")
    state = graph.run_file(
        raw / NEW_NOTE,
        kg_path=kg,
        raw_dir=raw,
        settings=SETTINGS,
        decider=ScriptedDecider(script),
        embedder=FakeEmbedder(),
    )
    assert [f["doc_path"] for f in state["report"]["files"]] == [NEW_NOTE]
    assert state["report"]["run"]["mode"] == "file"
    last = json.loads((kg.parent / "pipeline" / "last_file.json").read_text())
    assert last["run"]["mode"] == "file"


def test_a_file_run_leaves_the_report_of_the_full_run_byte_identical(raw, kg):
    run(raw, kg)
    latest = kg.parent / "pipeline" / "latest.json"
    before = latest.read_bytes()
    (raw / NEW_NOTE).write_bytes(b"Weiming: oil again.\n")
    graph.run_file(
        raw / NEW_NOTE,
        kg_path=kg,
        raw_dir=raw,
        settings=SETTINGS,
        decider=ScriptedDecider(script),
        embedder=FakeEmbedder(),
    )
    assert latest.read_bytes() == before
    last = json.loads((kg.parent / "pipeline" / "last_file.json").read_text())
    assert [f["doc_path"] for f in last["files"]] == [NEW_NOTE]
    assert LATEST == Path("pipeline") / "latest.json"
    assert LAST_FILE == Path("pipeline") / "last_file.json"
