"""Tests for a whole run of the data generator: same output every time, the mentions
file and the command line. They also check that the committed data matches a fresh
render."""

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from app.config import DATA_DIR
from app.generator import __main__ as cli
from app.generator.mentions import file_key, mentions_to_json, merge
from app.generator.render_inventory import render_inventory
from app.generator.render_logs import render_logs
from app.generator.render_manuals import render_manuals
from app.generator.render_notes import render_notes
from app.generator.world import World, build_world

DETERMINISM_SEED = 42

FILES_PER_FOLDER = {
    "logs": 5,
    "handovers": 90,
    "maintenance": 6,
    "inventory": 2,
    "manuals": 4,
}
FILE_COUNT = sum(FILES_PER_FOLDER.values())


def render_all(world: World, out: Path):
    """Four renders of the world, each with a different seed offset."""
    return merge(
        render_logs(world, random.Random(world.seed + 1), out),
        render_notes(world, random.Random(world.seed + 2), out),
        render_inventory(world, random.Random(world.seed + 3), out),
        render_manuals(world, random.Random(world.seed + 4), out),
    )


def raw_files(out: Path) -> list[Path]:
    return sorted(path for path in (out / "raw").rglob("*") if path.is_file())


def content_of(out: Path) -> dict[str, object]:
    """The bytes of every text file and every PDF, and the cell values of the
    workbook. reportlab in invariant mode repeats the bytes of a PDF."""
    found: dict[str, object] = {}
    for path in raw_files(out):
        key = file_key(out, path)
        if path.suffix == ".xlsx":
            workbook = load_workbook(path)
            found[key] = {
                sheet.title: [[c.value for c in row] for row in sheet.iter_rows()]
                for sheet in workbook
            }
        else:
            assert path.suffix in (".log", ".txt", ".csv", ".pdf"), path
            found[key] = path.read_bytes()
    return found


@pytest.fixture(scope="module")
def determinism_runs(tmp_path_factory):
    world_42 = build_world(DETERMINISM_SEED)
    runs = []
    for name in ("first", "second"):
        out = tmp_path_factory.mktemp(name)
        runs.append((out, render_all(world_42, out)))
    return runs


def test_two_renders_with_seed_42_give_identical_files_and_cell_values(
    determinism_runs,
):
    (first, first_mentions), (second, second_mentions) = determinism_runs
    assert content_of(first) == content_of(second)
    assert mentions_to_json(first_mentions) == mentions_to_json(second_mentions)
    assert len(content_of(first)) == FILE_COUNT == 107


def test_another_seed_gives_another_set_of_notes(determinism_runs, tmp_path):
    first, _ = determinism_runs[0]
    render_all(build_world(DETERMINISM_SEED + 1), tmp_path)
    assert content_of(first) != content_of(tmp_path)


def test_a_render_ignores_and_leaves_alone_the_global_random_state(
    world, tmp_path_factory
):
    random.seed(1)
    state = random.getstate()
    first = content_of_after_render(world, tmp_path_factory.mktemp("global_a"))
    assert random.getstate() == state
    random.seed(2)
    assert content_of_after_render(world, tmp_path_factory.mktemp("global_b")) == first


def content_of_after_render(world: World, out: Path):
    render_all(world, out)
    return content_of(out)


def test_the_files_do_not_depend_on_the_hash_seed(determinism_runs, tmp_path):
    """Set order changes with the hash seed of each process, so a render that relied on
    it would differ between processes."""
    expected, _ = determinism_runs[0]
    for hash_seed in ("0", "4242"):
        out = tmp_path / hash_seed
        result = subprocess.run(
            [sys.executable, "-m", "app.generator", "--seed", "42", "--out", str(out)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert content_of(out) == content_of(expected), hash_seed


def test_the_file_count_per_raw_folder(tmp_path):
    cli.main(["--seed", "42", "--out", str(tmp_path)])
    for folder, count in FILES_PER_FOLDER.items():
        assert len([p for p in (tmp_path / "raw" / folder).iterdir()]) == count, folder


def test_the_command_line_writes_the_raw_files_and_mentions_json(tmp_path, capsys):
    assert cli.main(["--seed", "42", "--out", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("wrote ") and "world.json" in lines[0]
    assert any("mentions.json" in line for line in lines[1:])
    mentions = json.loads(
        (tmp_path / "truth" / "mentions.json").read_text(encoding="utf-8")
    )
    listed = set(mentions)
    written = {file_key(tmp_path, path) for path in raw_files(tmp_path)}
    assert listed == written and len(written) == FILE_COUNT
    # Every incident is mentioned by a handover, an export row or a loose note.
    mentioned = {i for mention in mentions.values() for i in mention["incidents"]}
    incidents = {
        n.id for n in build_world(DETERMINISM_SEED).nodes if n.type == "Incident"
    }
    assert mentioned == incidents and len(incidents) == 60


def test_the_command_line_uses_offsets_1_to_4_on_the_seed(tmp_path_factory):
    direct = tmp_path_factory.mktemp("direct")
    via_cli = tmp_path_factory.mktemp("cli")
    mentions = render_all(build_world(42), direct)
    cli.main(["--seed", "42", "--out", str(via_cli)])
    assert content_of(via_cli) == content_of(direct)
    written = (via_cli / "truth" / "mentions.json").read_text(encoding="utf-8")
    assert written == mentions_to_json(mentions)


def test_the_command_line_replaces_exactly_the_five_raw_folders(tmp_path):
    stale = [
        tmp_path / "raw" / "logs" / "stale.log",
        tmp_path / "raw" / "logs" / "sub" / "deeper.log",
        tmp_path / "raw" / "handovers" / "old note.txt",
        tmp_path / "raw" / "maintenance" / "old.csv",
        tmp_path / "raw" / "inventory" / "old.xlsx",
        tmp_path / "raw" / "manuals" / "old manual.pdf",
        tmp_path / "raw" / "manuals" / "sub" / "deeper.pdf",
    ]
    kept = [
        tmp_path / "raw" / "other" / "manual.pdf",
        tmp_path / "raw" / "readme.txt",
        tmp_path / "truth" / "other.json",
        tmp_path / "notes.txt",
    ]
    for path in [*stale, *kept]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    cli.main(["--seed", "42", "--out", str(tmp_path)])
    assert not any(path.exists() for path in stale)
    assert [path.read_text(encoding="utf-8") for path in kept] == [p.name for p in kept]
    for folder, count in FILES_PER_FOLDER.items():
        assert len(list((tmp_path / "raw" / folder).iterdir())) == count


def test_running_the_command_line_twice_gives_the_same_files(tmp_path):
    cli.main(["--seed", "42", "--out", str(tmp_path)])
    first = content_of(tmp_path)
    cli.main(["--seed", "42", "--out", str(tmp_path)])
    assert content_of(tmp_path) == first


# --- The committed data is the default seed render ---------------------------------

TEXT_SUFFIXES = (".log", ".txt", ".csv")
MENTIONS_KEY = "truth/mentions.json"
WORKBOOK_KEY = "raw/inventory/spares_2026-10.xlsx"
MANUALS_PREFIX = "raw/manuals/"


def generated_files(root: Path) -> dict[str, bytes]:
    """The bytes of every file the generator writes under root, keyed by path. It skips
    world.json, which another test checks, and other folders under raw."""
    files = {
        file_key(root, path): path.read_bytes()
        for folder in cli.RAW_FOLDERS
        for path in sorted((root / "raw" / folder).rglob("*"))
        if path.is_file()
    }
    mentions = root / MENTIONS_KEY
    if mentions.is_file():
        files[MENTIONS_KEY] = mentions.read_bytes()
    return files


def select(files: dict[str, bytes], keep) -> dict[str, bytes]:
    return {key: data for key, data in files.items() if keep(key)}


def drift(committed: dict[str, bytes], fresh: dict[str, bytes]) -> list[str]:
    """What differs between the committed files and a fresh render, one line each.
    A file the render has and the repo lacks is missing. A file the repo has and the
    render lacks is unexpected."""

    def reason(key: str) -> str:
        if key not in committed:
            return "missing"
        return "unexpected" if key not in fresh else "differs"

    return [
        f"{key} ({reason(key)})"
        for key in sorted(committed.keys() | fresh.keys())
        if committed.get(key) != fresh.get(key)
    ]


def out_of_date(problems: list[str]) -> str:
    return "committed data is out of date with the generator: run `make data`: " + (
        ", ".join(problems)
    )


@pytest.fixture(scope="module")
def fresh_data(tmp_path_factory):
    """A fresh render with the default seed, made in a temporary folder. The repo's data
    folder is only read."""
    out = tmp_path_factory.mktemp("fresh")
    assert cli.main(["--out", str(out)]) == 0
    return generated_files(out)


@pytest.fixture(scope="module")
def committed_data():
    return generated_files(DATA_DIR)


def test_the_drift_check_reports_missing_unexpected_and_changed_files():
    committed = {"a": b"1", "b": b"2", "c": b"3", "same": b"="}
    fresh = {"a": b"1", "b": b"changed", "d": b"4", "same": b"="}
    assert drift(committed, fresh) == ["b (differs)", "c (unexpected)", "d (missing)"]
    assert drift(fresh, fresh) == []


def test_the_failure_message_says_to_run_make_data():
    message = out_of_date(["raw/logs/HP01.log (differs)"])
    assert "run `make data`" in message and "raw/logs/HP01.log (differs)" in message


def test_the_generated_files_leave_out_the_other_raw_folders_and_world_json(tmp_path):
    for name in (
        "raw/logs/a.log",
        "raw/maintenance/sub/b.csv",
        "raw/manuals/manual.pdf",
        "raw/other/manual.pdf",
        "raw/readme.txt",
        "truth/world.json",
        "truth/mentions.json",
        "notes.txt",
    ):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(name.encode())
    assert sorted(generated_files(tmp_path)) == [
        "raw/logs/a.log",
        "raw/maintenance/sub/b.csv",
        "raw/manuals/manual.pdf",
        "truth/mentions.json",
    ]


def test_the_committed_raw_folders_hold_exactly_the_files_the_generator_writes(
    committed_data, fresh_data
):
    problems = [
        line
        for line in drift(committed_data, fresh_data)
        if line.endswith(("(missing)", "(unexpected)"))
    ]
    assert not problems, out_of_date(problems)


def test_the_committed_text_files_and_mentions_json_are_the_default_seed_render(
    committed_data, fresh_data
):
    def is_text(key: str) -> bool:
        return key.endswith(TEXT_SUFFIXES) or key == MENTIONS_KEY

    problems = drift(select(committed_data, is_text), select(fresh_data, is_text))
    assert not problems, out_of_date(problems)


def test_the_committed_workbook_is_the_default_seed_render(committed_data, fresh_data):
    def is_workbook(key: str) -> bool:
        return key == WORKBOOK_KEY

    problems = drift(
        select(committed_data, is_workbook), select(fresh_data, is_workbook)
    )
    assert not problems, out_of_date(problems)


def test_the_committed_manuals_are_the_default_seed_render(committed_data, fresh_data):
    """The PDFs are compared byte for byte. The PDF library gives the same bytes on
    every run in this mode."""

    def is_manual(key: str) -> bool:
        return key.startswith(MANUALS_PREFIX)

    committed, fresh = select(committed_data, is_manual), select(fresh_data, is_manual)
    assert len(fresh) == FILES_PER_FOLDER["manuals"]
    problems = drift(committed, fresh)
    assert not problems, out_of_date(problems)
