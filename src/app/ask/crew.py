"""Runs the whole troubleshooting flow, from the question to the answer card.

Triage first reads the question. If it is clear and in scope, a graph agent and a manual
agent look things up in parallel. A synthesizer then writes a summary with cited claims,
a verifier checks each claim, and assemble builds the card. If the question needs
clarifying or is out of scope, the flow skips straight to assemble.

If a model step fails, the card is still built without that step's result and says what
is missing. Budget and setup errors stop the run instead. Each step reports when it
starts and ends, with its timing and cost.

ask returns the card. stream_ask yields progress events as the run goes.
"""

import operator
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from app import llm_cache
from app.ask import tools
from app.ask.assemble import (
    GRAPH_FAILED_NOTE,
    RETRIEVAL,
    SUMMARY_ID,
    build_card,
    rank_causes,
)
from app.ask.graph_agent import STILL_ASKING, graph_agent
from app.ask.manual_agent import manual_agent
from app.ask.synthesize import Synthesized, synthesize
from app.ask.triage import triage as run_triage
from app.ask.types import (
    AnswerCard,
    Claim,
    GraphFacts,
    Hints,
    ManualPassages,
    NodeTrace,
    ToolCall,
    Triage,
)
from app.ask.verify import is_kept, verify
from app.config import KG_PATH, Settings, load_settings
from app.decide import Decider, NoDecisionBackend, get_decider
from app.embed import Embedder
from app.ingest.report import ledger_mark, ledger_rows_since
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey, NoCacheInstalled, tracing_callbacks

RUN_NAME = "ask"
MAX_CONCURRENCY = 4
# These errors stop the run. Any other error in a model step is caught, and the card is
# built without that step.
FATAL = (BudgetExceeded, MissingApiKey, NoDecisionBackend, NoCacheInstalled)
MAX_ERROR_CHARS = 200
_STAMP = TypeAdapter(datetime)
# The names each step uses when it records its model costs.
PURPOSES = {
    "triage": "triage",
    "graph_agent": "graph agent",
    "manual_agent": "manual agent",
    "synthesize": "synthesize",
    "verify": "verify",
}
NO_DATABASE = "there is no graph database at {path}; run a full ingest first"


@dataclass
class Models:
    """Models to use in place of the real ones, for tests. Any left empty is built as usual."""

    graph: BaseChatModel | None = None
    manual: BaseChatModel | None = None
    synth: BaseChatModel | None = None
    embedder: Embedder | None = None


@dataclass
class Run:
    """Everything that stays fixed while one question runs."""

    kg_path: Path
    decider: Decider
    models: Models
    mark: int
    settings: Settings  # shared by every model step


class CrewState(TypedDict, total=False):
    question: str
    history: list[str]
    triage: Triage
    facts: GraphFacts
    graph_failed: bool  # the graph agent failed, so assemble reads the facts itself
    passages: ManualPassages
    synthesis: Synthesized
    judged: list[Claim]
    card: AnswerCard
    # the two parallel agents each add their own entry
    trace: Annotated[list[NodeTrace], operator.add]


# --- timing, cost and events ---------------------------------------------------------


def _booked(run: Run, name: str) -> tuple[str | None, bool | None, float]:
    """The model, cache use and cost a step recorded in the spend ledger."""
    purpose = PURPOSES.get(name)
    found = [
        row
        for row in ledger_rows_since(run.mark)
        if purpose and str(row["purpose"]).startswith(purpose)
    ]
    if not found:
        return None, None, 0.0
    models = ", ".join(dict.fromkeys(row["model"] for row in found))
    return (
        models,
        all(row["cache_hit"] for row in found),
        sum(row["usd"] for row in found),
    )


def _traced(run: Run, name: str, body: Callable[[CrewState, Any], dict[str, Any]]):
    """Wrap a step so it reports its start and end and records its timing."""

    def node(state: CrewState) -> dict[str, Any]:
        write = get_stream_writer()
        started = datetime.now(UTC)
        clock = time.perf_counter()
        write(
            {
                "event": "node_start",
                "node": name,
                "at": _STAMP.dump_python(started, mode="json"),
            }
        )
        update = body(state, run)
        notes = update.pop("trace_notes", [])
        ended = datetime.now(UTC)
        backend, cache_hit, cost = _booked(run, name)
        entry = NodeTrace(
            name=name,
            started_at=started,
            ended_at=ended,
            seconds=round(time.perf_counter() - clock, 4),
            backend=backend,
            cache_hit=cache_hit,
            cost_usd=cost,
            notes=notes,
        )
        if (
            "card" in update
        ):  # the card carries the full trace, including this last step
            trace = sorted([*state.get("trace", []), entry], key=lambda t: t.started_at)
            update["card"] = update["card"].model_copy(update={"trace": trace})
        write(
            {
                "event": "node_end",
                "node": name,
                **entry.model_dump(mode="json", exclude={"name"}),
            }
        )
        return {**update, "trace": [entry]}

    return node


# --- the nodes ---------------------------------------------------------------------------


def _triage_node(state: CrewState, run: Run) -> dict[str, Any]:
    with closing(tools.connect_ro(run.kg_path)) as conn:
        result = run_triage(state["question"], state.get("history"), conn, run.decider)
    return {"triage": result}


def _hints(triage: Triage) -> Hints:
    return Hints(
        asset_id=triage.asset_id,
        alarm_code=triage.alarm_code,
        failure_mode_id=triage.failure_mode_id,
        intent=triage.intent,
    )


def _failed(error: Exception) -> str:
    """A short description of an error, for the trace."""
    return f"{type(error).__name__}: {str(error)[:MAX_ERROR_CHARS]}"


def _lead_with_the_symptom(
    facts: GraphFacts, triage: Triage, question: str, conn: sqlite3.Connection
) -> GraphFacts:
    """Put the cause that matches the symptom first. If that changes the top cause, read
    the past breakdowns again for it. If the read fails, keep the earlier ones."""
    causes = rank_causes(facts.causes, triage)
    if not causes or not causes[0].matches_symptom:
        return facts.model_copy(update={"causes": causes})
    first, code = causes[0], facts.alarm_code or triage.alarm_code
    args = {"asset_id": first.asset_id, "symptom": question}
    if code:
        args["code"] = code
    args["failure_mode_id"] = first.failure_mode_id
    try:
        breakdowns = tools.similar_breakdowns(
            conn, first.asset_id, code, first.failure_mode_id, question
        )
    except sqlite3.Error:
        return facts.model_copy(update={"causes": causes})
    call = ToolCall(
        name="similar_breakdowns",
        args=args,
        filled_by="code",
        result_count=len(breakdowns),
    )
    return facts.model_copy(
        update={
            "causes": causes,
            "breakdowns": breakdowns,
            "tool_calls": [*facts.tool_calls, call],
        }
    )


def _graph_node(state: CrewState, run: Run) -> dict[str, Any]:
    try:
        with closing(tools.connect_ro(run.kg_path)) as conn:
            facts = graph_agent(
                state["question"],
                _hints(state["triage"]),
                conn,
                model=run.models.graph,
                settings=run.settings,
            )
            # later steps see the symptom-matching cause first
            facts = _lead_with_the_symptom(
                facts, state["triage"], state["question"], conn
            )
    except FATAL:
        raise
    except Exception as error:
        return {
            "facts": GraphFacts(notes=[GRAPH_FAILED_NOTE]),
            "graph_failed": True,
            "trace_notes": [_failed(error)],
        }
    # the note about a cut-off search is for developers, so it goes to the trace
    cut = [n for n in facts.notes if n.startswith(STILL_ASKING)]
    facts = facts.model_copy(update={"notes": [n for n in facts.notes if n not in cut]})
    return {"facts": facts, "trace_notes": cut}


def _manual_node(state: CrewState, run: Run) -> dict[str, Any]:
    try:
        with closing(tools.connect_ro(run.kg_path)) as conn:
            found = manual_agent(
                state["question"],
                _hints(state["triage"]),
                conn,
                model=run.models.manual,
                embedder=run.models.embedder,
                settings=run.settings,
            )
    except FATAL:
        raise
    except Exception as error:
        note = "the manual search failed; the card shows the graph facts only"
        return {
            "passages": ManualPassages(notes=[note]),
            "trace_notes": [_failed(error)],
        }
    return {"passages": found}


def _synthesize_node(state: CrewState, run: Run) -> dict[str, Any]:
    try:
        result = synthesize(
            state["question"],
            state["triage"],
            state["facts"],
            state["passages"],
            model=run.models.synth,
            settings=run.settings,
        )
    except FATAL:
        raise
    except Exception as error:
        note = "the summary could not be written; the card shows the graph facts without it"
        return {
            "synthesis": Synthesized("", [], {}, [note]),
            "trace_notes": [_failed(error)],
        }
    return {"synthesis": result, "trace_notes": list(result.dropped)}


def _not_shown(claim: Claim) -> str:
    """A trace note saying why the verifier did not keep the summary."""
    if claim.verdict is None:
        why = claim.reason
    else:
        why = f"{claim.verdict} at {claim.p:.2f}, support {claim.support:.2f}"
    return f'the summary was not shown ({why}): "{claim.text}"'


def _verify_node(state: CrewState, run: Run) -> dict[str, Any]:
    """Check the claims, and check the summary as one more claim. The card shows the
    summary only if it passes."""
    synthesis = state["synthesis"]
    claims = list(synthesis.claims)
    if synthesis.summary:
        cited = list(
            dict.fromkeys(cid for claim in claims for cid in claim.citation_ids)
        )
        claims.append(Claim(id=SUMMARY_ID, text=synthesis.summary, citation_ids=cited))
    judged = verify(claims, synthesis.citations, run.decider)
    notes = [
        _not_shown(claim)
        for claim in judged
        if claim.id == SUMMARY_ID and not is_kept(claim)
    ]
    return {"judged": judged, "trace_notes": notes}


def _assemble_node(state: CrewState, run: Run) -> dict[str, Any]:
    trace: list[str] = []
    card = build_card(state, run.kg_path, trace)
    return {"card": card, "trace_notes": trace}


# --- the graph ---------------------------------------------------------------------------------


def build_graph(run: Run):
    """Build the step graph for one question."""

    def route(state: CrewState) -> list[str] | str:
        if state["triage"].status in RETRIEVAL:
            return ["graph_agent", "manual_agent"]
        return "assemble"

    nodes = {
        "triage": _triage_node,
        "graph_agent": _graph_node,
        "manual_agent": _manual_node,
        "synthesize": _synthesize_node,
        "verify": _verify_node,
        "assemble": _assemble_node,
    }
    builder = StateGraph(CrewState)
    for name, body in nodes.items():
        builder.add_node(name, _traced(run, name, body))
    builder.add_edge(START, "triage")
    builder.add_conditional_edges(
        "triage", route, ["graph_agent", "manual_agent", "assemble"]
    )
    builder.add_edge(["graph_agent", "manual_agent"], "synthesize")
    builder.add_edge("synthesize", "verify")
    builder.add_edge("verify", "assemble")
    builder.add_edge("assemble", END)
    return builder.compile()


def run_config(settings: Settings) -> dict[str, Any]:
    """Settings for a run: its name, a limit on parallel steps, and tracing if enabled."""
    config: dict[str, Any] = {"max_concurrency": MAX_CONCURRENCY, "run_name": RUN_NAME}
    handlers = tracing_callbacks(settings)
    if handlers:
        config["callbacks"] = handlers
    return config


def _start(
    question: str,
    history: list[str] | None,
    kg_path: Path,
    settings: Settings | None,
    decider: Decider | None,
    models: Models | None,
):
    """Set up the graph, its input and its config for one question."""
    settings = settings or load_settings()
    kg_path = Path(kg_path)
    if not kg_path.is_file():
        raise FileNotFoundError(NO_DATABASE.format(path=kg_path))
    llm_cache.enable_cache()
    run = Run(
        kg_path=kg_path,
        decider=decider or get_decider(settings),
        models=models or Models(),
        mark=ledger_mark(),
        settings=settings,
    )
    inputs = {"question": question, "history": list(history or [])}
    return build_graph(run), inputs, run_config(settings)


def ask(
    question: str,
    history: list[str] | None = None,
    *,
    kg_path: Path = KG_PATH,
    settings: Settings | None = None,
    decider: Decider | None = None,
    models: Models | None = None,
) -> AnswerCard:
    """Answer a question and return the card. history holds the earlier turns, oldest first."""
    graph, inputs, config = _start(
        question, history, kg_path, settings, decider, models
    )
    return graph.invoke(inputs, config)["card"]


def stream_ask(
    question: str,
    history: list[str] | None = None,
    *,
    kg_path: Path = KG_PATH,
    settings: Settings | None = None,
    decider: Decider | None = None,
    models: Models | None = None,
) -> Iterator[dict[str, Any]]:
    """Answer a question and yield progress events and step results as they happen."""
    graph, inputs, config = _start(
        question, history, kg_path, settings, decider, models
    )
    yield from graph.stream(
        inputs, config, stream_mode=["updates", "custom"], version="v2"
    )
