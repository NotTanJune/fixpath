"""Tests of the upload endpoint: the checks on the uploaded file, where the file is saved,
and the stream of events from a fake ingest run.
"""

import asyncio
import io
import json
from datetime import datetime

import numpy as np
import pytest
from api_stubs import NO_KEYS, SHEET_ROWS, WITH_KEYS, make_app, parse_events
from fastapi.testclient import TestClient
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.api import uploads
from app.decide import NoDecisionBackend
from app.ingest import graph as ingest_graph
from app.ingest import router
from app.ingest.normalize import document_id
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey

REFUSAL = "This file is not text, a PDF or a workbook, so it cannot be read."
HANDOVER = b"Shift handover - 2026-10-20 - A shift\n\nHP-01: seals replaced again.\n"
REPORT = {
    "files": [{"doc_path": "handovers/new.txt"}],
    "totals": {"nodes": {"Incident": 1}},
}


@pytest.fixture
def root(tmp_path):
    return tmp_path


@pytest.fixture
def client(root):
    return TestClient(make_app(root, WITH_KEYS))


def upload(client, name="new.txt", data=HANDOVER, type_="text/plain"):
    return client.post("/api/ingest", files={"file": (name, data, type_)})


def fake_run(monkeypatch, root, chunks=None, error=None):
    """Replace the ingest run with a script. It saves the same record a real run saves."""
    seen = []

    def stream_ingest(path=None, raw_dir=None, kg_path=None, settings=None, **kwargs):
        seen.append(path)
        for chunk in chunks if chunks is not None else CHUNKS:
            yield chunk
        if error:
            raise error
        (root / "data" / "pipeline" / "last_file.json").write_text(json.dumps(REPORT))

    monkeypatch.setattr(ingest_graph, "stream_ingest", stream_ingest)
    return seen


def custom(**data):
    return {"type": "custom", "ns": (), "data": data}


def update(node, value):
    return {"type": "updates", "ns": (), "data": {node: value}}


CHUNKS = [
    update("prepare", {"paths": ["x"]}),
    custom(
        event="extracted",
        doc_path="handovers/new.txt",
        doc_type="handover",
        extractor="sol-text",
        nodes=2,
        edges=1,
        chunks=0,
        notes=0,
    ),
    update("extract", {"extractions": [object()]}),  # not valid JSON, so it must not be sent
    update("resolve", {"resolution": object()}),
    update("link", {"resolution": object()}),
    update("validate", {"validated": object()}),
    update("load", {"report": REPORT}),
]


# --- refusals ---


def test_in_replay_mode_an_upload_is_refused_with_a_reason(root):
    client = TestClient(make_app(root, NO_KEYS))
    response = upload(client)
    assert response.status_code == 403
    assert "keys" in response.json()["detail"]
    assert not (root / "data" / "raw" / "handovers").exists()


@pytest.mark.parametrize(
    "name", ["tool.exe", "notes.bin", "noextension", "data.xlsm", "a.TXT.bak"]
)
def test_a_binary_is_refused_whatever_its_name_with_the_one_message(client, name):
    response = upload(client, name, b"MZ\x90\x00\x03\x00\x00\x00binary", "x/y")
    assert response.status_code == 415
    assert response.json()["detail"] == REFUSAL


@pytest.mark.parametrize(
    "name, saved",
    [
        ("tool.exe", "handovers/tool.exe.txt"),
        ("notes.md", "handovers/notes.md.txt"),
        ("noextension", "handovers/noextension.txt"),
        ("a.TXT.bak", "handovers/a.TXT.bak.txt"),
    ],
)
def test_any_other_text_is_saved_as_name_dot_txt(
    client, monkeypatch, root, name, saved
):
    fake_run(monkeypatch, root)
    assert upload(client, name).status_code == 200
    assert (root / "data" / "raw" / saved).read_bytes() == HANDOVER


def test_a_converted_file_says_what_it_came_from(root):
    page = b"<html><body><p>Shift handover - 2026-10-20 - A shift</p></body></html>"
    saved = uploads.save_upload("wi.html", page, root / "raw")
    assert saved.path == root / "raw" / "handovers" / "wi.html.txt"
    assert (saved.converted_from, saved.chars) == (
        ".html",
        len("Shift handover - 2026-10-20 - A shift"),
    )
    assert saved.path.read_text() == "Shift handover - 2026-10-20 - A shift"


def test_a_native_file_is_saved_as_it_came(root):
    saved = uploads.save_upload("new.txt", HANDOVER, root / "raw")
    assert (saved.path.name, saved.converted_from, saved.chars) == (
        "new.txt",
        None,
        None,
    )


def test_converted_text_with_no_known_header_is_a_loose_maintenance_note(root):
    saved = uploads.save_upload("note.eml", b"Subject: x\n\nsome words\n", root / "raw")
    assert saved.path == root / "raw" / "maintenance" / "note.eml.txt"


def test_a_docx_is_read_as_text(root):
    import io

    from docx import Document

    document = Document()
    document.add_paragraph("Shift handover - 2026-10-20 - A shift")
    document.add_paragraph("HP-01: seals replaced.")
    buffer = io.BytesIO()
    document.save(buffer)
    saved = uploads.save_upload("proc.docx", buffer.getvalue(), root / "raw")
    assert saved.path.name == "proc.docx.txt" and saved.converted_from == ".docx"
    assert saved.path.parent.name == "handovers"
    assert "seals replaced" in saved.path.read_text()


def test_a_file_over_1_mb_is_refused(client):
    assert upload(client, "big.txt", b"x" * (1024 * 1024 + 1)).status_code == 413


def test_a_file_of_exactly_1_mb_passes_the_size_check(client, monkeypatch, root):
    fake_run(monkeypatch, root)
    body = b"Shift handover - x\n" + b"y" * (1024 * 1024 - 19)
    assert len(body) == 1024 * 1024
    assert upload(client, "edge.txt", body).status_code == 200


def test_a_file_nothing_identifies_is_a_422_and_leaves_nothing_behind(client, root):
    response = upload(client, "random.txt", b"just some words\n")
    assert response.status_code == 422 and "kind of file" in response.json()["detail"]
    assert not list((root / "data" / "raw").rglob("random.txt"))


def test_an_existing_file_is_never_overwritten(client, monkeypatch, root):
    fake_run(monkeypatch, root)
    (root / "data" / "raw" / "handovers").mkdir()
    (root / "data" / "raw" / "handovers" / "new.txt").write_bytes(b"original")
    assert upload(client).status_code == 409
    assert (root / "data" / "raw" / "handovers" / "new.txt").read_bytes() == b"original"


def test_a_missing_file_part_is_a_422(client):
    assert client.post("/api/ingest").status_code == 422


def test_a_stock_list_is_refused_and_nothing_is_saved(client, root):
    from openpyxl import Workbook

    book = Workbook()
    for row in SHEET_ROWS:
        book.active.append(row)
    sheet = io.BytesIO()
    book.save(sheet)
    response = upload(client, "stock.xlsx", sheet.getvalue())
    assert response.status_code == 422
    assert response.json()["detail"] == "Stock lists are loaded by a full ingest"
    csv = ",".join(SHEET_ROWS[0]) + "\nSEAL-KIT-50,Seal kit,3,pc,B12,14\n"
    assert upload(client, "stock.csv", csv.encode()).status_code == 422
    assert not list((root / "data" / "raw").rglob("stock.*"))


def test_a_file_that_no_extractor_reads_is_refused_and_never_written(client, root):
    handover_csv = upload(client, "h.csv", HANDOVER)  # a handover header, but in a .csv file
    assert handover_csv.status_code == 422
    assert "extractor" in handover_csv.json()["detail"]
    assert not list((root / "data" / "raw").rglob("h.csv"))


# --- names and folders ---


@pytest.mark.parametrize(
    "raw, safe",
    [
        ("new.txt", "new.txt"),
        ("../../etc/evil.txt", "evil.txt"),
        ("..\\..\\win\\evil.txt", "evil.txt"),
        ("/abs/path/x.txt", "x.txt"),
        ("shift notes 5 Jun A.txt", "shift notes 5 Jun A.txt"),
        ("odd;name$(x)&.txt", "odd_name__x__.txt"),
        (".hidden.txt", "hidden.txt"),
        ("naïve.txt", "na_ve.txt"),
        ("spaced   .txt", "spaced   .txt"),
    ],
)
def test_a_file_name_is_cut_to_its_base_name_and_made_of_safe_characters(raw, safe):
    assert uploads.safe_name(raw) == safe


@pytest.mark.parametrize("raw", ["", "...", "../", "/", "x" * 151])
def test_a_name_with_nothing_left_is_refused(raw):
    with pytest.raises(uploads.Refused) as refused:
        uploads.safe_name(raw)
    assert refused.value.status == 422


def test_the_file_goes_under_the_folder_of_its_doc_type(client, monkeypatch, root):
    seen = fake_run(monkeypatch, root)
    assert upload(client, "../../up/new.txt").status_code == 200
    saved = root / "data" / "raw" / "handovers" / "new.txt"
    assert saved.read_bytes() == HANDOVER and seen == [saved]
    assert (
        upload(client, "events.log", b"01/04/2026 06:45:00 | ERR 200 | X\n").status_code
        == 200
    )
    assert (root / "data" / "raw" / "logs" / "events.log").is_file()


# --- the stream ---


def test_the_stream_has_extracted_a_step_per_node_and_done_with_the_report(
    client, monkeypatch, root
):
    fake_run(monkeypatch, root)
    response = upload(client)
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_events(response.text)
    assert [n for n, _ in events] == [
        "step",
        "extracted",
        "step",
        "step",
        "step",
        "step",
        "step",
        "done",
    ]
    assert [d["node"] for n, d in events if n == "step"] == [
        "prepare",
        "extract",
        "resolve",
        "link",
        "validate",
        "load",
    ]
    extracted = events[1][1]
    assert extracted["doc_path"] == "handovers/new.txt" and extracted["nodes"] == 2
    assert "event" not in extracted
    assert events[-1] == ("done", {"report": REPORT})


def test_a_skipped_file_is_reported_too(client, monkeypatch, root):
    fake_run(
        monkeypatch,
        root,
        chunks=[
            custom(
                event="skipped", doc_path="handovers/new.txt", reason="no extractor"
            ),
            update("load", {"report": REPORT}),
        ],
    )
    events = parse_events(upload(client).text)
    assert events[0] == (
        "skipped",
        {"doc_path": "handovers/new.txt", "reason": "no extractor"},
    )


@pytest.mark.parametrize(
    "error, text",
    [
        (MissingApiKey("no key"), "keys"),
        (NoDecisionBackend("no answer"), "keys"),
        (BudgetExceeded("spent $2 of the $1.00 budget"), "budget"),
        (router.UnknownFile("cannot tell what kind of file /x is"), "kind of file"),
        (ValueError("secret value error /x"), "wrong"),  # not an unknown-file error
        (RuntimeError("secret internals"), "wrong"),
    ],
)
def test_a_failure_is_an_error_event_and_the_unloaded_file_is_removed(
    client, monkeypatch, root, error, text
):
    fake_run(
        monkeypatch,
        root,
        chunks=[
            update("prepare", {}),
            custom(event="skipped", doc_path="a", reason="r"),
        ],
        error=error,
    )
    events = parse_events(upload(client).text)
    assert [n for n, _ in events][-1] == "error" and "done" not in [
        n for n, _ in events
    ]
    message = events[-1][1]["message"]
    assert text in message and "secret" not in message and "/x" not in message
    assert not (root / "data" / "raw" / "handovers" / "new.txt").exists()


def test_a_file_whose_data_was_loaded_stays_even_if_the_run_then_fails(
    client, monkeypatch, root
):
    fake_run(
        monkeypatch,
        root,
        chunks=[update("load", {"report": REPORT})],
        error=RuntimeError("late"),
    )
    events = parse_events(upload(client).text)
    assert events[-1][0] == "error"
    assert (root / "data" / "raw" / "handovers" / "new.txt").is_file()


# --- the guard in front of the endpoint ---


def call_guard(guard, chunks, headers=()):
    """Feed the upload guard a body in chunks. Return the status, how many chunks it asked
    for, and the reply body."""
    sent, asked = [], []
    queue = list(chunks)

    async def receive():
        asked.append(1)
        body = queue.pop(0) if queue else b""
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/ingest",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
    }
    asyncio.run(guard(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    parts = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
    body = b"".join(parts)
    return start["status"], len(asked), body


async def reads_all(scope, receive, send):
    """An app that reads the whole body and replies 200."""
    more = True
    while more:
        more = (await receive()).get("more_body", False)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_the_guard_stops_counting_a_chunked_body_at_the_limit():
    guard = uploads.UploadGuard(reads_all, "/api/ingest")
    piece = 64 * 1024
    chunks = [b"x" * piece] * 480  # 30 MB, with no stated length
    status, asked, body = call_guard(guard, chunks)
    assert status == 413 and b"1 MB" in body
    assert asked * piece <= uploads.BODY_LIMIT + piece  # up to the limit and no further


def test_the_guard_stops_a_body_that_is_longer_than_its_content_length_says():
    guard = uploads.UploadGuard(reads_all, "/api/ingest")
    lying = [("content-length", "1000")]
    status, asked, _ = call_guard(guard, [b"x" * 65536] * 100, lying)
    assert status == 413 and asked <= 20


def test_the_guard_answers_a_big_content_length_without_reading_anything():
    guard = uploads.UploadGuard(reads_all, "/api/ingest")
    big = str(30 * 1024 * 1024)
    assert call_guard(guard, [b"x"] * 5, [("content-length", big)])[:2] == (413, 0)


def test_the_guard_in_replay_mode_answers_403_without_reading_anything():
    guard = uploads.UploadGuard(reads_all, "/api/ingest", refusal="needs keys")
    status, asked, body = call_guard(guard, [b"x"] * 5, [("content-length", "5")])
    assert (status, asked) == (403, 0) and json.loads(body) == {"detail": "needs keys"}


def test_the_guard_lets_a_body_within_the_limit_and_other_paths_through():
    guard = uploads.UploadGuard(reads_all, "/api/ingest")
    assert call_guard(guard, [b"x" * 1000], [("content-length", "1000")])[0] == 200
    refusing = uploads.UploadGuard(reads_all, "/api/ingest", refusal="needs keys")
    other = {"type": "http", "method": "GET", "path": "/api/health", "headers": []}
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    asyncio.run(refusing(other, receive, send))
    assert sent[0]["status"] == 200


def test_a_30_mb_upload_is_a_413_in_live_mode_and_a_403_in_replay_mode(root):
    big = ("big.txt", b"x" * (30 * 1024 * 1024), "text/plain")
    files = {"file": big}
    live = TestClient(make_app(root, WITH_KEYS)).post("/api/ingest", files=files)
    assert live.status_code == 413
    replay = TestClient(make_app(root / "again", NO_KEYS))
    replay = replay.post("/api/ingest", files=files)
    assert replay.status_code == 403


# --- the stage events around the stream, the map and the uploads list ---


def raw_of(root):
    return root / "data" / "raw"


def test_a_converted_file_starts_the_stream_with_converted(client, monkeypatch, root):
    fake_run(monkeypatch, root)
    events = parse_events(upload(client, "wi.md").text)
    assert events[0] == (
        "converted",
        {"from": ".md", "to": ".txt", "chars": len(HANDOVER.decode())},
    )
    assert [n for n, _ in events][1:3] == ["step", "extracted"]


def test_a_native_file_has_no_converted_event(client, monkeypatch, root):
    fake_run(monkeypatch, root)
    assert "converted" not in [n for n, _ in parse_events(upload(client).text)]


def test_a_docx_says_converted_from_docx(client, monkeypatch, root):
    import io

    from docx import Document

    fake_run(monkeypatch, root)
    document = Document()
    document.add_paragraph("Shift handover - 2026-10-20 - A shift")
    buffer = io.BytesIO()
    document.save(buffer)
    events = parse_events(upload(client, "p.docx", buffer.getvalue()).text)
    name, data = events[0]
    assert name == "converted" and data["from"] == ".docx" and data["to"] == ".txt"
    assert data["chars"] == len("Shift handover - 2026-10-20 - A shift")


def uploads_json(root):
    path = root / "data" / "pipeline" / "uploads.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_a_successful_ingest_is_appended_to_the_uploads_list(client, monkeypatch, root):
    fake_run(monkeypatch, root)
    assert upload(client).status_code == 200
    (entry,) = uploads_json(root)
    assert set(entry) == {"doc_id", "path", "doc_type", "added_at"}
    assert entry["path"] == "handovers/new.txt" and entry["doc_type"] == "handover"
    assert entry["doc_id"] == document_id("handovers/new.txt")
    assert datetime.fromisoformat(entry["added_at"]).tzinfo is not None
    fake_run(monkeypatch, root)
    assert upload(client, "second.txt").status_code == 200
    assert [e["path"] for e in uploads_json(root)] == [
        "handovers/new.txt",
        "handovers/second.txt",
    ]


def test_a_failed_ingest_is_not_in_the_uploads_list(client, monkeypatch, root):
    fake_run(monkeypatch, root, chunks=[update("prepare", {})], error=RuntimeError("x"))
    upload(client)
    assert uploads_json(root) is None


def test_a_run_that_loaded_then_failed_is_not_listed_either(client, monkeypatch, root):
    fake_run(
        monkeypatch,
        root,
        chunks=[update("load", {"report": REPORT})],
        error=ValueError("x"),
    )
    upload(client)
    assert uploads_json(root) is None


def mapped_root(root):
    """Give the app a map of two points and their vectors."""
    from app import factmap

    base = {"id": "A", "kind": "passage", "type": "log", "label": "a", "docs": []}
    fact_map = {
        "model": "m",
        "umap": {},
        "kg_sha256": "old",
        "counts": {"passages": 2, "nodes": 0, "links": 0},
        "points": [
            {**base, "x": 0.0, "y": 0.0, "z": 0.0},
            {**base, "id": "B", "x": 1.0, "y": 0.0, "z": 0.0},
        ],
    }
    path = root / "data" / "map" / "fact_map.json"
    path.parent.mkdir(parents=True)
    path.write_text(factmap.to_json(fact_map))
    np.save(factmap.vectors_path(path), np.eye(2, 384, dtype=np.float32))
    return path


def test_the_placed_points_are_appended_to_the_map_after_a_successful_load(
    client, monkeypatch, root
):
    from app import factmap

    map_path = mapped_root(root)
    point = {
        "id": "P-1",
        "kind": "passage",
        "type": "handover",
        "label": "p",
        "docs": ["DOC-x"],
        "x": 0.5,
        "y": 0.0,
        "z": 0.0,
    }
    placement = factmap.Placement([point], [], np.ones((1, 384), dtype=np.float32))
    fake_run(
        monkeypatch,
        root,
        chunks=[update("load", {"report": REPORT, "placed": placement})],
    )
    assert upload(client).status_code == 200
    points = json.loads(map_path.read_text())["points"]
    assert [p["id"] for p in points] == ["A", "B", "P-1"] and points[-1][
        "placed"
    ] is True
    assert np.load(factmap.vectors_path(map_path)).shape == (3, 384)


def test_a_map_that_cannot_be_appended_to_does_not_fail_the_ingest(
    client, monkeypatch, root
):
    from app import factmap

    map_path = mapped_root(root)
    np.save(factmap.vectors_path(map_path), np.eye(5, 384, dtype=np.float32))  # stale
    placement = factmap.Placement([], [], np.empty((0, 384), dtype=np.float32))
    fake_run(
        monkeypatch,
        root,
        chunks=[update("load", {"report": REPORT, "placed": placement})],
    )
    events = parse_events(upload(client).text)
    assert events[-1][0] == "done" and uploads_json(root)


def test_without_a_placement_the_map_is_left_alone(client, monkeypatch, root):
    map_path = mapped_root(root)
    before = map_path.read_bytes()
    fake_run(monkeypatch, root)
    assert upload(client).status_code == 200
    assert map_path.read_bytes() == before


def test_sources_gain_added_for_the_uploaded_documents(client, monkeypatch, root):
    before = client.get("/api/sources").json()
    assert all("added" not in s for s in before)
    fake_run(monkeypatch, root)
    upload(client)
    after = {s["id"]: s for s in client.get("/api/sources").json()}
    assert after == {
        s["id"]: s for s in before
    }  # new.txt has no document row in the fake
    path = root / "data" / "pipeline" / "uploads.json"
    doc_id = "DOC-dddddddd"  # the log in the test data
    path.write_text(
        json.dumps(
            [
                {
                    "doc_id": doc_id,
                    "path": "logs/hp.log",
                    "doc_type": "log",
                    "added_at": "2026-10-29T10:00:00+00:00",
                }
            ]
        )
    )
    listed = {s["id"]: s for s in client.get("/api/sources").json()}
    assert listed[doc_id]["added"] == "2026-10-29T10:00:00+00:00"
    assert all("added" not in s for i, s in listed.items() if i != doc_id)


def test_a_broken_uploads_list_does_not_break_the_sources(client, root):
    (root / "data" / "pipeline" / "uploads.json").write_text("not json")
    assert client.get("/api/sources").status_code == 200


# --- ingest on a keyless server (the dry run of the test set) ---


def test_ingest_can_be_enabled_without_keys_and_a_miss_then_ends_the_stream_cleanly(
    root,
):
    client = TestClient(make_app(root, NO_KEYS, ingest_enabled=True))
    assert client.get("/api/health").json()["ingest_enabled"] is True
    response = upload(client)
    assert response.status_code == 200
    events = parse_events(response.text)
    assert events[-1][0] == "error" and "keys" in events[-1][1]["message"]
    assert not (root / "data" / "raw" / "handovers" / "new.txt").exists()


def test_ingest_stays_refused_without_keys_unless_it_is_enabled(root):
    client = TestClient(make_app(root, NO_KEYS))
    assert client.get("/api/health").json()["ingest_enabled"] is False
    assert upload(client).status_code == 403


# --- converted documents are text, never a stock list ---


def _docx_with_table(header):
    from docx import Document

    document = Document()
    document.add_paragraph("Procedure: change the oil on HP-01")
    table = document.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = header
    table.rows[1].cells[0].text, table.rows[1].cells[1].text = "OIL-HLP46-20L", "3"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("header", [("Part", "Qty"), ("Item", "Quantity")])
def test_a_docx_with_a_stock_style_table_is_a_procedure_not_a_refused_stock_list(
    root, header
):
    saved = uploads.save_upload("proc.docx", _docx_with_table(header), root / "raw")
    assert saved.path == root / "raw" / "procedures" / "proc.docx.txt"
    assert saved.converted_from == ".docx"
    assert " | ".join(header) in saved.path.read_text()


def test_an_html_page_with_a_stock_style_table_is_a_loose_note(root):
    page = b"<h1>Gripper</h1><table><tr><th>Part</th><th>Qty</th></tr><tr><td>GRP-FNG-12</td><td>2</td></tr></table>"
    saved = uploads.save_upload("wi.html", page, root / "raw")
    assert saved.path.parent.name == "maintenance"


def test_a_markdown_handover_with_a_heading_goes_to_handovers_and_keeps_its_text(
    root,
):
    text = b"# Shift handover 20 Oct Shift B\n\n- HP-01: seals replaced.\n"
    saved = uploads.save_upload("h.md", text, root / "raw")
    assert saved.path == root / "raw" / "handovers" / "h.md.txt"
    assert saved.path.read_bytes() == text


def test_a_native_csv_stock_list_is_still_refused(root):
    with pytest.raises(uploads.Refused) as refused:
        uploads.save_upload("count.csv", b"Part,Qty\nA-1,2\n", root / "raw")
    assert refused.value.message == uploads.STOCK_LISTS
