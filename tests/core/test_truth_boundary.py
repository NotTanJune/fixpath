"""Checks that the product code never refers to the hidden truth.

The evals score what ingestion recovers from the messy files. So the code that
ingests, asks, serves, decides, stores and calls the model may not read or mention
data/truth. Paths that do not exist yet are skipped.
"""

import ast
from pathlib import Path

from app.config import ROOT

APP = ROOT / "src" / "app"
GUARDED = (
    "ingest",
    "ask",
    "api",
    "decide",
    "store.py",
    "llm.py",
    "embed.py",
    "llm_cache.py",
    "ledger.py",
    "ontology.py",
    "factmap.py",
    "upkeep.py",
)
# The modules that run in the product. They never import the evals or the generator,
# which are the only code that may read the truth.
RUNTIME = (
    "ingest",
    "ask",
    "api",
    "decide",
    "store.py",
    "llm.py",
    "llm_cache.py",
    "embed.py",
    "ledger.py",
    "factmap.py",
    "upkeep.py",
)
OFF_LIMITS = ("app.evals", "app.generator")
FORBIDDEN = ("TRUTH_DIR", "data/truth", "world.json", "mentions.json")


def offenders(paths: list[Path]) -> list[tuple[Path, str]]:
    """Each file under the given paths that holds a forbidden name, with the name.
    A path that does not exist yet is skipped."""
    found = []
    for root in paths:
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for file in files:
            if not file.is_file() or "__pycache__" in file.parts:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            found += [(file, word) for word in FORBIDDEN if word in text]
    return found


def test_the_ingestion_side_never_names_the_hidden_truth():
    assert offenders([APP / name for name in GUARDED]) == []


def test_the_scan_flags_every_forbidden_name_in_a_file_or_a_folder(tmp_path):
    (tmp_path / "ingest" / "deep").mkdir(parents=True)
    (tmp_path / "decide").mkdir()
    (tmp_path / "ingest" / "a.py").write_text("from app.config import TRUTH_DIR\n")
    (tmp_path / "ingest" / "deep" / "b.py").write_text('open("data/truth/x.json")\n')
    (tmp_path / "decide" / "notes.txt").write_text("see mentions.json\n")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "app.py").write_text('PATH = "world.json"\n')
    (tmp_path / "store.py").write_text("print('clean')\n")
    found = offenders([tmp_path / name for name in GUARDED])
    assert sorted((path.name, word) for path, word in found) == [
        ("a.py", "TRUTH_DIR"),
        ("app.py", "world.json"),
        ("b.py", "data/truth"),
        ("notes.txt", "mentions.json"),
    ]


def test_the_scan_skips_paths_that_do_not_exist_and_compiled_files(tmp_path):
    (tmp_path / "ingest" / "__pycache__").mkdir(parents=True)
    (tmp_path / "ingest" / "__pycache__" / "a.cpython-313.pyc").write_text("TRUTH_DIR")
    (tmp_path / "ingest" / "a.py").write_text("print('clean')\n")
    assert offenders([tmp_path / name for name in GUARDED]) == []


def imported_modules(source: str) -> set[str]:
    """Every module a source file imports, as a dotted name: `from app import evals`
    counts as app.evals."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def importers(paths: list[Path]) -> list[tuple[Path, str]]:
    """Each file under the paths that imports app.evals or app.generator (or a module
    inside them), with the module."""
    found = []
    for root in paths:
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for file in files:
            for module in sorted(imported_modules(file.read_text(encoding="utf-8"))):
                if any(
                    module == name or module.startswith(name + ".")
                    for name in OFF_LIMITS
                ):
                    found.append((file, module))
    return found


def test_no_runtime_module_imports_the_evals_or_the_generator():
    assert importers([APP / name for name in RUNTIME]) == []


def test_the_import_scan_catches_every_way_to_import_them(tmp_path):
    (tmp_path / "ask").mkdir()
    (tmp_path / "ask" / "a.py").write_text("from app.evals import match\n")
    (tmp_path / "ask" / "b.py").write_text("import app.generator.world\n")
    (tmp_path / "ask" / "c.py").write_text("from app import evals\n")
    (tmp_path / "ask" / "d.py").write_text(
        "def f():\n    from app.generator import world\n"
    )
    (tmp_path / "ask" / "e.py").write_text(
        "from app import ledger\nimport app.evalsx\n"
    )
    found = importers([tmp_path / "ask"])
    assert sorted((path.name, module) for path, module in found) == [
        ("a.py", "app.evals"),
        ("a.py", "app.evals.match"),
        ("b.py", "app.generator.world"),
        ("c.py", "app.evals"),
        ("d.py", "app.generator"),
        ("d.py", "app.generator.world"),
    ]


def test_every_module_that_runs_in_the_product_is_guarded():
    # a new top-level module is either guarded or a known exception
    exempt = {"__init__.py", "brand.py", "config.py"}
    folders = {"evals", "generator", "__pycache__"}
    top = {
        p.name
        for p in APP.iterdir()
        if p.name not in exempt and p.name not in folders and not p.name.startswith(".")
    }
    assert top <= set(GUARDED), sorted(top - set(GUARDED))
