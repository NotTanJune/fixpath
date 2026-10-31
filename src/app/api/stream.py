"""Turn a question or an uploaded file into a stream of live events for the client.

`ask_events` streams the steps and the answer for one question. `ingest_events` streams
the stages of reading one uploaded file into the graph. Failures reach the client as one
plain error message, never as raw error text.

Each run happens in its own worker thread. `Line` makes runs take turns, `launch`
starts the worker, and `next_event` hands its events to the endpoint.
"""

import json
import logging
import queue
import re
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anyio import to_thread
from fastapi import Request
from fastapi.sse import ServerSentEvent

from app import factmap
from app.api import uploaded
from app.ask import crew
from app.ask.types import AnswerCard
from app.config import Settings
from app.decide import NoDecisionBackend
from app.decide.base import CacheOnlyDecider, DecisionCache
from app.ingest import graph as ingest_graph
from app.ingest import router
from app.ingest.normalize import doc_path_for, document_id
from app.ingest.report import default_report_path, ledger_mark, ledger_rows_since
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey

logger = logging.getLogger(__name__)

CACHE_MISS = (
    "This question is not in the replay cache; ask one of the suggested questions "
    "or add keys"
)
NO_DATABASE = "There is no graph database yet; run a full ingest first."
FAILED = "Something went wrong while answering. Try again."
INGEST_FAILED = "Something went wrong while ingesting the file. Try again."
STEP_FAILED = "this step failed"
POLL_SECONDS = 0.2  # how often, in seconds, a waiting run checks whether it should stop
DONE = object()  # the last item a worker puts on its queue
# Matches a note that holds raw error text, which the client must not see.
_EXCEPTION_NOTE = re.compile(r"(?:^|failed with )[A-Za-z_][A-Za-z0-9_]*: ")


def event(name: str, data: Any = None) -> ServerSentEvent:
    return ServerSentEvent(data=data, event=name)


def queued(ahead: int) -> ServerSentEvent:
    """Tell a waiting run how many runs are ahead of it."""
    return event("queued", {"position": ahead})


# --- one run at a time, in a worker thread -------------------------------------------


class Line:
    """A queue that lets one run go at a time, in the order they joined."""

    def __init__(self, max_waiting: int):
        self.max_waiting = max_waiting
        self._places: deque[object] = deque()
        self._changed = threading.Condition()

    def join(self) -> object | None:
        """Take a place at the end of the queue, or return None if it is full."""
        with self._changed:
            if len(self._places) > self.max_waiting:
                return None
            place = object()
            self._places.append(place)
            return place

    def leave(self, place: object) -> None:
        with self._changed:
            if place in self._places:
                self._places.remove(place)
            self._changed.notify_all()

    def wait_turn(
        self, place: object, stop: threading.Event, moved: Callable[[int], None]
    ) -> bool:
        """Wait until it is this place's turn, or give up if stop is set.

        Return True for a turn and False for a stop. Report the number of runs ahead
        each time it changes."""
        last = None
        with self._changed:
            while True:
                ahead = self._places.index(place)
                if ahead == 0:
                    return True
                if stop.is_set():
                    return False
                if ahead != last:
                    last = ahead
                    moved(ahead)
                self._changed.wait(POLL_SECONDS)

    @contextmanager
    def turn(self) -> Iterator[None]:
        """Wait for a turn and hold it, for work that is not a request."""
        with self._changed:
            place = object()
            self._places.append(place)
        try:
            self.wait_turn(place, threading.Event(), lambda ahead: None)
            yield
        finally:
            self.leave(place)


@dataclass
class Job:
    """The queue a worker fills with events, and the flag that tells it to stop."""

    out: "queue.Queue[Any]" = field(default_factory=queue.Queue)
    stop: threading.Event = field(default_factory=threading.Event)


def launch(
    line: Line,
    place: object,
    make: Callable[[], Iterator[ServerSentEvent]],
    failed: str,
    after: Callable[[], None] | None = None,
) -> Job:
    """Start a worker thread for one run and return its job.

    The worker waits for its turn, passes on each event, and stops when asked. When it
    ends, it runs the clean-up step, leaves the queue and marks the job as done. An
    unhandled error becomes one error event with the given message."""
    job = Job()

    def work() -> None:
        try:
            if line.wait_turn(place, job.stop, lambda n: job.out.put(queued(n))):
                with closing(make()) as events:
                    for item in events:
                        job.out.put(item)
                        if job.stop.is_set():
                            break
        except Exception:
            logger.exception("a run failed outside its events")
            job.out.put(event("error", {"message": failed}))
        finally:
            try:
                if after:
                    after()
            except Exception:
                logger.exception("the clean-up after a run failed")
            finally:
                line.leave(place)
                job.out.put(DONE)

    threading.Thread(target=work, daemon=True, name="run-worker").start()
    return job


async def next_event(job: Job, request: Request) -> Any:
    """Return the next event, or DONE when the worker finished or the client left."""
    while True:
        try:
            return await to_thread.run_sync(lambda: job.out.get(timeout=POLL_SECONDS))
        except queue.Empty:
            if await request.is_disconnected():
                return DONE


# --- POST /api/ask -----------------------------------------------------------------


def _decisions(card_triage) -> dict[str, Any]:
    """List what triage decided, in the form the client expects."""
    return {
        "machine": card_triage.asset_id,
        "alarm": card_triage.alarm_code,
        "root_cause": card_triage.failure_mode_id,
        "intent": card_triage.intent,
        "safety_critical": card_triage.safety_critical,
    }


def ask_error(error: Exception, settings: Settings) -> str:
    """Turn a failure of a question into a plain message."""
    if isinstance(error, (MissingApiKey, NoDecisionBackend)):
        return CACHE_MISS
    if isinstance(error, BudgetExceeded):
        return f"The model budget is used up: {error}."
    if isinstance(error, FileNotFoundError):
        return NO_DATABASE
    logger.error("the crew failed", exc_info=error)
    return FAILED


def public_notes(notes: list[str]) -> list[str]:
    """Hide raw error text in the notes. The client sees a plain "this step failed"."""
    return [STEP_FAILED if _EXCEPTION_NOTE.search(note) else note for note in notes]


def _shown(data: dict[str, Any]) -> dict[str, Any]:
    """Return the event data without its name and without raw error text."""
    shown = {k: v for k, v in data.items() if k != "event"}
    if "notes" in shown:
        shown["notes"] = public_notes(shown["notes"])
    return shown


def public_card(card: AnswerCard) -> dict[str, Any]:
    """Return the answer card as JSON, with safe notes."""
    shown = card.model_dump(mode="json")
    for entry in shown["trace"]:
        entry["notes"] = public_notes(entry["notes"])
    return shown


def crew_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the costs the question booked, not those of a file ingested at the same time."""
    purposes = tuple(crew.PURPOSES.values())
    return [row for row in rows if str(row["purpose"]).startswith(purposes)]


def ask_events(
    question: str,
    history: list[str],
    kg_path: Path | None,
    settings: Settings,
    *,
    replay: bool = False,
) -> Iterator[ServerSentEvent]:
    """Stream the events of one question, ending with the answer and the cost.

    A failure becomes a single error event. In replay mode only saved answers are used,
    whatever keys are set."""
    mark = ledger_mark()
    waiting: dict[str, Any] | None = None  # the end of triage, held back until its decisions are known
    card: AnswerCard | None = None
    try:
        with closing(
            crew.stream_ask(
                question,
                history,
                kg_path=kg_path,
                settings=settings,
                decider=CacheOnlyDecider(DecisionCache()) if replay else None,
            )
        ) as chunks:
            for chunk in chunks:
                data = chunk["data"]
                if chunk["type"] == "custom":
                    shown = _shown(data)
                    if data["event"] == "node_end" and data["node"] == "triage":
                        waiting = shown
                    else:
                        yield event(data["event"], shown)
                    continue
                for node, update in data.items():
                    if not isinstance(update, dict):
                        continue
                    if node == "triage":
                        triage = update["triage"]
                        yield event(
                            "node_end",
                            {
                                **(waiting or {"node": "triage"}),
                                "status": triage.status,
                                "decisions": _decisions(triage),
                                "confidence": triage.confidence,
                            },
                        )
                        waiting = None
                        if triage.status == "clarify":
                            yield event(
                                "clarify",
                                {
                                    "chips": [
                                        c.model_dump(mode="json") for c in triage.chips
                                    ],
                                    "reason": triage.reason,
                                },
                            )
                    elif node == "graph_agent":
                        for call in update["facts"].tool_calls:
                            yield event("tool_call", call.model_dump(mode="json"))
                    elif node == "assemble":
                        card = update["card"]
    except Exception as error:
        yield event("error", {"message": ask_error(error, settings)})
        return
    if card is None:
        yield event("error", {"message": FAILED})
        return
    rows = crew_totals(ledger_rows_since(mark))
    yield event("answer", public_card(card))
    yield event(
        "graph_path", {"path": [step.model_dump(mode="json") for step in card.path]}
    )
    yield event(
        "done",
        {
            "cost_usd": round(sum(row["usd"] for row in rows), 6),
            "calls": len(rows),
            "cache_hits": sum(int(row["cache_hit"]) for row in rows),
        },
    )


# --- POST /api/ingest --------------------------------------------------------------


@dataclass
class Saved:
    """An uploaded file that was saved, and whether its data reached the graph.

    The file stays only if it did, so a failed upload can be sent again."""

    path: Path
    loaded: bool = False
    converted_from: str | None = (
        None  # the ending of the original file, if it was converted
    )
    chars: int | None = None  # the length of the text a converted file became

    def discard(self) -> None:
        if not self.loaded:
            self.path.unlink(missing_ok=True)


def ingest_error(error: Exception) -> str:
    """Turn a failure of an ingest into a plain message."""
    if isinstance(error, (MissingApiKey, NoDecisionBackend)):
        return "This file needs keys: its extraction is not in the replay cache."
    if isinstance(error, BudgetExceeded):
        return f"The model budget is used up: {error}."
    if isinstance(error, FileNotFoundError):
        return NO_DATABASE
    if isinstance(error, router.UnknownFile):
        return "Cannot tell what kind of file this is."
    logger.error("the ingest failed", exc_info=error)
    return INGEST_FAILED


def _doc_type(saved: "Saved", extracted: dict[str, Any]) -> str | None:
    """Return the document type, from the extractor if it gave one."""
    if extracted.get("doc_type"):
        return extracted["doc_type"]
    try:
        return router.classify(saved.path)
    except router.UnknownFile:
        return None


def _keep_after_load(
    saved: "Saved",
    raw_dir: Path,
    kg_path: Path,
    map_path: Path | None,
    placement: factmap.Placement | None,
    extracted: dict[str, Any],
) -> None:
    """Add the file's points to the map and its entry to the upload list.

    The data is already in the graph, so a failure here is logged and is not an error.
    A map that was not updated is out of date, and `make map` rebuilds it."""
    if placement is not None and map_path is not None:
        try:
            factmap.append_placed(
                map_path,
                factmap.vectors_path(map_path),
                placement.passages,
                placement.facts,
                placement.vectors,
            )
        except Exception:
            logger.exception("the placed points could not be added to the map")
    try:
        doc_path = doc_path_for(saved.path, raw_dir)
        uploaded.record(
            kg_path, document_id(doc_path), doc_path, _doc_type(saved, extracted)
        )
    except Exception:
        logger.exception("the upload could not be added to the uploads list")


def ingest_events(
    saved: Saved,
    raw_dir: Path,
    kg_path: Path,
    settings: Settings,
    map_path: Path | None = None,
) -> Iterator[ServerSentEvent]:
    """Stream the events of ingesting one saved file, ending with its report.

    A failure becomes a single error event. After a successful load, the file is added
    to the map and to the upload list before the final event."""
    placement: factmap.Placement | None = None
    extracted: dict[str, Any] = {}
    try:
        if saved.converted_from is not None:
            yield event(
                "converted",
                {"from": saved.converted_from, "to": ".txt", "chars": saved.chars},
            )
        with closing(
            ingest_graph.stream_ingest(
                saved.path,
                raw_dir=raw_dir,
                kg_path=kg_path,
                settings=settings,
                map_path=map_path,
            )
        ) as chunks:
            for chunk in chunks:
                data = chunk["data"]
                if chunk["type"] == "custom":
                    shown = {k: v for k, v in data.items() if k != "event"}
                    if data["event"] == "extracted":
                        extracted = shown
                    yield event(data["event"], shown)
                    continue
                for node, update in data.items():
                    saved.loaded = saved.loaded or node == "load"
                    if node == "load" and isinstance(update, dict):
                        placement = update.get("placed")
                    yield event("step", {"node": node})
        report = json.loads(
            default_report_path(kg_path, "file").read_text(encoding="utf-8")
        )
    except Exception as error:
        yield event("error", {"message": ingest_error(error)})
        return
    _keep_after_load(saved, raw_dir, kg_path, map_path, placement, extracted)
    yield event("done", {"report": report})
