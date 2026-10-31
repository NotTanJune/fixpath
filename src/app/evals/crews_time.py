"""A small, paid test run that times the two crews, started with --time-crews.

It measures what the results file cannot: how long each step of the ingestion crew takes
and what the model steps of the troubleshooting crew cost. It makes a handful of real
model calls, so it runs under a hard spending cap. Its calls are booked in the real
spend ledger. All work happens on scratch copies, so the saved caches, the graph and the
pipeline report are never changed. The raw measurements go to a timing file that the
crews block is later built from, at no cost.

run_timing() runs the whole sample. confirm_cap() refuses to start unless the cap is
really enforced. The phase functions time the free steps, the paid text and manual
reading, the full pipeline, and the question answering.
"""

import dataclasses
import json
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.globals import get_llm_cache, set_llm_cache

from app import embed, ledger, llm, llm_cache
from app.api import suggestions
from app.ask import crew
from app.config import KG_PATH, RAW_DIR, ROOT, Settings
from app.decide import Decider, backend_name, get_decider
from app.decide import base as decide_base
from app.evals.layer5 import percentile
from app.ingest import graph, parsers, router
from app.ingest.extract_manual import extract_manual
from app.ingest.extract_text import extract_note
from app.ingest.load import raw_files
from app.ingest.report import ledger_mark, ledger_rows_since

CAP_USD = 0.10
TIMING_PATH = ROOT / "data" / "evals" / "results" / "crews_timing.json"
MANUAL_NAME = "BH-250 service manual.pdf"
ANSWER_QUESTIONS = suggestions.QUESTIONS[:4]
PARSED_TYPES = ("log", "inventory", "maintenance_note")
MODEL_NODES = ("triage", "graph_agent", "manual_agent", "synthesize", "verify")


class CapNotEnforced(RuntimeError):
    """Raised when the spending cap is not in force, so no paid call may start."""


# --- the cap and the settings -------------------------------------------------------------


def cap_for(spent: float) -> float:
    """The budget of the timed run: the ledger total now plus the cap of the run."""
    return spent + CAP_USD


def confirm_cap(settings: Settings, spent: float) -> None:
    """Stop before any paid call unless the budget equals the ledger total plus the cap,
    and the budget check really stops at that limit."""
    if abs(settings.budget_usd - cap_for(spent)) > 1e-9:
        raise CapNotEnforced(
            f"the budget is ${settings.budget_usd:.6f}, not the ledger total "
            f"${spent:.6f} plus ${CAP_USD:.2f}"
        )
    try:
        ledger.check_budget(dataclasses.replace(settings, budget_usd=spent))
    except ledger.BudgetExceeded:
        pass
    else:
        raise CapNotEnforced(
            "the budget check did not stop at a spend equal to the budget"
        )
    ledger.check_budget(settings)  # stops here if the cap is already spent


def require_backends(settings: Settings) -> None:
    """Check that both API keys are set and that JEV is the decider, so no other
    service stands in for a step."""
    if not settings.has_openai:
        raise RuntimeError("OPENAI_API_KEY is not set; the Sol and Terra steps need it")
    if not settings.has_typesafe:
        raise RuntimeError("TYPESAFE_API_KEY is not set; the JEV steps need it")
    chosen = backend_name(settings)
    if chosen != "jev":
        raise RuntimeError(f"the decider is {chosen}, not jev; set DECIDER=jev or auto")


def without_tracing(settings: Settings) -> Settings:
    """The settings without the Langfuse keys, so trace uploads do not affect timing."""
    return dataclasses.replace(
        settings, langfuse_public_key=None, langfuse_secret_key=None
    )


# --- scratch state --------------------------------------------------------------------------


def copy_database(source: Path, target: Path) -> None:
    """A consistent copy of a SQLite file, read through a read-only connection."""
    uri = f"{Path(source).resolve().as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True)) as old,
        closing(sqlite3.connect(target)) as new,
    ):
        old.backup(new)


@contextmanager
def scratch_caches(model_cache: Path, decision_cache: Path) -> Iterator[None]:
    """Point the default cache files at scratch files inside the block, so nothing real
    is written. The original paths are restored on exit."""
    saved = (llm_cache.CACHE_PATH, decide_base.CACHE_PATH, get_llm_cache())
    llm_cache.CACHE_PATH, decide_base.CACHE_PATH = (
        Path(model_cache),
        Path(decision_cache),
    )
    try:
        yield
    finally:
        llm_cache.CACHE_PATH, decide_base.CACHE_PATH = saved[:2]
        set_llm_cache(saved[2])


@contextmanager
def scratch_ledger(path: Path) -> Iterator[None]:
    """Book the calls in a scratch ledger, for the steps that cost nothing."""
    saved = ledger.LEDGER_PATH
    ledger.LEDGER_PATH = Path(path)
    try:
        yield
    finally:
        ledger.LEDGER_PATH = saved


@contextmanager
def patched(
    owner: Any, name: str, wrapper: Callable[[Callable], Callable]
) -> Iterator[None]:
    """Swap owner.name for a wrapped version inside the block, then put it back."""
    original = getattr(owner, name)
    setattr(owner, name, wrapper(original))
    try:
        yield
    finally:
        setattr(owner, name, original)


# --- timing ---------------------------------------------------------------------------------


class TimingDecider:
    """Passes every question to a decider and records its purpose, time, whether it was
    a live call, and its cost."""

    def __init__(self, inner: Decider):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.asks: list[dict[str, Any]] = []

    def ask(self, state, questions, purpose):
        mark = ledger_mark()
        started = time.perf_counter()
        answers = self.inner.ask(state, questions, purpose)
        seconds = time.perf_counter() - started
        rows = [r for r in ledger_rows_since(mark) if r["purpose"] == purpose]
        self.asks.append(
            {
                "purpose": purpose,
                "seconds": seconds,
                "live": any(not r["cache_hit"] for r in rows),
                "usd": sum(r["usd"] for r in rows),
                "row_ids": [r["id"] for r in rows],
            }
        )
        return answers

    def questions(self, prefix: str) -> dict[str, Any]:
        """The live questions whose purpose starts with prefix, and how many were
        answered from the cache instead."""
        mine = [a for a in self.asks if a["purpose"].startswith(prefix)]
        live = [a for a in mine if a["live"]]
        return {
            "questions": [
                {
                    "purpose": a["purpose"],
                    "seconds": a["seconds"],
                    "usd": a["usd"],
                    "row_ids": a["row_ids"],
                }
                for a in live
            ],
            "cached": len(mine) - len(live),
        }


def pick_text_files(paths: Sequence[Path], kind: Callable[[Path], str]) -> list[Path]:
    """The files to read live: the first, middle and last handover, and the first note."""
    handovers = sorted(p for p in paths if kind(p) == "handover")
    notes = sorted(
        p for p in paths if kind(p) == "maintenance_note" and p.suffix == ".txt"
    )
    chosen = (
        [handovers[0], handovers[len(handovers) // 2], handovers[-1]]
        if handovers
        else []
    )
    return chosen + notes[:1]


def write_timing(data: dict[str, Any], path: Path = TIMING_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()
    return result, time.perf_counter() - started


# --- the phases -----------------------------------------------------------------------------


def _free_phase(raw: Path, work: Path, model_copy: Path) -> dict[str, Any]:
    """Time the router and the code parsers on every file. No keys are used. Returns the
    timings and the alias table the paid phase needs."""
    keyless = Settings()
    files = raw_files(raw)
    with (
        scratch_caches(model_copy, work / "decisions_free.sqlite"),
        scratch_ledger(work / "free.sqlite"),
    ):
        llm_cache.enable_cache()
        routed = []
        for path in files:
            doc_type, seconds = _timed(lambda p=path: router.classify(p))
            routed.append(
                {"doc_path": _doc(path, raw), "doc_type": doc_type, "seconds": seconds}
            )
        workbook = next(
            (p for p in files if p.suffix.lower() in (".xlsx", ".xlsm")), None
        )
        aliases = parsers.build_alias_table(workbook, keyless)
        parsed = []
        for path in router.ordered(files):
            doc_type = router.classify(path)
            extractor = router.extractor_for(path)
            if doc_type in PARSED_TYPES and extractor not in (
                extract_note,
                extract_manual,
            ):
                _, seconds = _timed(
                    lambda e=extractor, p=path: e(p, aliases, settings=keyless)
                )
                parsed.append(
                    {
                        "doc_path": _doc(path, raw),
                        "doc_type": doc_type,
                        "seconds": seconds,
                    }
                )
    return {
        "router": {"files": routed},
        "parsers": {"files": parsed},
        "aliases": aliases,
    }


def _doc(path: Path, raw: Path) -> str:
    return path.relative_to(raw).as_posix()


def _sol_phase(
    raw: Path, work: Path, aliases: Any, settings: Settings
) -> dict[str, Any]:
    """Read three handovers, one note and the manual live, each on an empty cache. The
    cost is booked in the real ledger."""
    picked = pick_text_files(raw_files(raw), router.classify)
    manual = next(p for p in raw_files(raw) if p.name == MANUAL_NAME)
    calls: list[dict[str, Any]] = []

    def one(path: Path, extractor: Callable, index: int) -> dict[str, Any]:
        model_seconds: list[float] = []

        def timed_structured(original):
            def call(*args, **kwargs):
                result, seconds = _timed(lambda: original(*args, **kwargs))
                model_seconds.append(seconds)
                return result

            return call

        mark = ledger_mark()
        cache = work / f"sol_{index}.sqlite"
        with scratch_caches(cache, work / f"sol_decisions_{index}.sqlite"):
            llm_cache.enable_cache()
            with patched(llm, "structured", timed_structured):
                _, seconds = _timed(lambda: extractor(path, aliases, settings=settings))
        rows = [r for r in ledger_rows_since(mark) if not r["cache_hit"]]
        return {
            "doc_path": _doc(path, raw),
            "doc_type": router.classify(path),
            "seconds": seconds,
            "model_s": sum(model_seconds),
            "usd": sum(r["usd"] for r in rows),
            "row_ids": [r["id"] for r in rows],
        }

    for index, path in enumerate(picked):
        calls.append(one(path, extract_note, index))
    manual_call = one(manual, extract_manual, len(picked))
    return {"text_reader": {"files": calls}, "manual_reader": {"files": [manual_call]}}


def _pipeline_phase(
    raw: Path, work: Path, model_copy: Path, settings: Settings
) -> dict[str, Any]:
    """Time the resolve, link, validate and load steps over every file. Extractions come
    from a copy of the saved cache, and without keys a miss raises an error instead of
    paying. The JEV questions run live on an empty cache."""
    keyless = Settings()
    embedder = embed.Embedder()
    embedder.embed_passages(["warm up the embedding model"])
    marks: dict[str, float] = {}

    def timer(name: str):
        def wrap(original):
            def call(*args, **kwargs):
                result, seconds = _timed(lambda: original(*args, **kwargs))
                marks[name] = marks.get(name, 0.0) + seconds
                return result

            return call

        return wrap

    with scratch_caches(model_copy, work / "decisions_pipeline.sqlite"):
        llm_cache.enable_cache()
        decider = TimingDecider(get_decider(settings))
        run = graph.Run(
            mode="full",
            raw_dir=raw,
            kg_path=work / "kg_rebuilt.sqlite",
            report_path=work / "pipeline_rebuilt.json",
            decider=decider,
            embedder=embedder,
            mark=ledger_mark(),
            started=time.monotonic(),
            settings=keyless,
        )
        compiled = graph.build_graph(run)
        with (
            patched(graph, "resolve", timer("resolver")),
            patched(graph, "link", timer("linker")),
            patched(graph, "validate", timer("validator")),
            patched(graph, "load_full", timer("loader")),
        ):
            _, total = _timed(
                lambda: compiled.invoke(
                    {"mode": "full", "paths": raw_files(raw)}, graph.run_config(keyless)
                )
            )
    return {
        "resolver": {"seconds": marks["resolver"], **decider.questions("merge")},
        "linker": {"seconds": marks["linker"], **decider.questions("link")},
        "validator": {"seconds": marks["validator"]},
        "loader": {"seconds": marks["loader"]},
        "pipeline_seconds": total,
    }


def _answer_phase(
    work: Path, kg_copy: Path, settings: Settings
) -> list[dict[str, Any]]:
    """Run the suggested questions through the crew on empty caches."""
    embed.embed_query("warm up the embedding model")
    answers = []
    for index, question in enumerate(ANSWER_QUESTIONS):
        mark = ledger_mark()
        with scratch_caches(
            work / f"ask_{index}.sqlite", work / f"ask_decisions_{index}.sqlite"
        ):
            card = crew.ask(question, kg_path=kg_copy, settings=settings)
        trace = card.trace
        rows = ledger_rows_since(mark)
        by_purpose: dict[str, dict[str, float]] = {}
        for row in rows:
            entry = by_purpose.setdefault(row["purpose"], {"calls": 0, "usd": 0.0})
            entry["calls"] += 1
            entry["usd"] += row["usd"]
        answers.append(
            {
                "question": question,
                "status": card.triage.status if card.triage else None,
                "seconds": (
                    max(t.ended_at for t in trace) - min(t.started_at for t in trace)
                ).total_seconds(),
                "nodes": {
                    t.name: {
                        "seconds": t.seconds,
                        "usd": t.cost_usd,
                        "backend": t.backend,
                        "cache_hit": t.cache_hit,
                    }
                    for t in trace
                },
                "ledger": dict(sorted(by_purpose.items())),
                "row_ids": [r["id"] for r in rows if not r["cache_hit"]],
            }
        )
    return answers


# --- the run --------------------------------------------------------------------------------


def _rows_since(mark: int) -> list[dict[str, Any]]:
    return ledger_rows_since(mark)


def run_timing(
    settings: Settings,
    *,
    raw_dir: Path = RAW_DIR,
    kg_path: Path = KG_PATH,
    out_path: Path = TIMING_PATH,
    spent: float | None = None,
) -> dict[str, Any]:
    """Run the timed sample and write the measurements to out_path. The budget in
    settings must be the ledger total plus the cap. Partial results are saved to a
    temporary file after each phase, so a stop at the cap keeps what was measured."""
    settings = without_tracing(settings)
    spent = ledger.spent_usd() if spent is None else spent
    require_backends(settings)
    confirm_cap(settings, spent)
    mark = ledger_mark()
    partial = Path(tempfile.gettempdir()) / "crews_timing_partial.json"
    result: dict[str, Any] = {
        "measured_at": datetime.now(UTC).date().isoformat(),
        "spend": {
            "ledger_before": spent,
            "cap_usd": CAP_USD,
            "budget_usd": settings.budget_usd,
        },
    }
    with tempfile.TemporaryDirectory(prefix="crews-timing-") as folder:
        work = Path(folder)
        raw = work / "raw"
        shutil.copytree(raw_dir, raw)
        model_copy, kg_copy = work / "llm_cache.sqlite", work / "kg.sqlite"
        copy_database(llm_cache.CACHE_PATH, model_copy)
        copy_database(kg_path, kg_copy)
        try:
            free = _free_phase(raw, work, model_copy)
            aliases = free.pop("aliases")
            ingestion = dict(free)
            result["ingestion"] = ingestion
            ingestion |= _sol_phase(raw, work, aliases, settings)
            write_timing(result, partial)
            ingestion |= _pipeline_phase(raw, work, model_copy, settings)
            write_timing(result, partial)
            result["answers"] = _answer_phase(work, kg_copy, settings)
        finally:
            rows = _rows_since(mark)
            result["spend"] |= {
                "ledger_after": ledger.spent_usd(),
                "rows": [
                    {k: r[k] for k in ("id", "purpose", "model", "usd", "cache_hit")}
                    for r in rows
                    if not r["cache_hit"]
                ],
                "cache_hit_rows": sum(1 for r in rows if r["cache_hit"]),
            }
            write_timing(result, partial)
    result["spend"]["ledger_added_usd"] = result["spend"]["ledger_after"] - spent
    _attach_seconds(result)
    write_timing(result, out_path)
    return result


def _attach_seconds(result: dict[str, Any]) -> None:
    """Add the wall time to each ledger row where one call is one row. Agent calls have
    no time of their own, only their node does."""
    seconds: dict[int, float] = {}
    ingestion = result["ingestion"]
    for step in ("resolver", "linker"):
        for q in ingestion[step]["questions"]:
            for row_id in q["row_ids"]:
                seconds[row_id] = q["seconds"]
    for step in ("text_reader", "manual_reader"):
        for f in ingestion[step]["files"]:
            if len(f["row_ids"]) == 1:
                seconds[f["row_ids"][0]] = f["model_s"]
    for row in result["spend"]["rows"]:
        row["seconds"] = seconds.get(row["id"])


def summary(result: dict[str, Any]) -> list[str]:
    """The lines to print after a finished run."""
    spend = result["spend"]
    answers = result["answers"]
    median = percentile([a["seconds"] for a in answers], 50)
    return [
        f"timed run of {result['measured_at']}",
        (
            f"ledger total before ${spend['ledger_before']:.6f}, "
            f"after ${spend['ledger_after']:.6f}, "
            f"added ${spend['ledger_added_usd']:.6f} (cap ${CAP_USD:.2f})"
        ),
        f"paid calls: {len(spend['rows'])}",
        f"answers timed: {len(answers)}, median {median:.1f} s",
    ]
