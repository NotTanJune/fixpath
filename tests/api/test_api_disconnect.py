"""Tests of what happens when a client goes away mid-request. They use a real local
server and a real socket.

The framework stops the streaming work when the client leaves, but it can leave a lock
held. The app therefore runs each question in its own worker thread. These tests close a
real connection and check that the next client is served straight away.
"""

import threading
import time

import httpx
import pytest
from api_stubs import NO_KEYS, WITH_KEYS, CountingApp, RawClient, make_app, serving
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.api import uploads
from app.ask import crew
from app.ingest import graph as ingest_graph

HANDOVER = b"Shift handover - 2026-10-20 - A shift\n\nHP-01: seals replaced again.\n"
FREED_WITHIN = 3.0  # seconds from a disconnect to the next question's first event


@pytest.fixture(autouse=True)
def close_clients():
    yield
    RawClient.close_all()


def tick(node="triage"):
    data = {"event": "node_start", "node": node, "at": "t"}
    return {"type": "custom", "ns": (), "data": data}


def slow_crew(monkeypatch, steps=40, pause=0.25):
    """A slow stand-in crew. It records which questions started and which were closed,
    and it can hold one question until the test releases it."""
    seen = {"started": [], "closed": [], "release": threading.Event()}

    def stream_ask(question, history=None, **kwargs):
        seen["started"].append(question)
        try:
            if question == "hold":
                seen["release"].wait(30)
            for _ in range(steps):
                time.sleep(pause)
                yield tick()
        finally:
            seen["closed"].append(question)

    monkeypatch.setattr(crew, "stream_ask", stream_ask)
    return seen


def first_event_after(port, question):
    """Seconds until a new question starts. A queued notice only says it is waiting."""
    start = time.monotonic()
    client = RawClient(port).post_json("/api/ask", {"question": question})
    try:
        assert client.wait_for("node_start", within=15), "the question never started"
        return time.monotonic() - start
    finally:
        client.close()


# --- ask ---


def test_a_closed_socket_frees_the_ask_lock_for_the_next_question(
    tmp_path, monkeypatch
):
    seen = slow_crew(monkeypatch)
    with serving(make_app(tmp_path)) as port:
        first = RawClient(port).post_json("/api/ask", {"question": "A"})
        assert [n for n, _ in first.events(1)] == ["node_start"]
        first.close()
        waited = first_event_after(port, "B")
    assert waited < FREED_WITHIN
    assert seen["started"] == ["A", "B"]
    assert "A" in seen["closed"]  # the worker closed the crew of question A


def test_a_reset_connection_frees_the_ask_lock_too(tmp_path, monkeypatch):
    import socket
    import struct

    slow_crew(monkeypatch)
    with serving(make_app(tmp_path)) as port:
        first = RawClient(port).post_json("/api/ask", {"question": "A"})
        first.events(1)
        linger = struct.pack("ii", 1, 0)  # drop the connection at once instead of closing it politely
        first.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        first.close()
        assert first_event_after(port, "B") < FREED_WITHIN


def test_a_waiting_question_gets_a_queued_event_with_its_position(
    tmp_path, monkeypatch
):
    seen = slow_crew(monkeypatch, steps=1, pause=0.05)
    with serving(make_app(tmp_path)) as port:
        holder = RawClient(port).post_json("/api/ask", {"question": "hold"})
        deadline = time.monotonic() + 5
        while "hold" not in seen["started"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        second = RawClient(port).post_json("/api/ask", {"question": "second"})
        third = RawClient(port).post_json("/api/ask", {"question": "third"})
        assert second.events(1)[0] == ("queued", {"position": 1})
        assert third.events(1)[0] == ("queued", {"position": 2})
        assert seen["started"] == ["hold"]  # nobody jumped the queue
        seen["release"].set()
        assert [n for n, _ in second.events(2)][:2] == ["queued", "node_start"]
        # the third question is told each time it moves up, then it starts
        moved = [d["position"] for n, d in third.events(4) if n == "queued"]
        assert moved[0] == 2 and moved == sorted(moved, reverse=True)
        for client in (holder, second, third):
            client.close()
    assert seen["started"][:3] == ["hold", "second", "third"]


def test_a_fifth_question_gets_a_429_with_a_plain_message(tmp_path, monkeypatch):
    seen = slow_crew(monkeypatch, steps=1, pause=0.05)
    with serving(make_app(tmp_path)) as port:
        clients = [RawClient(port).post_json("/api/ask", {"question": "hold"})]
        deadline = time.monotonic() + 5
        while "hold" not in seen["started"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        for number in range(3):  # three can wait behind the one that runs
            waiting = {"question": f"w{number}"}
            clients.append(RawClient(port).post_json("/api/ask", waiting))
            assert clients[-1].events(1)[0][0] == "queued"
        url = f"http://127.0.0.1:{port}/api/ask"
        response = httpx.post(url, json={"question": "one too many"}, timeout=10)
        assert response.status_code == 429
        assert "waiting" in response.json()["detail"]
        seen["release"].set()
        for client in clients:
            client.close()
        # the queue has room again once the others finish or leave
        deadline = time.monotonic() + 15
        while (
            httpx.post(
                f"http://127.0.0.1:{port}/api/ask",
                json={"question": "later"},
                timeout=10,
            ).status_code
            == 429
        ):
            assert time.monotonic() < deadline
            time.sleep(0.2)


def test_a_question_that_leaves_the_line_is_never_run(tmp_path, monkeypatch):
    seen = slow_crew(monkeypatch, steps=1, pause=0.05)
    with serving(make_app(tmp_path)) as port:
        holder = RawClient(port).post_json("/api/ask", {"question": "hold"})
        deadline = time.monotonic() + 5
        while "hold" not in seen["started"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        gone = RawClient(port).post_json("/api/ask", {"question": "gone"})
        gone.events(1)
        gone.close()
        time.sleep(0.6)  # give the waiting worker time to notice the closed connection
        seen["release"].set()
        assert first_event_after(port, "next") < FREED_WITHIN
        holder.close()
    assert "gone" not in seen["started"]


# --- ingest ---


def slow_ingest(monkeypatch, steps=40, pause=0.25):
    seen = {"started": [], "closed": []}

    def stream_ingest(path=None, raw_dir=None, kg_path=None, settings=None, **kwargs):
        seen["started"].append(path)
        try:
            yield {"type": "updates", "ns": (), "data": {"prepare": {"paths": ["x"]}}}
            for _ in range(steps):
                time.sleep(pause)
                yield {
                    "type": "custom",
                    "ns": (),
                    "data": {
                        "event": "extracted",
                        "doc_path": "handovers/a.txt",
                        "doc_type": "handover",
                        "extractor": "sol-text",
                        "nodes": 1,
                        "edges": 0,
                        "chunks": 0,
                        "notes": 0,
                    },
                }
        finally:
            seen["closed"].append(path)

    monkeypatch.setattr(ingest_graph, "stream_ingest", stream_ingest)
    return seen


def test_a_closed_socket_frees_the_ingest_lock_and_removes_the_unloaded_file(
    tmp_path, monkeypatch
):
    seen = slow_ingest(monkeypatch)
    app = make_app(tmp_path, WITH_KEYS)
    saved = tmp_path / "data" / "raw" / "handovers" / "a.txt"
    with serving(app) as port:
        first = RawClient(port).post_file("/api/ingest", "a.txt", HANDOVER)
        assert len(first.events(2)) == 2 and saved.is_file()
        first.close()
        start = time.monotonic()
        deadline = start + FREED_WITHIN
        while saved.exists():
            assert time.monotonic() < deadline, "the closed upload is still saved"
            time.sleep(0.05)
        # the same file can be sent again, and its run starts at once
        second = RawClient(port).post_file("/api/ingest", "a.txt", HANDOVER)
        assert second.events(1, within=FREED_WITHIN)
        assert second.status == 200
        second.close()
    assert len(seen["started"]) == 2 and seen["closed"][0] == seen["started"][0]


# --- the size of an upload, on the wire ---


def chunked(total, piece=64 * 1024):
    """An upload body of the given size that does not state its length up front."""
    yield (
        b'--zzz\r\nContent-Disposition: form-data; name="file"; '
        b'filename="big.txt"\r\nContent-Type: text/plain\r\n\r\n'
    )
    sent = 0
    while sent < total:
        size = min(piece, total - sent)
        yield b"x" * size
        sent += size
    yield b"\r\n--zzz--\r\n"


def test_a_30_mb_upload_in_replay_mode_is_a_403_and_none_of_it_is_read(tmp_path):
    counted = CountingApp(make_app(tmp_path))  # no keys, so replay mode
    with serving(counted) as port:
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/ingest",
            files={"file": ("big.txt", b"x" * (30 * 1024 * 1024), "text/plain")},
            timeout=30,
        )
    assert response.status_code == 403 and "keys" in response.json()["detail"]
    assert counted.bytes_read == 0


def test_a_30_mb_upload_with_a_length_is_a_413_and_none_of_it_is_read(tmp_path):
    counted = CountingApp(make_app(tmp_path, WITH_KEYS))
    with serving(counted) as port:
        response = httpx.post(
            f"http://127.0.0.1:{port}/api/ingest",
            files={"file": ("big.txt", b"x" * (30 * 1024 * 1024), "text/plain")},
            timeout=30,
        )
    assert response.status_code == 413
    assert counted.bytes_read == 0


def test_a_chunked_upload_is_cut_off_once_it_passes_the_limit(tmp_path):
    counted = CountingApp(make_app(tmp_path, WITH_KEYS))
    with serving(counted) as port:
        try:
            response = httpx.post(
                f"http://127.0.0.1:{port}/api/ingest",
                content=chunked(30 * 1024 * 1024),
                headers={"Content-Type": "multipart/form-data; boundary=zzz"},
                timeout=30,
            )
            status = response.status_code
        except httpx.TransportError:  # the server closed the connection while we were sending
            status = 413
    assert status == 413
    # it stops at the message that passes the limit, and one read can hand up 256 KiB
    assert counted.bytes_read <= uploads.BODY_LIMIT + 512 * 1024
    assert counted.bytes_read >= uploads.MAX_BYTES  # it did read up to the limit


@pytest.mark.parametrize("live", [True, False])
def test_a_file_of_exactly_1_mb_still_passes_over_the_wire(tmp_path, monkeypatch, live):
    slow_ingest(monkeypatch, steps=1, pause=0.01)
    body = b"Shift handover - x\n" + b"y" * (uploads.MAX_BYTES - 19)
    app = make_app(tmp_path, WITH_KEYS if live else NO_KEYS)
    with serving(app) as port:
        client = RawClient(port).post_file("/api/ingest", "edge.txt", body)
        assert client.head() == (200 if live else 403)
