"""Tests for the product name and the script that applies it to the project files.

The name never appears in this file. The tests read it from the brand settings."""

import dataclasses
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.brand import Brand, load_brand
from app.config import ROOT

SCRIPT = ROOT / "scripts" / "brand.py"
HEADER = (
    "<!-- Generated from {template} by scripts/brand.py. "
    "Edit the template, not this file. -->"
)
TEMPLATE_PAIRS = (("docs/deck/deck.template.md", "docs/deck/deck.md"),)
FIVE_MIB = 5 * 1024 * 1024


def run_script(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def spaced(name: str) -> str:
    """Put a space between the camel-case parts of the name."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def load_script():
    """Import scripts/brand.py as a module so tests can call main() in-process."""
    spec = importlib.util.spec_from_file_location("brand_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- app.brand -------------------------------------------------------------


def test_load_brand_returns_all_four_fields():
    data = json.loads((ROOT / "config/brand.json").read_text(encoding="utf-8"))
    brand = load_brand()
    assert isinstance(brand, Brand)
    assert [f.name for f in dataclasses.fields(Brand)] == [
        "name",
        "slug",
        "tagline",
        "description",
    ]
    assert dataclasses.asdict(brand) == data
    assert all(isinstance(value, str) and value for value in data.values())


def test_load_brand_is_cached_and_frozen():
    assert load_brand() is load_brand()
    with pytest.raises(dataclasses.FrozenInstanceError):
        load_brand().name = "other"


# --- scripts/brand.py render ------------------------------------------------


def test_render_writes_the_header_and_substituted_text(tmp_path):
    write(
        tmp_path,
        "docs/deck/deck.template.md",
        "# {{name}}\n\n{{tagline}}\n\n{{description}}\n\nSlug: {{slug}}\n",
    )
    result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    brand = load_brand()
    output = (tmp_path / "docs" / "deck" / "deck.md").read_text(encoding="utf-8")
    assert output.split("\n")[0] == HEADER.format(template="docs/deck/deck.template.md")
    for expected in (
        f"# {brand.name}\n",
        f"\n{brand.tagline}\n",
        f"\n{brand.description}\n",
        f"Slug: {brand.slug}\n",
    ):
        assert expected in output
    assert "{{" not in output
    assert output.endswith("\n")


def test_render_handles_the_deck_pair(tmp_path):
    write(tmp_path, "docs/deck/deck.template.md", "# {{name}}\n")
    result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stderr

    output = (tmp_path / "docs" / "deck" / "deck.md").read_text(encoding="utf-8")
    assert output.split("\n")[0] == HEADER.format(template="docs/deck/deck.template.md")
    assert f"# {load_brand().name}\n" in output
    assert not (tmp_path / "README.md").exists()


def test_render_skips_pairs_without_a_template(tmp_path):
    result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []


def test_render_replaces_every_occurrence_and_is_repeatable(tmp_path):
    write(tmp_path, "docs/deck/deck.template.md", "{{name}} {{name}} {{slug}}\n")
    first = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert first.returncode == 0, first.stderr
    output = (tmp_path / "docs" / "deck" / "deck.md").read_bytes()

    brand = load_brand()
    assert f"{brand.name} {brand.name} {brand.slug}\n".encode() in output

    second = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert second.returncode == 0, second.stderr
    assert (tmp_path / "docs" / "deck" / "deck.md").read_bytes() == output


def test_render_exits_1_on_unknown_placeholder_and_writes_no_output(tmp_path):
    write(
        tmp_path,
        "docs/deck/deck.template.md",
        "# {{name}}\n\nOwner: {{owner}}\n\nSpaced: {{ name }}\n\nAgain: {{owner}}\n",
    )
    result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert not (tmp_path / "docs" / "deck" / "deck.md").exists()

    report = result.stdout + result.stderr
    assert "{{owner}}" in report
    assert "{{ name }}" in report
    assert report.count("{{owner}}") == 2


def test_render_leaves_an_existing_output_untouched_on_error(tmp_path):
    write(tmp_path, "docs/deck/deck.template.md", "{{unknown}}\n")
    existing = write(tmp_path, "docs/deck/deck.md", "old content\n")
    result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert existing.read_text(encoding="utf-8") == "old content\n"


def test_render_never_writes_the_hand_written_readme(tmp_path):
    """README.md is written by hand; render must not create or change it."""
    write(tmp_path, "README.template.md", "# {{name}}\n")
    write(tmp_path, "docs/deck/deck.template.md", "# {{name}}\n")
    result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "README.md").exists()

    readme = write(tmp_path, "README.md", "my own words\n")
    result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert readme.read_text(encoding="utf-8") == "my own words\n"


def test_committed_outputs_match_their_templates(tmp_path):
    """Run `make brand` after editing a template or config/brand.json."""
    for template, output in TEMPLATE_PAIRS:
        source = ROOT / template
        if not source.exists():
            continue
        write(tmp_path, template, source.read_text(encoding="utf-8"))
        result = run_script("render", "--root", str(tmp_path), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        committed = ROOT / output
        assert committed.is_file(), f"{output} is missing, run make brand"
        assert committed.read_bytes() == (tmp_path / output).read_bytes(), (
            f"{output} is stale, run make brand"
        )


# --- scripts/brand.py check -------------------------------------------------


def test_check_exits_1_and_names_the_file(tmp_path):
    name = load_brand().name
    write(tmp_path, "notes.txt", f"first line\nmentions {name} here\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert f"notes.txt:2: {name}" in result.stdout
    assert "brand check passed" not in result.stdout


@pytest.mark.parametrize(
    "variant", ["name", "slug", "upper", "spaced", "lower spaced", "spaced twice"]
)
def test_check_catches_every_spelling(tmp_path, variant):
    brand = load_brand()
    text = {
        "name": brand.name,
        "slug": brand.slug,
        "upper": brand.name.upper(),
        "spaced": spaced(brand.name),
        "lower spaced": spaced(brand.name).lower(),
        "spaced twice": spaced(brand.name).replace(" ", "  "),
    }[variant]
    write(tmp_path, "doc.txt", f"before {text} after\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert f"doc.txt:1: {text}" in result.stdout


@pytest.mark.parametrize("lead", ["pre", "suf", "a", "Z"])
def test_check_ignores_the_spaced_name_right_after_a_letter(tmp_path, lead):
    # A word that ends in the first part of the name, followed by a word that
    # starts with the second part, is ordinary text. "pre" and "suf" build two.
    text = lead + spaced(load_brand().name).lower()
    write(tmp_path, "notes.txt", f"see the {text} here\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "brand check passed"


@pytest.mark.parametrize("gap", [" ", "  "], ids=["one-space", "two-spaces"])
@pytest.mark.parametrize(
    "lead",
    ["", " ", "(", "_", "7"],
    ids=["line-start", "space", "bracket", "underscore", "digit"],
)
def test_check_catches_the_spaced_name_after_anything_but_a_letter(tmp_path, lead, gap):
    text = spaced(load_brand().name).replace(" ", gap)
    write(tmp_path, "doc.txt", f"{lead}{text} after\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert f"doc.txt:1: {text}" in result.stdout


@pytest.mark.parametrize("variant", ["name", "slug"])
def test_check_catches_the_unspaced_name_and_slug_inside_identifiers(tmp_path, variant):
    brand = load_brand()
    text = {"name": brand.name, "slug": brand.slug}[variant]
    write(tmp_path, "module.py", f"def pre{text}_post():\n    pass\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert f"module.py:1: {text}" in result.stdout


def test_check_reports_every_hit_on_its_own_line(tmp_path):
    brand = load_brand()
    write(tmp_path, "a.txt", f"{brand.name} and {brand.slug}\nclean\n{brand.name}\n")
    write(tmp_path, "sub/b.txt", f"{brand.name}\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        f"a.txt:1: {brand.name}",
        f"a.txt:1: {brand.slug}",
        f"a.txt:3: {brand.name}",
        f"sub/b.txt:1: {brand.name}",
    ]


def test_check_passes_on_a_clean_root(tmp_path):
    write(tmp_path, "notes.txt", "Replace the seal, then restart the press.\n")
    write(tmp_path, "src/module.py", "print('hello')\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "brand check passed"


def test_check_passes_on_an_empty_root(tmp_path):
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "brand check passed"


def test_check_passes_on_the_real_repo(tmp_path):
    # The working directory holds a mention. If the default root were the
    # working directory instead of the repo root, the check would fail.
    write(tmp_path, "decoy.txt", f"{load_brand().name}\n")
    result = run_script("check", cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "brand check passed"


def test_check_skips_excluded_directories_files_binaries_and_big_files(tmp_path):
    name = load_brand().name
    mention = f"{name}\n"
    for relative in (
        ".git/config",
        ".venv/lib/module.py",
        "node_modules/pkg/index.js",
        "web/node_modules/pkg/index.js",
        "dist/app.js",
        "web/dist/bundle.js",
        "src/__pycache__/module.txt",
        ".pytest_cache/v/cache.txt",
        ".cache/x.txt",
        "web/.cache/x.txt",
        "config/brand.json",
        "README.md",
        "docs/deck/deck.md",
        "docs/deck/deck.pdf",
        "docs/private/deck.md",
        "docs/private/audience.txt",
    ):
        write(tmp_path, relative, mention)
    write(tmp_path, "blob.bin", b"\x00" + name.encode() + b"\n")
    write(tmp_path, "big.txt", name.encode() + b"\n" + b"x" * FIVE_MIB)

    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "brand check passed"


@pytest.mark.parametrize(
    "relative", [".env", ".env.local", "web/.env", "web/api/.env.production"]
)
def test_check_skips_env_files_at_any_depth(tmp_path, relative):
    write(tmp_path, relative, f"PROJECT={load_brand().name}\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0, result.stdout
    assert result.stdout.strip() == "brand check passed"


@pytest.mark.parametrize("relative", [".env.example", "web/.env.example"])
def test_check_still_scans_env_example_files(tmp_path, relative):
    name = load_brand().name
    write(tmp_path, relative, f"PROJECT={name}\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert f"{relative}:1: {name}" in result.stdout


def test_check_still_scans_look_alike_locations(tmp_path):
    name = load_brand().name
    mention = f"{name}\n"
    scanned = (
        "nested/README.md",
        "docs/notes.md",
        "docs/deck.txt",
        "docs/privately/notes.md",
        "private/notes.md",
        "web/docs/private/notes.md",
        "src/app/module.py",
        "web/src/App.tsx",
        "config/brand.json.bak",
        "Makefile",
        "distribution/notes.txt",
        ".environment.md",
    )
    for relative in scanned:
        write(tmp_path, relative, mention)
    # A NUL byte after the first 8 KB does not make a file binary.
    write(tmp_path, "late-nul.txt", name.encode() + b"\n" + b"x" * 9000 + b"\x00")
    # A file of exactly 5 MiB is not over the limit.
    padding = b"x" * (FIVE_MIB - len(name) - 1)
    write(tmp_path, "limit.txt", name.encode() + b"\n" + padding)
    assert (tmp_path / "limit.txt").stat().st_size == FIVE_MIB

    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    reported = {line.split(":")[0] for line in result.stdout.splitlines()}
    assert reported == {*scanned, "late-nul.txt", "limit.txt"}


def test_check_tolerates_files_that_are_not_utf8(tmp_path):
    name = load_brand().name
    write(tmp_path, "latin1.txt", b"caf\xe9 " + name.encode() + b"\n")
    result = run_script("check", "--root", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 1
    assert f"latin1.txt:1: {name}" in result.stdout


def test_check_does_not_follow_symlinks(tmp_path):
    outside = write(tmp_path, "outside/notes.txt", f"{load_brand().name}\n")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "linked.txt").symlink_to(outside)
    (root / "dangling.txt").symlink_to(tmp_path / "nowhere.txt")
    result = run_script("check", "--root", str(root), cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "brand check passed"


# --- scripts/brand.py usage -------------------------------------------------


def test_a_subcommand_is_required(tmp_path):
    result = run_script(cwd=tmp_path)
    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


@pytest.mark.parametrize("command", ["render", "check"])
def test_root_defaults_to_the_repo_root(monkeypatch, tmp_path, command):
    script = load_script()
    seen = []
    monkeypatch.setattr(script, command, lambda root: seen.append(root) or 0)
    monkeypatch.chdir(tmp_path)
    assert script.main([command]) == 0
    assert seen == [ROOT]


@pytest.mark.parametrize("command", ["render", "check"])
def test_root_must_be_a_directory(tmp_path, command):
    missing = tmp_path / "missing"
    result = run_script(command, "--root", str(missing), cwd=tmp_path)
    assert result.returncode == 2
    assert "not a directory" in result.stderr
