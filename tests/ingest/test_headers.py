"""Tests for finding the header row of a table and mapping its columns to fields. Known
synonyms map the real files with no model. One model call fills any gaps, and the
tests use a fake model for it."""

from ingest_stubs import (
    MACHINE_HEADERS,
    MAINTENANCE_HEADERS,
    SPARES_HEADERS,
    UNKNOWN_MAINTENANCE,
    UNKNOWN_SPARES,
    FakeModel,
    no_model,  # noqa: F401  (an autouse fixture)
    spares_answer,
)

from app.ingest import headers, layouts
from app.ingest.headers import HeaderChoice, HeaderMap, Table

# --- Synonym mapping ------------------------------------------------------------


def test_synonyms_map_every_real_header_row():
    assert headers.map_headers(layouts.SPARES, SPARES_HEADERS) == {
        "part_no": 0,
        "description": 1,
        "qty": 2,
        "uom": 3,
        "bin": 4,
        "lead_time": 5,
        "cost": 6,
        "remarks": 7,
    }
    assert headers.map_headers(layouts.SPARES, ["PN", "Desc", "OnHand", "Loc"]) == {
        "part_no": 0,
        "description": 1,
        "qty": 2,
        "bin": 3,
    }
    assert headers.map_headers(layouts.MACHINES, MACHINE_HEADERS) == {
        "tag": 0,
        "description": 1,
        "make": 2,
        "model": 3,
        "serial": 4,
        "location": 5,
        "aliases": 6,
    }
    assert headers.map_headers(layouts.MAINTENANCE, MAINTENANCE_HEADERS) == {
        "date": 0,
        "machine": 1,
        "problem": 2,
        "action": 3,
        "parts": 4,
        "tech": 5,
        "downtime": 6,
        "ref": 7,
    }


def test_a_header_key_is_lower_case_words():
    assert headers.header_key("Cost (SGD)") == "cost sgd"
    assert headers.header_key("  Part No. ") == "part no"
    assert headers.header_key("Downtime (min)") == "downtime min"
    assert headers.header_key(None) == ""
    assert headers.header_key(12) == "12"


def test_a_column_holds_one_field_and_the_leftmost_header_wins():
    # A header serves one field, and a repeat of it goes unused.
    assert headers.map_headers(layouts.SPARES, ["Qty", "Qty", "PN"]) == {
        "part_no": 2,
        "qty": 0,
    }
    assert headers.map_headers(layouts.SPARES, [None, "", 5]) == {}


def test_the_same_header_can_mean_different_fields_in_different_layouts():
    assert headers.map_headers(layouts.SPARES, ["Location"]) == {"bin": 0}
    assert headers.map_headers(layouts.MACHINES, ["Location"]) == {"location": 0}
    assert headers.map_headers(layouts.MAINTENANCE, ["Description"]) == {"problem": 0}
    assert headers.map_headers(layouts.MACHINES, ["Description"]) == {"description": 0}


def test_the_required_fields_of_each_layout():
    def required(layout):
        return [c.name for c in layout.columns if c.required]

    assert required(layouts.SPARES) == ["part_no", "description", "qty", "bin"]
    assert required(layouts.MACHINES) == [c.name for c in layouts.MACHINES.columns]
    assert required(layouts.MAINTENANCE) == ["date", "machine", "problem", "ref"]
    for layout in layouts.LAYOUTS:
        assert layout.column(layout.anchor).required
        assert layout.column("nope") is None


# --- Header rows ----------------------------------------------------------------


def test_the_header_row_is_the_first_row_with_a_part_number_header():
    title_gap_and_headers = [["Line 3 spares, stock count"], [], SPARES_HEADERS]
    assert headers.header_row(layouts.SPARES, title_gap_and_headers) == 2
    assert headers.header_row(layouts.SPARES, [["stock count"], ["PN", "Desc"]]) == 1
    assert headers.header_row(layouts.SPARES, [["stock count"], ["a", "b"]]) is None
    assert headers.header_row(layouts.SPARES, []) is None


def test_a_header_row_needs_a_second_recognised_header_beside_the_anchor():
    assert headers.header_row(layouts.SPARES, [["Part"], ["PN", "x"]]) is None
    assert headers.header_row(layouts.SPARES, [["Part", "Qty"]]) == 0


def test_the_header_row_search_stops_after_the_limit():
    rows = [["x"]] * 5 + [["PN", "Desc"]]
    assert headers.header_row(layouts.SPARES, rows) == 5
    assert headers.header_row(layouts.SPARES, rows, limit=5) is None


def test_a_guessed_header_row_has_two_cells_and_no_numbers():
    rows = [
        ["stock count 01.07.2026"],
        [None, None],
        ["Item Ref", "What", "How many", "Where"],
        ["BRG-1", "Seal", 3, "A02"],
    ]
    assert headers.guess_header_row(rows) == 2
    assert headers.guess_header_row([["BRG-1", "Seal", 3, "A02"]]) is None
    assert headers.guess_header_row([["a", "1"], ["a", "b"]]) == 1
    assert headers.guess_header_row([["a", "2.5"], ["a", "b"]]) == 1
    assert headers.guess_header_row([]) is None


def test_sniff_finds_the_layout_and_the_delimiter_of_the_header_line():
    spares = ["stock count 01.07.2026", "PN;Desc;OnHand;Loc", "A-1;x;1;A01"]
    assert headers.sniff(spares) == (layouts.SPARES, ";")
    export = ["Date,Mach.,Problem desc,Ref", "2026-05-01,CNC-01,x,WO-1"]
    assert headers.sniff(export) == (layouts.MAINTENANCE, ",")
    machines = ["Asset tag\tDescription\tModel"]
    assert headers.sniff(machines) == (layouts.MACHINES, "\t")
    assert headers.sniff(["just some words", "and some more"]) is None
    assert headers.sniff([]) is None


# --- The model fallback ---------------------------------------------------------


def test_the_unknown_headers_precondition_holds():
    assert headers.map_headers(layouts.SPARES, UNKNOWN_SPARES) == {}
    assert headers.map_headers(layouts.MAINTENANCE, UNKNOWN_MAINTENANCE) == {}


def test_an_unknown_layout_asks_the_model_exactly_once():
    fake = FakeModel(spares_answer())
    mapped = headers.map_columns(
        {"spares": Table(layouts.SPARES, UNKNOWN_SPARES)}, ask=fake
    )
    assert len(fake.calls) == 1
    assert mapped == {"spares": {"part_no": 0, "description": 1, "qty": 2, "bin": 3}}
    call = fake.calls[0]
    assert call["role"] == "sol"
    assert call["schema"] is HeaderMap
    assert call["purpose"] == "map headers"
    assert call["reasoning_effort"] == "low"
    for header in UNKNOWN_SPARES:
        assert header in call["user"]


def test_the_model_gets_the_sample_rows_to_look_at():
    fake = FakeModel(spares_answer())
    table = Table(layouts.SPARES, UNKNOWN_SPARES, [["X-1", "Seal", "3", "A02"]])
    headers.map_columns({"spares": table}, ask=fake)
    assert "X-1" in fake.calls[0]["user"]


def test_the_request_is_the_same_each_time_so_the_cache_can_answer_a_repeat():
    first, second = FakeModel(spares_answer()), FakeModel(spares_answer())
    table = Table(layouts.SPARES, UNKNOWN_SPARES, [["X-1", "Seal", "3", "A02"]])
    headers.map_columns({"spares": table}, ask=first)
    headers.map_columns({"spares": table}, ask=second)
    assert first.calls == second.calls


def test_a_layout_that_maps_fully_never_reaches_the_model():
    fake = FakeModel()
    mapped = headers.map_columns(
        {
            "spares": Table(layouts.SPARES, SPARES_HEADERS),
            "machines": Table(layouts.MACHINES, MACHINE_HEADERS),
            "maintenance": Table(layouts.MAINTENANCE, MAINTENANCE_HEADERS),
        },
        ask=fake,
    )
    assert fake.calls == []
    assert mapped["spares"]["part_no"] == 0
    assert mapped["machines"]["tag"] == 0
    assert mapped["maintenance"]["ref"] == 7


def test_an_optional_field_left_unmapped_does_not_call_the_model():
    fake = FakeModel()
    old_count = ["PN", "Desc", "OnHand", "Loc"]  # no lead time or cost column
    headers.map_columns({"spares": Table(layouts.SPARES, old_count)}, ask=fake)
    assert fake.calls == []


def test_two_unknown_tables_in_one_file_still_make_one_call():
    fake = FakeModel(
        HeaderMap(
            choices=[
                HeaderChoice(table="spares", field="part_no", header="Item Ref"),
                HeaderChoice(table="machines", field="tag", header="Unit"),
            ]
        )
    )
    mapped = headers.map_columns(
        {
            "spares": Table(layouts.SPARES, UNKNOWN_SPARES),
            "machines": Table(layouts.MACHINES, ["Unit", "Kind"]),
        },
        ask=fake,
    )
    assert len(fake.calls) == 1
    assert "Table spares" in fake.calls[0]["user"]
    assert "Table machines" in fake.calls[0]["user"]
    assert mapped["spares"]["part_no"] == 0
    assert mapped["machines"] == {"tag": 0}


def test_only_the_tables_with_a_gap_are_put_to_the_model():
    fake = FakeModel(spares_answer())
    headers.map_columns(
        {
            "machines": Table(layouts.MACHINES, MACHINE_HEADERS),
            "spares": Table(layouts.SPARES, UNKNOWN_SPARES),
        },
        ask=fake,
    )
    assert "Table spares" in fake.calls[0]["user"]
    assert "Table machines" not in fake.calls[0]["user"]


def test_a_synonym_match_beats_the_model_and_only_gaps_go_to_it():
    fake = FakeModel(
        HeaderMap(
            choices=[
                HeaderChoice(table="spares", field="part_no", header="Qty"),
                HeaderChoice(table="spares", field="bin", header="Where"),
            ]
        )
    )
    mapped = headers.map_columns(
        {"spares": Table(layouts.SPARES, ["Part No.", "Description", "Qty", "Where"])},
        ask=fake,
    )
    assert len(fake.calls) == 1
    assert "- part_no" not in fake.calls[0]["user"]  # it already had a column
    assert "- bin" in fake.calls[0]["user"]
    assert mapped["spares"] == {"part_no": 0, "description": 1, "qty": 2, "bin": 3}


def test_answers_that_do_not_fit_the_file_are_ignored():
    fake = FakeModel(
        HeaderMap(
            choices=[
                HeaderChoice(table="spares", field="part_no", header="Not a header"),
                HeaderChoice(table="spares", field="bogus", header="What"),
                HeaderChoice(table="other", field="qty", header="How many"),
                HeaderChoice(table="spares", field="description", header="what"),
                HeaderChoice(table="spares", field="qty", header="What"),
            ]
        )
    )
    mapped = headers.map_columns(
        {"spares": Table(layouts.SPARES, UNKNOWN_SPARES)}, ask=fake
    )
    # a header is matched ignoring case, and one column holds one field only
    assert mapped["spares"] == {"description": 1}


def test_a_table_the_model_was_not_asked_about_is_left_alone():
    # The old count has every required column and no cost column, so only the
    # machines table has a gap. A cost answer for the old count is not applied.
    fake = FakeModel(
        HeaderMap(
            choices=[
                HeaderChoice(table="spares", field="cost", header="Price tag"),
                HeaderChoice(table="machines", field="make", header="Maker's name"),
            ]
        )
    )
    mapped = headers.map_columns(
        {
            "spares": Table(
                layouts.SPARES, ["PN", "Desc", "OnHand", "Loc", "Price tag"]
            ),
            "machines": Table(layouts.MACHINES, ["Asset tag", "Maker's name"]),
        },
        ask=fake,
    )
    assert len(fake.calls) == 1
    assert "Table spares" not in fake.calls[0]["user"]
    assert "cost" not in mapped["spares"]
    assert mapped["machines"] == {"tag": 0, "make": 1}


def test_the_missing_required_fields_are_listed_after_the_model_has_answered():
    fake = FakeModel(HeaderMap(choices=[]))
    mapped = headers.map_columns(
        {"spares": Table(layouts.SPARES, UNKNOWN_SPARES)}, ask=fake
    )["spares"]
    assert [f.name for f in headers.missing_required(layouts.SPARES, mapped)] == [
        "part_no",
        "description",
        "qty",
        "bin",
    ]


def test_the_default_ask_is_app_llm_structured(monkeypatch):
    from app import llm

    fake = FakeModel(spares_answer())
    monkeypatch.setattr(llm, "structured", fake)
    mapped = headers.map_columns({"spares": Table(layouts.SPARES, UNKNOWN_SPARES)})
    assert len(fake.calls) == 1
    assert mapped["spares"]["part_no"] == 0


def test_the_settings_of_the_caller_go_to_the_default_ask(monkeypatch):
    from app import llm
    from app.config import Settings

    fake = FakeModel(spares_answer())
    monkeypatch.setattr(llm, "structured", fake)
    mine = Settings(budget_usd=3.0)
    headers.map_columns(
        {"spares": Table(layouts.SPARES, UNKNOWN_SPARES)}, settings=mine
    )
    assert fake.calls[0]["settings"] is mine


def test_the_header_map_schema_fits_strict_structured_output():
    # Strict json_schema wants every field required, and an optional value written
    # as X | None with no default, in nested models too.
    schema = HeaderMap.model_json_schema()
    assert schema["required"] == ["choices"]
    choice = schema["$defs"]["HeaderChoice"]
    assert choice["required"] == ["table", "field", "header"]
    assert all("default" not in prop for prop in choice["properties"].values())
    header_types = [t["type"] for t in choice["properties"]["header"]["anyOf"]]
    assert sorted(header_types) == ["null", "string"]
