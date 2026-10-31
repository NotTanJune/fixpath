"""Tests of the read-only endpoints: health, ontology, graph, sources, pipeline, evals and
activity.
"""

import json

import pytest
from api_stubs import NO_KEYS, NOTE_TEXT, WITH_KEYS, make_app
from fastapi.testclient import TestClient
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app import brand, ledger
from app.ontology import NODE_TYPES


@pytest.fixture
def client(tmp_path):
    return TestClient(make_app(tmp_path))


def get_json(client, url, status=200):
    response = client.get(url)
    assert response.status_code == status, response.text
    return response.json()


# --- the app ---


def test_the_title_and_description_come_from_the_brand(tmp_path):
    app = make_app(tmp_path)
    assert app.title == brand.load_brand().name
    assert app.description == brand.load_brand().description


# --- health ---


def test_health_without_keys_is_replay_mode_on_the_cache(client):
    health = get_json(client, "/api/health")
    assert health["keys"] == {"openai": False, "typesafe": False, "langfuse": False}
    assert health["decider"] == "cache"
    assert health["replay_mode"] is True and health["ingest_enabled"] is False
    assert health["budget"] == {"spent_usd": 0.0, "limit_usd": NO_KEYS.budget_usd}


def test_health_with_keys_picks_jev_and_enables_ingest(tmp_path):
    client = TestClient(make_app(tmp_path, WITH_KEYS))
    health = get_json(client, "/api/health")
    assert health["keys"] == {"openai": True, "typesafe": True, "langfuse": True}
    assert health["decider"] == "jev"
    assert health["replay_mode"] is False and health["ingest_enabled"] is True


@pytest.mark.parametrize(
    "setting, backend",
    [
        (dict(openai_api_key="k"), "decisions"),
        (dict(openai_api_key="k", typesafe_api_key="k"), "jev"),
        (
            dict(openai_api_key="k", typesafe_api_key="k", decider="decisions"),
            "decisions",
        ),
        # without an OpenAI key the app is in replay mode, which never makes a live call
        (dict(typesafe_api_key="k"), "cache"),
        (dict(typesafe_api_key="k", decider="jev"), "cache"),
    ],
)
def test_health_names_the_backend_the_app_uses(tmp_path, setting, backend):
    from app.config import Settings

    client = TestClient(make_app(tmp_path, Settings(**setting)))
    assert get_json(client, "/api/health")["decider"] == backend


@pytest.mark.parametrize(
    "setting",
    [
        dict(openai_api_key="k", decider="jev"),
        dict(openai_api_key="k", decider="nonsense"),
    ],
)
def test_health_and_get_decider_agree_on_settings_with_no_usable_backend(
    tmp_path, setting
):
    from app.config import Settings
    from app.decide import get_decider

    settings = Settings(**setting)
    with pytest.raises(ValueError):  # the app refuses them
        get_decider(settings)
    client = TestClient(make_app(tmp_path, settings))
    assert get_json(client, "/api/health")["decider"] == "unavailable"


def test_health_reports_the_spend_in_the_ledger(client):
    ledger.record("gpt-6.1-sol", "x", 1_000_000, 0, 0, False)
    assert get_json(client, "/api/health")["budget"]["spent_usd"] == 2.0


def test_no_response_holds_a_key_or_a_part_of_one(tmp_path):
    client = TestClient(make_app(tmp_path, WITH_KEYS))
    secrets = [
        WITH_KEYS.openai_api_key,
        WITH_KEYS.typesafe_api_key,
        WITH_KEYS.langfuse_public_key,
        WITH_KEYS.langfuse_secret_key,
    ]
    for url in (
        "/api/health",
        "/api/ontology",
        "/api/graph",
        "/api/sources",
        "/api/pipeline/latest",
        "/api/suggestions",
        "/openapi.json",
    ):
        body = client.get(url).text
        for secret in secrets:
            assert secret not in body and secret[:6] not in body, (url, secret[:3])


# --- ontology ---


def test_the_ontology_is_the_exported_file(client):
    assert set(get_json(client, "/api/ontology")["node_types"]) == set(NODE_TYPES)


# --- graph ---


def ids(elements):
    return {n["data"]["id"] for n in elements["nodes"]}


def test_a_focus_at_depth_1_holds_its_direct_neighbours_and_the_edges_between_them(
    client,
):
    elements = get_json(client, "/api/graph?focus=HP-01&depth=1")
    assert ids(elements) == {
        "HP-01",
        "LINE-01",
        "HP-01-MAINCYL",
        "HP-01-RELIEF",
        "HP-01-HYDPUMP",
        "E-217",
        *{f"INC-2026-00{i}" for i in range(1, 6)},
    }
    edge = next(e["data"] for e in elements["edges"] if e["data"]["rel"] == "RAISED")
    assert edge == {
        "id": "HP-01|RAISED|E-217",
        "source": "HP-01",
        "target": "E-217",
        "rel": "RAISED",
        "label": "raised",
    }
    assert all(
        {e["data"]["source"], e["data"]["target"]} <= ids(elements)
        for e in elements["edges"]
    )


def test_depth_2_reaches_the_next_ring(client):
    one = ids(get_json(client, "/api/graph?focus=HP-01&depth=1"))
    two = ids(get_json(client, "/api/graph?focus=HP-01&depth=2"))
    assert one < two and {"FM-SEAL-WEAR", "SEAL-KIT-50"} <= two
    assert "PROC-HP-04" not in two


def test_a_node_carries_its_type_family_and_glyph_from_the_ontology(client):
    elements = get_json(client, "/api/graph?focus=HP-01")
    data = next(n["data"] for n in elements["nodes"] if n["data"]["id"] == "HP-01")
    spec = NODE_TYPES["Asset"]
    assert data == {
        "id": "HP-01",
        "label": "Hydraulic press",
        "type": "Asset",
        "family": spec.family,
        "glyph": spec.glyph,
    }


def test_without_a_focus_the_whole_graph_comes_back(client):
    elements = get_json(client, "/api/graph")
    assert len(elements["nodes"]) == 35 and len(elements["edges"]) == 55


def test_an_unknown_focus_is_a_404_and_a_bad_depth_a_422(client):
    assert "NOPE-99" in client.get("/api/graph?focus=NOPE-99").text
    assert client.get("/api/graph?focus=NOPE-99").status_code == 404
    assert client.get("/api/graph?focus=HP-01&depth=3").status_code == 422


# --- sources ---


def test_the_sources_list_every_document_with_its_size_on_disk(client):
    sources = {s["id"]: s for s in get_json(client, "/api/sources")}
    assert len(sources) == 6
    log = sources["DOC-dddddddd"]
    assert log == {
        "id": "DOC-dddddddd",
        "path": "logs/hp.log",
        "doc_type": "log",
        "bytes": 102,
    }
    assert sources["DOC-ffffffff"]["bytes"] is None  # the out-of-date sheet has no file


def test_a_text_file_comes_back_as_is_with_the_offsets_of_its_quotes(client):
    source = get_json(client, "/api/sources/DOC-dddddddd")
    assert source["kind"] == "text" and source["text"].startswith("01/04/2026")
    (span,) = source["spans"]
    assert source["text"][span["start"] : span["end"]] == "ERR 217" == span["quote"]
    assert (span["fact_kind"], span["fact_id"]) == ("edge", "HP-01|RAISED|E-217")
    assert (span["type"], span["label"]) == ("RAISED", "HP-01 raised E-217")


def test_a_quote_that_is_not_in_the_text_is_listed_without_an_offset(client):
    source = get_json(client, "/api/sources/DOC-cccccccc")
    assert source["text"] == NOTE_TEXT
    by_quote = {s["quote"]: s for s in source["spans"]}
    found = by_quote["note about INC-2026-001"]
    assert source["text"][found["start"] : found["end"]] == found["quote"]
    assert found["type"] == "Incident" and found["label"] == "INC-2026-001"
    missing = by_quote["note about INC-2026-002"]
    assert missing["start"] is None and missing["end"] is None


def test_the_workbook_comes_back_as_rows_of_cells(client):
    source = get_json(client, "/api/sources/DOC-eeeeeeee")
    assert source["kind"] == "workbook"
    (sheet,) = source["sheets"]
    assert sheet["name"] == "Spares" and sheet["rows"][1] == [
        "SEAL-KIT-50",
        "Main cylinder seal kit",
        "3",
        "pc",
        "B12",
        "14",
    ]
    quote = "SEAL-KIT-50 | Main cylinder seal kit | 3 | pc | B12 | 14"
    span = next(s for s in source["spans"] if s["quote"] == quote)
    assert source["text"][span["start"] : span["end"]] == quote
    assert (span["type"], span["label"]) == ("Part", "Main cylinder seal kit")


def test_a_pdf_comes_back_as_page_texts_and_a_quote_is_matched_on_its_page(client):
    source = get_json(client, "/api/sources/DOC-aaaaaaaa")
    assert source["kind"] == "pdf"
    assert [p["page"] for p in source["pages"]] == [1, 2, 3, 4, 5]
    assert "Check the seals" in source["pages"][2]["text"]
    span = next(s for s in source["spans"] if s["quote"] == "Check the seals")
    assert span["page"] == 3
    assert source["text"][span["start"] : span["end"]] == "Check the seals"
    assert span["start"] >= source["pages"][2]["start"]
    assert (span["type"], span["label"]) == (
        "INDICATES",
        "E-217 usually means FM-SEAL-WEAR",
    )


def test_an_unknown_source_and_a_missing_file_are_404s(client):
    assert client.get("/api/sources/DOC-00000000").status_code == 404
    assert client.get("/api/sources/DOC-ffffffff").status_code == 404


# --- pipeline and evals ---


def test_the_pipeline_is_the_last_report_with_the_open_review_queue(client):
    pipeline = get_json(client, "/api/pipeline/latest")
    assert pipeline["totals"] == {"nodes": {"Asset": 3}}
    queue = pipeline["review_queue"]
    assert sorted(r["kind"] for r in queue) == ["conflict", "merge"]
    conflict = next(r for r in queue if r["kind"] == "conflict")
    assert conflict["payload"]["key"] == "WO-1001" and conflict["id"]


def test_no_pipeline_report_is_a_404(tmp_path):
    client = TestClient(make_app(tmp_path))
    (tmp_path / "data" / "pipeline" / "latest.json").unlink()
    assert client.get("/api/pipeline/latest").status_code == 404


def test_evals_are_the_results_file_or_a_404(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/evals/latest").status_code == 404
    path = tmp_path / "evals" / "results" / "latest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"layer1": {"score": 0.9}}))
    assert get_json(client, "/api/evals/latest") == {"layer1": {"score": 0.9}}


def test_the_map_is_the_map_file_or_a_404(tmp_path):
    client = TestClient(make_app(tmp_path))
    response = client.get("/api/map")
    assert response.status_code == 404
    assert "The fact map has not been built. Run make map." in response.text
    path = tmp_path / "data" / "map" / "fact_map.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"points": [], "counts": {"passages": 0}}))
    assert get_json(client, "/api/map") == {"points": [], "counts": {"passages": 0}}


def test_a_database_swapped_between_requests_is_read_on_the_next_one(tmp_path):
    client = TestClient(make_app(tmp_path))
    assert len(get_json(client, "/api/graph")["nodes"]) == 35
    kg = tmp_path / "data" / "kg.sqlite"
    import sqlite3
    from contextlib import closing

    replacement = tmp_path / "swap.sqlite"
    kg.replace(replacement)
    with closing(sqlite3.connect(replacement)) as conn, conn:
        conn.execute("DELETE FROM edges WHERE src = 'LINE-01'")
        conn.execute("DELETE FROM nodes WHERE id = 'RW-01'")
    replacement.replace(kg)
    assert len(get_json(client, "/api/graph")["nodes"]) == 34


# --- activity ---


def edit_graph(tmp_path, *statements):
    """Run SQL statements on the stub graph through a plain write connection."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(tmp_path / "data" / "kg.sqlite")) as conn, conn:
        for sql, params in statements:
            conn.execute(sql, params)


def test_activity_lists_the_latest_breakdowns_newest_first(client):
    activity = get_json(client, "/api/activity")
    assert set(activity) == {"breakdowns", "alarms"}
    rows = activity["breakdowns"]
    assert [r["id"] for r in rows] == [f"INC-2026-00{n}" for n in range(7, 0, -1)]
    assert rows[0]["date"] == "2026-09-01"


def test_a_breakdown_row_carries_what_the_rail_shows_and_the_first_source(client):
    rows = {r["id"]: r for r in get_json(client, "/api/activity")["breakdowns"]}
    assert rows["INC-2026-001"] == {
        "id": "INC-2026-001",
        "date": "2026-04-19",
        "shift": "C",
        "downtime_min": 240,
        "machine": "HP-01",
        "machine_name": "Hydraulic press",
        "alarm": "E-217",
        "alarm_message": "Pressure low",
        "cause_id": "FM-SEAL-WEAR",
        "cause_name": "main cylinder seal wear",
        "symptom": "press losing pressure mid-stroke, oil on the floor",
        "doc_id": "DOC-cccccccc",
    }


def test_a_breakdown_with_missing_values_gives_null_for_each(client):
    rows = {r["id"]: r for r in get_json(client, "/api/activity")["breakdowns"]}
    no_alarm = rows["INC-2026-004"]
    assert no_alarm["alarm"] is None and no_alarm["alarm_message"] is None
    no_shift = rows["INC-2026-003"]
    assert no_shift["shift"] is None and no_shift["downtime_min"] is None


def test_a_breakdown_without_evidence_has_no_doc_id(tmp_path):
    client = TestClient(make_app(tmp_path))
    edit_graph(
        tmp_path,
        ("DELETE FROM evidence WHERE fact_id = ?", ("INC-2026-007",)),
    )
    rows = {r["id"]: r for r in get_json(client, "/api/activity")["breakdowns"]}
    assert rows["INC-2026-007"]["doc_id"] is None
    assert rows["INC-2026-006"]["doc_id"] == "DOC-cccccccc"


def test_two_breakdowns_on_one_day_sort_by_id_newest_first(tmp_path):
    client = TestClient(make_app(tmp_path))
    edit_graph(
        tmp_path,
        (
            (
                "UPDATE nodes SET props = json_set(props, '$.date', '2026-09-01')"
                " WHERE id = 'INC-2026-006'"
            ),
            (),
        ),
    )
    ids_ = [r["id"] for r in get_json(client, "/api/activity")["breakdowns"]]
    assert ids_[:2] == ["INC-2026-007", "INC-2026-006"]


def test_the_breakdown_limit_defaults_to_eight_and_can_be_set(client):
    assert (
        len(get_json(client, "/api/activity")["breakdowns"]) == 7
    )  # the stub has seven
    rows = get_json(client, "/api/activity?limit=3")["breakdowns"]
    assert [r["id"] for r in rows] == ["INC-2026-007", "INC-2026-006", "INC-2026-005"]
    get_json(client, "/api/activity?limit=0", status=422)
    get_json(client, "/api/activity?limit=100", status=422)


def test_the_default_breakdown_limit_is_eight(tmp_path):
    client = TestClient(make_app(tmp_path))
    edit_graph(
        tmp_path,
        *[
            (
                "INSERT INTO nodes(id, type, label, props) VALUES (?, 'Incident', ?, ?)",
                (
                    f"INC-2026-1{n:02d}",
                    "x",
                    json.dumps({"id": f"INC-2026-1{n:02d}", "date": "2026-10-01"}),
                ),
            )
            for n in range(5)
        ],
    )
    rows = get_json(client, "/api/activity")["breakdowns"]
    assert len(rows) == 8
    assert rows[0]["id"] == "INC-2026-104"
    assert rows[0]["machine"] is None and rows[0]["cause_id"] is None


def test_activity_lists_the_latest_alarms_newest_first(tmp_path):
    client = TestClient(make_app(tmp_path))
    edit_graph(
        tmp_path,
        (
            "INSERT INTO edges(id, src, rel, dst, props) VALUES (?, ?, 'RAISED', ?, ?)",
            (
                "CNC-01|RAISED|E-214",
                "CNC-01",
                "E-214",
                json.dumps(
                    {
                        "count": 2,
                        "first_seen": "2026-09-29T01:00:00+00:00",
                        "last_seen": "2026-09-30T01:00:00+00:00",
                    }
                ),
            ),
        ),
    )
    alarms = get_json(client, "/api/activity")["alarms"]
    assert alarms == [
        {
            "machine": "CNC-01",
            "machine_name": "Lathe",
            "alarm": "E-214",
            "alarm_message": "Hot",
            "severity": "critical",
            "last_seen": "2026-09-30T01:00:00+00:00",
            "count": 2,
            "doc_id": None,
        },
        {
            "machine": "HP-01",
            "machine_name": "Hydraulic press",
            "alarm": "E-217",
            "alarm_message": "Pressure low",
            "severity": "critical",
            "last_seen": "2026-09-28T09:51:40+00:00",
            "count": 15,
            "doc_id": "DOC-dddddddd",
        },
    ]


def test_the_alarm_limit_is_five_and_can_be_set(tmp_path):
    client = TestClient(make_app(tmp_path))
    edit_graph(
        tmp_path,
        *[
            (
                "INSERT INTO nodes(id, type, label, props) VALUES (?, 'AlarmCode', ?, '{}')",
                (f"X-{n}", f"Alarm {n}"),
            )
            for n in range(7)
        ],
        *[
            (
                "INSERT INTO edges(id, src, rel, dst, props) VALUES (?, 'CNC-01', 'RAISED', ?, ?)",
                (
                    f"CNC-01|RAISED|X-{n}",
                    f"X-{n}",
                    json.dumps(
                        {"count": 1, "last_seen": f"2026-10-0{n + 1}T00:00:00+00:00"}
                    ),
                ),
            )
            for n in range(7)
        ],
    )
    alarms = get_json(client, "/api/activity")["alarms"]
    assert len(alarms) == 5
    assert alarms[0]["alarm"] == "X-6"
    assert alarms[0]["alarm_message"] == "Alarm 6"  # the label used when there is no text
    assert alarms[0]["severity"] is None
    assert len(get_json(client, "/api/activity?alarm_limit=2")["alarms"]) == 2
    get_json(client, "/api/activity?alarm_limit=0", status=422)


def test_activity_without_a_graph_is_a_404(tmp_path):
    app = make_app(tmp_path)
    (tmp_path / "data" / "kg.sqlite").unlink()
    response = TestClient(app).get("/api/activity")
    assert response.status_code == 404
    assert "no graph database" in response.json()["detail"]


# --- a database that stays locked ---


def test_a_database_locked_past_the_busy_timeout_is_a_503_with_a_plain_message(
    tmp_path, monkeypatch
):
    import sqlite3
    from contextlib import closing

    from app.api import views

    client = TestClient(make_app(tmp_path))
    kg_path = tmp_path / "data" / "kg.sqlite"
    monkeypatch.setattr(views, "BUSY_TIMEOUT_MS", 100)
    with closing(sqlite3.connect(kg_path, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode = DELETE")
        writer.execute("BEGIN EXCLUSIVE")
        for url in ("/api/graph", "/api/sources", "/api/activity"):
            response = client.get(url)
            assert response.status_code == 503, url
            assert response.json() == {"detail": views.BUSY}
        writer.execute("ROLLBACK")
    assert client.get("/api/graph").status_code == 200  # works again once the lock is released


def test_other_database_errors_are_not_taken_for_a_busy_database():
    import sqlite3

    from app.api import views

    assert views.is_busy(sqlite3.OperationalError("database is locked"))
    assert not views.is_busy(sqlite3.OperationalError("no such table: nodes"))


def test_a_workbook_source_carries_the_styles_of_its_sheets(client):
    source = get_json(client, "/api/sources/DOC-eeeeeeee")
    assert list(source["styles"]) == ["Spares"]
    assert set(source["styles"]["Spares"]) == {
        "columns",
        "rows",
        "merged",
        "freeze",
        "cells",
    }


def test_only_a_workbook_source_has_styles(client):
    assert "styles" not in get_json(client, "/api/sources/DOC-aaaaaaaa")
    assert "styles" not in get_json(client, "/api/sources/DOC-cccccccc")
