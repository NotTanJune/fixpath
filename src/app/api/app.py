"""The web app: `create_app` connects the endpoints to the code behind them.

The endpoints serve the graph, the sources, the reports, the suggested questions, the
question stream and the file upload. The raw files and reports are found beside the
database, so a test can point the app at a temporary folder and leave the committed data alone.
"""

import logging
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from anyio import CancelScope, to_thread
from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field, StringConstraints

from app import ledger
from app.api import recorded, stream, suggestions, uploads, views
from app.brand import load_brand
from app.config import KG_PATH, ROOT, Settings, load_settings
from app.decide import backend_name
from app.ingest.report import default_report_path

logger = logging.getLogger(__name__)

REPLAY_REFUSAL = (
    "Ingest needs keys. This server is in replay mode, which has no OpenAI key, so a new "
    "file cannot be extracted; add keys to ingest files."
)
MAX_QUESTION_CHARS = 500
MAX_TURNS = 20
MAX_TURN_CHARS = 2000
MAX_WAITING = 3  # runs that may wait behind the one that runs, for asks and for ingests
BUSY_ASK = "Too many questions are waiting; try again in a moment."
BUSY_INGEST = "Too many files are waiting; try again in a moment."
EVALS_LATEST = ROOT / "data" / "evals" / "results" / "latest.json"
ONTOLOGY_PATH = ROOT / "config/ontology.json"


class Turn(BaseModel):
    """One earlier turn of the conversation."""

    role: Literal["technician", "assistant"]
    text: str = Field(max_length=MAX_TURN_CHARS)


class AskRequest(BaseModel):
    # Spaces are stripped first, so a question of only spaces counts as empty.
    question: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS
        ),
    ]
    history: list[Turn] = Field(default_factory=list, max_length=MAX_TURNS)


def decider_name(settings: Settings) -> str:
    """Name the backend that makes decisions for these settings, or "unavailable"."""
    try:
        return backend_name(settings)
    except ValueError:
        return "unavailable"


def spent_usd() -> float:
    """Return the money spent so far. A missing ledger counts as zero."""
    return ledger.spent_usd() if ledger.LEDGER_PATH.exists() else 0.0


def create_app(
    settings: Settings | None = None,
    kg_path: Path = KG_PATH,
    *,
    raw_dir: Path | None = None,
    evals_path: Path = EVALS_LATEST,
    map_path: Path | None = None,
    replay_check: Callable[[str], bool] | None = None,
    ingest_enabled: bool | None = None,
) -> FastAPI:
    """Build the app.

    The raw files and the fact map default to folders beside the database. By default,
    uploads are accepted only when an OpenAI key is set. Passing ingest_enabled=True
    lets a server without keys take uploads, which can then only replay saved answers.
    """
    settings = settings or load_settings()
    kg_path = Path(kg_path)
    raw_dir = Path(raw_dir) if raw_dir else kg_path.parent / "raw"
    map_path = Path(map_path) if map_path else kg_path.parent / "map" / "fact_map.json"
    brand = load_brand()
    replay_mode = not settings.has_openai
    ingest_on = settings.has_openai if ingest_enabled is None else ingest_enabled
    # One question runs at a time, in order, and only a few may wait. This keeps the
    # cost of each question separate from the others. The replay check takes a turn too.
    ask_line = stream.Line(MAX_WAITING)
    ingest_line = stream.Line(MAX_WAITING)  # one file at a time
    check = replay_check or (
        lambda question: suggestions.check_replayable(question, kg_path)
    )
    cached: list[dict[str, Any]] = []
    guard = threading.Lock()

    def replayable(question: str) -> bool:
        """Say whether saved answers cover a question. A failed check is logged and counts as no."""
        try:
            return bool(check(question))
        except Exception:
            logger.exception("the replay check failed for %r", question)
            return False

    def replay_status() -> list[dict[str, Any]]:
        """Say which suggested questions have saved answers. Checked once, then kept."""
        with guard:
            if not cached:
                with ask_line.turn():
                    cached.extend(
                        {"question": q, "replayable": replayable(q)}
                        for q in suggestions.QUESTIONS
                    )
            return cached

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await to_thread.run_sync(replay_status)
        yield

    app = FastAPI(title=brand.name, description=brand.description, lifespan=lifespan)
    app.add_middleware(
        uploads.UploadGuard,
        path="/api/ingest",
        refusal=None if ingest_on else REPLAY_REFUSAL,
    )

    @app.exception_handler(views.NotFound)
    async def not_found(request, error: views.NotFound):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, error: sqlite3.OperationalError):
        if not views.is_busy(error):
            raise error
        return JSONResponse({"detail": views.BUSY}, status_code=503)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "keys": {
                "openai": settings.has_openai,
                "typesafe": settings.has_typesafe,
                "langfuse": settings.has_langfuse,
            },
            "decider": "cache" if replay_mode else decider_name(settings),
            "replay_mode": replay_mode,
            "ingest_enabled": ingest_on,
            "budget": {"spent_usd": spent_usd(), "limit_usd": settings.budget_usd},
        }

    @app.get("/api/ontology")
    def ontology() -> Any:
        return views.load_json(ONTOLOGY_PATH, "ontology file")

    @app.get("/api/graph")
    def graph(
        focus: str | None = None, depth: int = Query(1, ge=1, le=2)
    ) -> dict[str, list]:
        return views.graph_elements(kg_path, focus, depth)

    @app.get("/api/activity")
    def activity(
        limit: int = Query(8, ge=1, le=50), alarm_limit: int = Query(5, ge=1, le=50)
    ) -> dict[str, list]:
        return views.activity(kg_path, limit, alarm_limit)

    @app.get("/api/sources")
    def sources() -> list[dict[str, Any]]:
        return views.list_sources(kg_path, raw_dir)

    @app.get("/api/sources/{doc_id}")
    def source(doc_id: str) -> dict[str, Any]:
        return views.get_source(kg_path, raw_dir, doc_id)

    @app.get("/api/sources/{doc_id}/file")
    def source_file(doc_id: str) -> FileResponse:
        path, media_type = views.source_file(kg_path, raw_dir, doc_id)
        return FileResponse(
            path,
            media_type=media_type,
            filename=path.name,
            content_disposition_type="inline",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
        )

    @app.get("/api/pipeline/latest")
    def pipeline() -> dict[str, Any]:
        return views.pipeline_latest(kg_path, default_report_path(kg_path, "full"))

    @app.get("/api/evals/latest")
    def evals() -> Any:
        return views.evals_latest(evals_path)

    @app.get("/api/map")
    def fact_map() -> Any:
        return views.fact_map(map_path)

    @app.get("/api/suggestions")
    def suggested() -> list[dict[str, Any]]:
        return replay_status()

    async def ask_job(body: AskRequest) -> AsyncIterator[stream.Job]:
        """Join the queue of questions and start the work, or refuse if the queue is full."""
        place = ask_line.join()
        if place is None:
            raise HTTPException(429, BUSY_ASK)
        turns = [f"{turn.role}: {turn.text}" for turn in body.history]
        job = stream.launch(
            ask_line,
            place,
            lambda: stream.ask_events(
                body.question, turns, kg_path, settings, replay=replay_mode
            ),
            stream.FAILED,
        )
        try:
            yield job
        finally:
            job.stop.set()

    @app.post("/api/ask", response_class=EventSourceResponse)
    async def ask(
        job: Annotated[stream.Job, Depends(ask_job)], request: Request
    ) -> AsyncIterator[ServerSentEvent]:
        try:
            while (item := await stream.next_event(job, request)) is not stream.DONE:
                yield item
        finally:
            job.stop.set()

    async def ingest_job(file: UploadFile) -> AsyncIterator[stream.Job]:
        """Save the upload, join the queue and start the work, or refuse the file.

        The saved file is removed again unless its data was loaded."""
        place = ingest_line.join()
        if place is None:
            raise HTTPException(429, BUSY_INGEST)
        try:
            data = await file.read(uploads.MAX_BYTES + 1)
            with CancelScope(shield=True):  # a saved file must reach the worker
                upload = await to_thread.run_sync(
                    uploads.save_upload, file.filename or "", data, raw_dir
                )
        except uploads.Refused as refused:
            ingest_line.leave(place)
            raise HTTPException(refused.status, refused.message) from None
        except BaseException:
            ingest_line.leave(place)
            raise
        saved = stream.Saved(
            upload.path, converted_from=upload.converted_from, chars=upload.chars
        )
        job = stream.launch(
            ingest_line,
            place,
            lambda: stream.ingest_events(saved, raw_dir, kg_path, settings, map_path),
            stream.INGEST_FAILED,
            saved.discard,
        )
        try:
            yield job
        finally:
            job.stop.set()

    @app.post("/api/ingest", response_class=EventSourceResponse)
    async def ingest(
        job: Annotated[stream.Job, Depends(ingest_job)], request: Request
    ) -> AsyncIterator[ServerSentEvent]:
        try:
            while (item := await stream.next_event(job, request)) is not stream.DONE:
                yield item
        finally:
            job.stop.set()

    recorded.mount(app)
    return app
