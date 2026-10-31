"""Tests of the endpoint that serves a source document's original file."""

import sqlite3
from contextlib import closing

import pytest
from api_stubs import make_app
from fastapi.testclient import TestClient
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.api import views

CSV_TEXT = "Part no,On hand\nSEAL-KIT-50,3\n"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HASH = "0" * 64


@pytest.fixture
def client(tmp_path):
    return TestClient(make_app(tmp_path))


def add_document(tmp_path, doc_id, path):
    kg = tmp_path / "data" / "kg.sqlite"
    with closing(sqlite3.connect(kg)) as conn, conn:
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?)",
            (doc_id, path, "inventory", HASH),
        )


def raw(tmp_path):
    return tmp_path / "data" / "raw"


@pytest.mark.parametrize(
    "doc_id, path, media_type",
    [
        ("DOC-aaaaaaaa", "manuals/hp.pdf", "application/pdf"),
        ("DOC-eeeeeeee", "inventory/spares.xlsx", XLSX_TYPE),
        ("DOC-ffffffff", "inventory/spares_old.csv", "text/csv; charset=utf-8"),
        ("DOC-dddddddd", "logs/hp.log", "text/plain; charset=utf-8"),
        ("DOC-cccccccc", "notes/note.txt", "text/plain; charset=utf-8"),
    ],
)
def test_each_kind_comes_back_as_its_bytes_with_its_media_type(
    tmp_path, doc_id, path, media_type
):
    client = TestClient(make_app(tmp_path))
    (raw(tmp_path) / "inventory" / "spares_old.csv").write_text(CSV_TEXT)
    response = client.get(f"/api/sources/{doc_id}/file")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == media_type
    name = path.rpartition("/")[2]
    assert response.headers["content-disposition"] == f'inline; filename="{name}"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-cache"
    assert response.content == (raw(tmp_path) / path).read_bytes()


def test_an_unknown_extension_is_an_octet_stream(tmp_path):
    client = TestClient(make_app(tmp_path))
    (raw(tmp_path) / "notes" / "blob.bin").write_bytes(b"\x00\x01\x02")
    add_document(tmp_path, "DOC-12345678", "notes/blob.bin")
    response = client.get("/api/sources/DOC-12345678/file")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.content == b"\x00\x01\x02"


def test_an_upper_case_extension_is_matched_too(tmp_path):
    client = TestClient(make_app(tmp_path))
    (raw(tmp_path) / "manuals" / "BIG.PDF").write_bytes(b"%PDF-1.4\n")
    add_document(tmp_path, "DOC-12345678", "manuals/BIG.PDF")
    response = client.get("/api/sources/DOC-12345678/file")
    assert response.headers["content-type"] == "application/pdf"


def test_a_file_name_with_a_quote_is_quoted_safely(tmp_path):
    client = TestClient(make_app(tmp_path))
    name = 'odd "name".txt'
    (raw(tmp_path) / "notes" / name).write_text("x")
    add_document(tmp_path, "DOC-12345678", f"notes/{name}")
    disposition = client.get("/api/sources/DOC-12345678/file").headers[
        "content-disposition"
    ]
    assert disposition.startswith("inline; ")
    assert '"name"' not in disposition  # the inner quotes are escaped or encoded
    assert "\n" not in disposition


def test_an_unknown_document_is_a_404_with_the_plain_message(client):
    response = client.get("/api/sources/DOC-00000000/file")
    assert response.status_code == 404
    assert response.json() == {"detail": "there is no document DOC-00000000"}


@pytest.mark.parametrize(
    "doc_id",
    ["nope", "DOC-xyz", "doc-aaaaaaaa", "DOC-AAAAAAAA", "DOC-aaaaaaaa0", "DOC-"],
)
def test_a_malformed_id_is_a_404(client, doc_id):
    assert client.get(f"/api/sources/{doc_id}/file").status_code == 404


@pytest.mark.parametrize(
    "doc_id",
    [
        "..%2fkg.sqlite",
        "%2e%2e%2fkg.sqlite",
        "..%2f..%2fdata%2fkg.sqlite",
        "%2fetc%2fpasswd",
        "DOC-aaaaaaaa%2f..%2f..%2fkg.sqlite",
        "..%5ckg.sqlite",
        "..",
        "%2e%2e",
    ],
)
def test_traversal_in_the_id_is_a_404(client, doc_id):
    response = client.get(f"/api/sources/{doc_id}/file")
    assert response.status_code == 404
    assert b"SQLite" not in response.content


def test_a_row_whose_path_leaves_the_raw_folder_is_a_404(tmp_path):
    client = TestClient(make_app(tmp_path))
    secret = tmp_path / "data" / "secret.txt"
    secret.write_text("outside the raw folder")
    outside = tmp_path / "outside.txt"
    outside.write_text("absolute outside")
    add_document(tmp_path, "DOC-11111111", "../secret.txt")
    add_document(tmp_path, "DOC-22222222", str(outside))
    (raw(tmp_path) / "link.txt").symlink_to(secret)
    add_document(tmp_path, "DOC-33333333", "link.txt")
    for doc_id in ("DOC-11111111", "DOC-22222222", "DOC-33333333"):
        response = client.get(f"/api/sources/{doc_id}/file")
        assert response.status_code == 404, doc_id
        assert b"outside the raw folder" not in response.content  # the file's text
        assert b"absolute outside" not in response.content


def test_a_row_with_its_file_deleted_is_a_404_like_the_detail_view(tmp_path):
    client = TestClient(make_app(tmp_path))
    (raw(tmp_path) / "logs" / "hp.log").unlink()
    response = client.get("/api/sources/DOC-dddddddd/file")
    assert response.status_code == 404
    assert response.json() == client.get("/api/sources/DOC-dddddddd").json()
    assert response.json()["detail"] == "the file logs/hp.log is not in the raw folder"


def test_a_file_added_after_start_is_served_once_its_row_exists(tmp_path):
    client = TestClient(make_app(tmp_path))
    (raw(tmp_path) / "notes" / "drop.txt").write_text("dropped")
    assert client.get("/api/sources/DOC-99999999/file").status_code == 404
    add_document(tmp_path, "DOC-99999999", "notes/drop.txt")
    response = client.get("/api/sources/DOC-99999999/file")
    assert (response.status_code, response.content) == (200, b"dropped")


def test_the_file_is_served_the_same_with_keys_in_live_mode(tmp_path):
    from api_stubs import WITH_KEYS

    client = TestClient(make_app(tmp_path, WITH_KEYS))
    response = client.get("/api/sources/DOC-dddddddd/file")
    assert response.status_code == 200
    assert response.content == (raw(tmp_path) / "logs" / "hp.log").read_bytes()


def test_a_database_locked_past_the_busy_timeout_is_a_503(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    kg_path = tmp_path / "data" / "kg.sqlite"
    monkeypatch.setattr(views, "BUSY_TIMEOUT_MS", 100)
    with closing(sqlite3.connect(kg_path, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode = DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        response = client.get("/api/sources/DOC-dddddddd/file")
        assert response.status_code == 503
        assert response.json() == {"detail": views.BUSY}
        writer.execute("ROLLBACK")
    assert client.get("/api/sources/DOC-dddddddd/file").status_code == 200
