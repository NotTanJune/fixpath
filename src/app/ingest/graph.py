"""The ingestion pipeline: raw files in, knowledge graph out.

The pipeline runs in stages. It reads each file with the right extractor, matches the
facts to known things, links causes, checks the result and writes the database and a
report. Files are read in parallel.

build_graph assembles the stages. run_full rebuilds the graph from every raw file.
run_file adds one file to the existing graph. stream_ingest does the same but reports
progress as it goes, for the interface.
"""

import logging
import operator
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app import factmap, llm_cache
from app.config import KG_PATH, RAW_DIR, Settings, load_settings
from app.decide import Decider, get_decider
from app.embed import Embedder
from app.ingest import events, parsers, router
from app.ingest.link import link
from app.ingest.load import (
    PassageEmbedder,
    load_file,
    load_full,
    raw_files,
    registry_from_store,
)
from app.ingest.normalize import AliasTable
from app.ingest.report import (
    build_report,
    default_report_path,
    ledger_mark,
    ledger_rows_since,
    write_report,
)
from app.ingest.resolve import Registry, resolve
from app.ingest.types import Extraction, Resolution
from app.ingest.validate import Validated, validate
from app.llm import tracing_callbacks

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 8
RUN_NAME = "ingest"
WORKBOOK_SUFFIXES = (".xlsx", ".xlsm")
NO_DATABASE = "there is no graph database at {path}; run a full ingest first"

Mode = Literal["full", "file"]


class IngestState(TypedDict, total=False):
    mode: Mode
    paths: list[Path]
    aliases: AliasTable
    # Files finish in any order, so each result records where its file belongs.
    positions: Annotated[dict[str, int], operator.or_]
    extractions: Annotated[list[Extraction], operator.add]
    resolution: Resolution
    validated: Validated
    report: dict[str, Any]
    placed: factmap.Placement  # where the new points sit on the map, for a file run


@dataclass
class Run:
    """The settings that stay the same for one run."""

    mode: Mode
    raw_dir: Path
    kg_path: Path
    report_path: Path
    decider: Decider
    embedder: PassageEmbedder
    mark: int
    started: float
    settings: Settings | None = None  # used for every model call
    map_path: Path | None = None  # the fact map, used to place a file's new points


def _workbook(raw_dir: Path) -> Path | None:
    """The stores workbook in the raw folder, or None."""
    return next(
        (p for p in raw_files(raw_dir) if p.suffix.lower() in WORKBOOK_SUFFIXES), None
    )


def _in_order(state: IngestState) -> list[Extraction]:
    """The extractions in the order their files were meant to be processed."""
    positions = state.get("positions", {})
    return sorted(
        state.get("extractions", []),
        key=lambda e: (positions.get(e.doc_path, len(positions)), e.doc_path),
    )


def build_graph(run: Run):
    """The ready-to-run pipeline for one run."""

    def prepare(state: IngestState) -> IngestState:
        workbook = _workbook(run.raw_dir)
        aliases = (
            parsers.build_alias_table(workbook, run.settings)
            if workbook
            else AliasTable([])
        )
        return {"paths": router.ordered(state["paths"]), "aliases": aliases}

    def fan_out(state: IngestState) -> list[Send] | str:
        if not state["paths"]:
            return "resolve"
        return [
            Send("extract", {"path": path, "index": index, "aliases": state["aliases"]})
            for index, path in enumerate(state["paths"])
        ]

    def extract(item: dict[str, Any]) -> IngestState:
        path: Path = item["path"]
        writer = get_stream_writer()
        extractor = router.extractor_for(path)
        if extractor is None:
            writer(
                {
                    "event": "skipped",
                    "doc_path": path.relative_to(run.raw_dir).as_posix()
                    if path.is_relative_to(run.raw_dir)
                    else path.name,
                    "reason": "no extractor for this kind of file",
                }
            )
            return {}
        extraction = extractor(path, item["aliases"], settings=run.settings)
        writer(
            {
                "event": "extracted",
                "doc_path": extraction.doc_path,
                "doc_type": extraction.doc_type,
                "extractor": extraction.extractor,
                "nodes": len(extraction.nodes),
                "edges": len(extraction.edges),
                "chunks": len(extraction.chunks),
                "notes": len(extraction.notes),
            }
        )
        if run.mode == "file":
            writer({"event": "chunked", **events.chunked(extraction)})
            writer({"event": "facts", **events.facts(extraction)})
        return {
            "extractions": [extraction],
            "positions": {extraction.doc_path: item["index"]},
        }

    def _place(run: Run) -> factmap.Placement | None:
        """Where the new points sit on the map, or None. The data is already saved, so
        a failure here is logged and the file still counts as ingested."""
        if run.map_path is None:
            return None
        try:
            return factmap.place_new(run.kg_path, run.map_path, run.embedder)
        except Exception:
            logger.exception("the new points could not be placed on the map")
            return None

    def resolve_node(state: IngestState) -> IngestState:
        registry = (
            Registry.empty() if run.mode == "full" else registry_from_store(run.kg_path)
        )
        extractions = _in_order(state)
        resolution = resolve(extractions, state["aliases"], run.decider, registry)
        if run.mode == "file":
            known = {node.id for node in registry.nodes}
            get_stream_writer()(
                {"event": "matched", **events.matched(extractions, known, resolution)}
            )
        return {"resolution": resolution}

    def link_node(state: IngestState) -> IngestState:
        return {"resolution": link(state["resolution"], run.decider)}

    def validate_node(state: IngestState) -> IngestState:
        return {"validated": validate(state["resolution"])}

    def load_node(state: IngestState) -> IngestState:
        extractions, resolution = _in_order(state), state["resolution"]
        validated = state["validated"]
        if run.mode == "full":
            loaded = load_full(
                validated,
                extractions,
                run.raw_dir,
                run.kg_path,
                review=resolution.review,
                embedder=run.embedder,
            )
        else:
            loaded = load_file(
                validated,
                extractions,
                state["paths"],
                run.kg_path,
                raw_dir=run.raw_dir,
                review=resolution.review,
                embedder=run.embedder,
            )
        report = build_report(
            extractions, resolution, validated, ledger_rows_since(run.mark)
        )
        report["run"] = {
            "mode": run.mode,
            "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - run.started, 2),
            "loaded": loaded,
        }
        write_report(report, run.report_path)
        update: IngestState = {"report": report}
        placement = _place(run) if run.mode == "file" else None
        if placement is not None:
            get_stream_writer()({"event": "placed", **placement.event()})
            update["placed"] = placement
        return update

    builder = StateGraph(IngestState)
    builder.add_node("prepare", prepare)
    builder.add_node("extract", extract)
    builder.add_node("resolve", resolve_node)
    builder.add_node("link", link_node)
    builder.add_node("validate", validate_node)
    builder.add_node("load", load_node)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges("prepare", fan_out, ["extract", "resolve"])
    builder.add_edge("extract", "resolve")
    builder.add_edge("resolve", "link")
    builder.add_edge("link", "validate")
    builder.add_edge("validate", "load")
    builder.add_edge("load", END)
    return builder.compile()


def run_config(settings: Settings) -> dict[str, Any]:
    """How a run is set up: how many files run at once, its name and tracing."""
    config: dict[str, Any] = {"max_concurrency": MAX_CONCURRENCY, "run_name": RUN_NAME}
    handlers = tracing_callbacks(settings)
    if handlers:
        config["callbacks"] = handlers
    return config


def _start(
    mode: Mode,
    paths: list[Path],
    raw_dir: Path,
    kg_path: Path,
    settings: Settings | None,
    decider: Decider | None,
    embedder: PassageEmbedder | None,
    report_path: Path | None,
    map_path: Path | None = None,
):
    """The pipeline, its input and its setup for one run. Turns the model cache on."""
    settings = settings or load_settings()
    llm_cache.enable_cache()
    run = Run(
        mode=mode,
        raw_dir=Path(raw_dir),
        kg_path=Path(kg_path),
        report_path=Path(report_path)
        if report_path
        else default_report_path(kg_path, mode),
        decider=decider or get_decider(settings),
        embedder=embedder or Embedder(),
        mark=ledger_mark(),
        started=time.monotonic(),
        settings=settings,
        map_path=Path(map_path) if map_path else None,
    )
    return build_graph(run), {"mode": mode, "paths": paths}, run_config(settings)


def _full_arguments(raw_dir: Path) -> list[Path]:
    return raw_files(Path(raw_dir))


def _file_arguments(path: Path | str, kg_path: Path) -> list[Path]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not a file")
    if not Path(kg_path).is_file():
        raise FileNotFoundError(NO_DATABASE.format(path=kg_path))
    return [path]


def run_full(
    raw_dir: Path = RAW_DIR,
    kg_path: Path = KG_PATH,
    settings: Settings | None = None,
    *,
    decider: Decider | None = None,
    embedder: PassageEmbedder | None = None,
    report_path: Path | None = None,
) -> IngestState:
    """Rebuild the database from every file in the raw folder and write the report."""
    graph, inputs, config = _start(
        "full",
        _full_arguments(raw_dir),
        raw_dir,
        kg_path,
        settings,
        decider,
        embedder,
        report_path,
    )
    return graph.invoke(inputs, config)


def run_file(
    path: Path | str,
    kg_path: Path = KG_PATH,
    settings: Settings | None = None,
    *,
    raw_dir: Path = RAW_DIR,
    decider: Decider | None = None,
    embedder: PassageEmbedder | None = None,
    report_path: Path | None = None,
) -> IngestState:
    """Add one file to the existing database, which keeps what it holds. Fails if the
    database does not exist yet."""
    graph, inputs, config = _start(
        "file",
        _file_arguments(path, kg_path),
        raw_dir,
        kg_path,
        settings,
        decider,
        embedder,
        report_path,
    )
    return graph.invoke(inputs, config)


def stream_ingest(
    path: Path | str | None = None,
    raw_dir: Path = RAW_DIR,
    kg_path: Path = KG_PATH,
    settings: Settings | None = None,
    *,
    decider: Decider | None = None,
    embedder: PassageEmbedder | None = None,
    report_path: Path | None = None,
    map_path: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Run an ingestion and yield progress messages as stages finish. With a path it
    adds that one file. Without one it rebuilds from the whole raw folder."""
    if path is None:
        mode, paths = "full", _full_arguments(raw_dir)
    else:
        mode, paths = "file", _file_arguments(path, kg_path)
    graph, inputs, config = _start(
        mode,
        paths,
        raw_dir,
        kg_path,
        settings,
        decider,
        embedder,
        report_path,
        map_path,
    )
    yield from graph.stream(
        inputs, config, stream_mode=["updates", "custom"], version="v2"
    )
