"""Tests for the record of what each generated file mentions. The renderers return it
and the evals read it."""

import json

import pytest

from app.generator.mentions import (
    Conflict,
    FileMentions,
    ManualMentions,
    file_key,
    mentions_to_json,
    merge,
    write_mentions,
)

# --- The mentions record -----------------------------------------------------------


def test_a_file_mentions_nothing_by_default():
    assert FileMentions().model_dump() == {
        "incidents": [],
        "assets": [],
        "alarm_codes": [],
        "parts": [],
        "conflicts": [],
    }


def test_file_mentions_sort_and_deduplicate_every_list():
    mention = FileMentions(
        incidents=["INC-2026-002", "INC-2026-001", "INC-2026-002"],
        assets=["HP-01", "CNC-01", "HP-01"],
        alarm_codes=["E-217", "E-200", "E-217"],
        parts=["SEAL-KIT-50", "BRG-7014-ACD", "SEAL-KIT-50"],
        conflicts=[
            Conflict(incident="INC-2026-002", says_asset="CNC-01"),
            Conflict(incident="INC-2026-001", says_asset="CNC-02"),
            Conflict(incident="INC-2026-002", says_asset="CNC-01"),
        ],
    )
    assert mention.incidents == ["INC-2026-001", "INC-2026-002"]
    assert mention.assets == ["CNC-01", "HP-01"]
    assert mention.alarm_codes == ["E-200", "E-217"]
    assert mention.parts == ["BRG-7014-ACD", "SEAL-KIT-50"]
    assert [(c.incident, c.says_asset) for c in mention.conflicts] == [
        ("INC-2026-001", "CNC-02"),
        ("INC-2026-002", "CNC-01"),
    ]


def test_file_key_is_relative_to_the_output_root_with_forward_slashes(tmp_path):
    path = tmp_path / "raw" / "logs" / "cnc-02 alarm export.log"
    assert file_key(tmp_path, path) == "raw/logs/cnc-02 alarm export.log"


def test_merge_combines_the_files_of_every_renderer():
    logs = {"raw/logs/HP01.log": FileMentions(assets=["HP-01"])}
    notes = {"raw/handovers/a.txt": FileMentions(incidents=["INC-2026-001"])}
    merged = merge(logs, notes)
    assert sorted(merged) == ["raw/handovers/a.txt", "raw/logs/HP01.log"]
    assert merged["raw/logs/HP01.log"].assets == ["HP-01"]


def test_merge_refuses_two_renderers_that_wrote_the_same_file():
    first = {"raw/logs/HP01.log": FileMentions(assets=["HP-01"])}
    second = {"raw/logs/HP01.log": FileMentions(assets=["CNC-01"])}
    with pytest.raises(ValueError, match="raw/logs/HP01.log"):
        merge(first, second)


def test_mentions_json_is_a_flat_map_with_sorted_keys_and_a_trailing_newline():
    text = mentions_to_json(
        {
            "raw/logs/b.log": FileMentions(assets=["HP-01"], alarm_codes=["E-217"]),
            "raw/handovers/a.txt": FileMentions(
                incidents=["INC-2026-001"],
                conflicts=[Conflict(incident="INC-2026-001", says_asset="CNC-01")],
            ),
        }
    )
    assert text.endswith("}\n") and not text.endswith("\n\n")
    data = json.loads(text)
    assert list(data) == ["raw/handovers/a.txt", "raw/logs/b.log"]
    assert data["raw/handovers/a.txt"] == {
        "alarm_codes": [],
        "assets": [],
        "conflicts": [{"incident": "INC-2026-001", "says_asset": "CNC-01"}],
        "incidents": ["INC-2026-001"],
        "parts": [],
    }
    assert text.startswith('{\n  "raw/handovers/a.txt": {\n    "alarm_codes"')


def test_write_mentions_creates_the_folder_and_returns_the_path(tmp_path):
    target = tmp_path / "truth" / "mentions.json"
    written = write_mentions({"raw/logs/b.log": FileMentions(assets=["HP-01"])}, target)
    assert written == target
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "raw/logs/b.log": {
            "alarm_codes": [],
            "assets": ["HP-01"],
            "conflicts": [],
            "incidents": [],
            "parts": [],
        }
    }


# --- The mentions of a vendor manual ------------------------------------------------


def test_a_manual_mentions_two_more_lists_than_other_files():
    assert ManualMentions().model_dump() == {
        "incidents": [],
        "assets": [],
        "alarm_codes": [],
        "parts": [],
        "conflicts": [],
        "failure_modes": [],
        "procedures": [],
    }


def test_a_manual_is_a_file_mentions_with_sorted_unique_failure_modes_and_procedures():
    mention = ManualMentions(
        assets=["HP-01"],
        failure_modes=["FM-SEAL-WEAR", "FM-OIL-CONTAM", "FM-SEAL-WEAR"],
        procedures=["PROC-HP-05", "PROC-HP-04", "PROC-HP-05"],
        parts=["SEAL-KIT-50", "FLT-HYD-10", "SEAL-KIT-50"],
    )
    assert isinstance(mention, FileMentions)
    assert mention.failure_modes == ["FM-OIL-CONTAM", "FM-SEAL-WEAR"]
    assert mention.procedures == ["PROC-HP-04", "PROC-HP-05"]
    assert mention.parts == ["FLT-HYD-10", "SEAL-KIT-50"]


def test_mentions_json_gives_only_a_manual_the_two_extra_lists():
    text = mentions_to_json(
        {
            "raw/logs/b.log": FileMentions(assets=["HP-01"]),
            "raw/manuals/m.pdf": ManualMentions(
                assets=["HP-01"],
                failure_modes=["FM-SEAL-WEAR"],
                procedures=["PROC-HP-04"],
            ),
        }
    )
    data = json.loads(text)
    other_keys = {"alarm_codes", "assets", "conflicts", "incidents", "parts"}
    assert set(data["raw/logs/b.log"]) == other_keys
    assert set(data["raw/manuals/m.pdf"]) == other_keys | {
        "failure_modes",
        "procedures",
    }
    assert data["raw/manuals/m.pdf"]["failure_modes"] == ["FM-SEAL-WEAR"]
    assert data["raw/manuals/m.pdf"]["procedures"] == ["PROC-HP-04"]


def test_merge_keeps_a_manual_record_as_it_is():
    manual = ManualMentions(procedures=["PROC-HP-04"])
    merged = merge({"raw/manuals/m.pdf": manual}, {"raw/logs/b.log": FileMentions()})
    assert merged["raw/manuals/m.pdf"] is manual
