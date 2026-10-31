"""The graph agent looks up facts in the plant's knowledge graph for one question.

A language model chooses which graph tools to call, one round at a time. Every fact on
the result comes from a tool, never from the model's own words. For a troubleshooting
question about a known machine, code then fills in whatever the model skipped: the
alarm's causes, the fixes, the parts and similar past breakdowns.

graph_agent is the entry point and returns the facts it found. A question is about one
machine, so a call about any other machine is refused.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import llm
from app.ask import tools
from app.ask.types import Cause, Fix, GraphFacts, Hints, ToolCall
from app.config import Settings
from app.llm import call_config, chat_model

PURPOSE = "graph agent"
MAX_ROUNDS = 6
MAX_TOOL_CALLS = 8
TROUBLESHOOT = "troubleshoot"
# Fields the model does not see. They are long and do not help it pick the next tool.
HIDDEN_FROM_MODEL = {"citations", "path", "steps", "matches_symptom"}
# Start of the note left when the search was cut off. The crew moves it to the trace.
STILL_ASKING = "the agent was still asking for tools"

SYSTEM_PROMPT = """\
You are the graph agent of a maintenance assistant for a precision-machining plant. \
You look facts up in the plant's knowledge graph with the tools you are given. You \
never answer from memory: every fact you pass on has to come from a tool result, and \
your own words are not kept.

Tools:
- find_machine: resolve a machine name or alias to its id, when the id is not known.
- alarm_causes: the likely root causes of an alarm on a machine, with how often each \
one was the cause before.
- fixes_for: the procedures that fix a root cause.
- parts_for: the parts a procedure needs, with the stock on hand.
- similar_breakdowns: past incidents on the machine that look like this one.
- alarm_history: how often the machine raised an alarm.

For a troubleshooting question about a known machine and alarm: get the causes, then \
the fixes of the most likely cause, then the parts of those fixes. similar_breakdowns \
and alarm_history do not depend on other calls, so ask for them in the first round with \
alarm_causes. Use the ids in the hints. Make independent calls in the same round. When \
you have what you need, reply with the single word done.
"""


# --- the tools the model sees --------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _IdArgs(_Args):
    @field_validator("*", mode="after")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class FindMachineArgs(_Args):
    text: str = Field(
        description="A machine id, name or alias as the technician wrote it."
    )


class AlarmCausesArgs(_IdArgs):
    asset_id: str = Field(description="The machine id, for example HP-01.")
    code: str = Field(description="The alarm code, for example E-217.")


class FixesForArgs(_IdArgs):
    failure_mode_id: str = Field(description="A root cause id from alarm_causes.")


class PartsForArgs(_IdArgs):
    procedure_id: str = Field(description="A procedure id from fixes_for.")


class SimilarBreakdownsArgs(_IdArgs):
    asset_id: str = Field(description="The machine id.")
    code: str | None = Field(
        None, description="Prefer incidents that started with this alarm."
    )
    failure_mode_id: str | None = Field(
        None, description="Prefer incidents with this root cause."
    )
    symptom: str | None = Field(
        None, description="Prefer incidents with a like symptom."
    )
    limit: int = Field(5, ge=1, le=10, description="How many incidents to return.")


class AlarmHistoryArgs(_IdArgs):
    asset_id: str = Field(description="The machine id.")
    code: str = Field(description="The alarm code.")


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    args: type[_Args]
    run: Callable[[sqlite3.Connection, Any], Any]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args.model_json_schema(),
            },
        }


# Each call looks the function up in app.ask.tools, so tests can swap it.
TOOLS: dict[str, _Tool] = {
    tool.name: tool
    for tool in (
        _Tool(
            "find_machine",
            "Find the machine ids that match a name, alias or id.",
            FindMachineArgs,
            lambda conn, a: tools.find_machine(conn, a.text),
        ),
        _Tool(
            "alarm_causes",
            "The root causes that an alarm indicates on a machine, most likely first.",
            AlarmCausesArgs,
            lambda conn, a: tools.alarm_causes(conn, a.asset_id, a.code),
        ),
        _Tool(
            "fixes_for",
            "The procedures that fix a root cause.",
            FixesForArgs,
            lambda conn, a: tools.fixes_for(conn, a.failure_mode_id),
        ),
        _Tool(
            "parts_for",
            "The parts a procedure needs, with quantity, stock on hand, bin and lead time.",
            PartsForArgs,
            lambda conn, a: tools.parts_for(conn, a.procedure_id),
        ),
        _Tool(
            "similar_breakdowns",
            "Past incidents on a machine like the current one, with technician and fix.",
            SimilarBreakdownsArgs,
            lambda conn, a: tools.similar_breakdowns(
                conn, a.asset_id, a.code, a.failure_mode_id, a.symptom, a.limit
            ),
        ),
        _Tool(
            "alarm_history",
            "How often a machine raised an alarm, and when first and last.",
            AlarmHistoryArgs,
            lambda conn, a: tools.alarm_history(conn, a.asset_id, a.code),
        ),
    )
}


# --- one run ---------------------------------------------------------------------------


def _upper(value: str | None) -> str | None:
    return value.strip().upper() if value and value.strip() else None


def _content(result: Any) -> str:
    """The tool result as the text the model reads."""

    def dump(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json", exclude=HIDDEN_FROM_MODEL)
        return item

    shown = (
        [dump(item) for item in result] if isinstance(result, list) else dump(result)
    )
    return json.dumps(shown, ensure_ascii=False)


class _Run:
    """One run of the graph agent: the facts so far and what was already fetched."""

    def __init__(self, question: str, hints: Hints, conn: sqlite3.Connection):
        self.question, self.hints, self.conn = question, hints, conn
        self.facts = GraphFacts()
        self.agent_calls = 0
        self.causes: dict[tuple[str, str], list[Cause]] = {}
        self.fixes: dict[str, list[Fix]] = {}
        self.parts: set[str] = set()
        self.breakdowns: set[str] = set()
        # The machine the question is about: from triage, or the first machine a call
        # finds facts for, or one that find_machine matches exactly.
        self.pinned: str | None = _upper(hints.asset_id)
        self.code_hit: str | None = None

    # --- running a call

    def execute(self, name: str, args: Any, filled_by: str) -> str:
        """Run one tool call and return the reply for the model."""
        shown = args if isinstance(args, dict) else {"raw": str(args)}
        tool = TOOLS.get(name)
        if tool is None:
            return self.fail(
                name,
                shown,
                filled_by,
                f"unknown tool {name!r}; the tools are {', '.join(TOOLS)}",
            )
        try:
            parsed = tool.args.model_validate(args)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}"
                for e in error.errors(include_url=False)
            )
            return self.fail(
                name, shown, filled_by, f"bad arguments for {name}: {problems}"
            )
        other = _upper(getattr(parsed, "asset_id", None))
        if self.pinned and other and other != self.pinned:
            return self.fail(
                name,
                shown,
                filled_by,
                f"this question is about {self.pinned}; facts for {other} were not fetched",
            )
        try:
            result = tool.run(self.conn, parsed)
        except sqlite3.Error as error:
            return self.fail(
                name, shown, filled_by, f"the {name} lookup failed: {error}"
            )
        count = self._absorb(name, parsed, result)
        self.facts.tool_calls.append(
            ToolCall(name=name, args=shown, filled_by=filled_by, result_count=count)
        )
        return _content(result)

    def fail(self, name: str, args: dict, filled_by: str, message: str) -> str:
        self.facts.tool_calls.append(
            ToolCall(name=name, args=args, filled_by=filled_by, result_count=0)
        )
        self.facts.notes.append(f"{filled_by} call to {name} failed: {message}")
        return f"Error: {message}"

    # --- keeping what a tool returned

    def _absorb(self, name: str, args: Any, result: Any) -> int:
        items = result if isinstance(result, list) else [result]
        if name == "find_machine":
            self._pin_from_find(args.text, items)
            return len(items)
        for item in items:
            for step in item.path:
                if step not in self.facts.path:
                    self.facts.path.append(step)
        asset = _upper(getattr(args, "asset_id", None))
        code = _upper(getattr(args, "code", None))
        if name == "alarm_causes":
            self.causes.setdefault((asset, code), [])
            group = self.causes[asset, code]
            self._add(group, self.facts.causes, items, lambda c: c.failure_mode_id)
            count = len(items)
        elif name == "fixes_for":
            mode = _upper(args.failure_mode_id)
            self.fixes.setdefault(mode, [])
            self._add(
                self.fixes[mode], self.facts.fixes, items, lambda f: f.procedure_id
            )
            return len(items)
        elif name == "parts_for":
            self.parts.add(_upper(args.procedure_id))
            self._add([], self.facts.parts, items, lambda p: (p.path[0].src, p.part_no))
            return len(items)
        elif name == "similar_breakdowns":
            self.breakdowns.add(asset)
            self._add([], self.facts.breakdowns, items, lambda b: b.incident_id)
            count = len(items)
        else:  # alarm_history
            self._add([], self.facts.history, items, lambda h: (h.asset_id, h.code))
            count = int(bool(items[0].count or items[0].incidents))
        if count and self.pinned is None:
            self.pinned = asset
        if count and code and self.code_hit is None:
            self.code_hit = code
        return count

    def _pin_from_find(self, text: str, items: list[dict]) -> None:
        """Accept the machine only on one exact match. A close match may be another
        machine, and several matches do not name one."""
        if self.pinned is not None or not items:
            return
        ids = ", ".join(item["id"] for item in items)
        if len(items) == 1 and items[0]["score"] == 100:
            self.pinned = items[0]["id"]
        elif all(item["score"] == 100 for item in items):
            self.facts.notes.append(
                f"find_machine matched {len(items)} machines for {text!r} ({ids}), "
                "so none was chosen"
            )
        else:
            self.facts.notes.append(
                f"find_machine found only a close match for {text!r} ({ids}); it was not used"
            )

    @staticmethod
    def _add(group: list, into: list, items: list, key: Callable[[Any], Any]) -> None:
        """Add the items not yet in `into`, and record them all in `group`."""
        known = {key(item) for item in into}
        for item in items:
            if key(item) not in known:
                into.append(item)
                known.add(key(item))
            group.append(item)

    # --- completing the facts

    def _read_cause_on(self, asset: str, mode: str) -> bool:
        """Read a root cause on the machine without an alarm. Only code uses this.
        Return False if the machine has no such cause or the read failed."""
        args = {"asset_id": asset, "failure_mode_id": mode}
        try:
            cause = tools.cause_on(self.conn, asset, mode)
        except sqlite3.Error as error:
            self.fail("cause_on", args, "code", f"the cause_on lookup failed: {error}")
            return False
        count = int(cause is not None)
        self.facts.tool_calls.append(
            ToolCall(name="cause_on", args=args, filled_by="code", result_count=count)
        )
        if cause is None:
            return False
        self._add([], self.facts.causes, [cause], lambda c: c.failure_mode_id)
        self.facts.path += [step for step in cause.path if step not in self.facts.path]
        return True

    def _keep_only(self, asset: str | None) -> None:
        """Drop facts about any other machine, such as a result fetched before the
        machine was known."""
        facts = self.facts
        if asset is None:
            return
        before = len(facts.causes) + len(facts.breakdowns) + len(facts.history)
        facts.causes = [c for c in facts.causes if c.asset_id == asset]
        facts.breakdowns = [b for b in facts.breakdowns if b.asset_id == asset]
        facts.history = [h for h in facts.history if h.asset_id == asset]
        if before != len(facts.causes) + len(facts.breakdowns) + len(facts.history):
            facts.path = []
            for group in (
                facts.causes,
                facts.fixes,
                facts.parts,
                facts.breakdowns,
                facts.history,
            ):
                for item in group:
                    facts.path += [s for s in item.path if s not in facts.path]

    def complete(self) -> GraphFacts:
        hints, facts = self.hints, self.facts
        facts.asset_id = self.pinned
        facts.alarm_code = _upper(hints.alarm_code) or self.code_hit
        self._keep_only(facts.asset_id)
        if hints.intent != TROUBLESHOOT:
            return facts
        asset, code = facts.asset_id, facts.alarm_code
        if asset is None:
            facts.notes.append(
                "the machine is not known, so no machine facts were fetched"
            )
            return facts
        mode = hints.failure_mode_id.strip().upper() if hints.failure_mode_id else None
        if code:
            if (asset, code) not in self.causes:
                self.execute("alarm_causes", {"asset_id": asset, "code": code}, "code")
            found = self.causes.get((asset, code))
            if found:
                mode = found[0].failure_mode_id
            else:
                facts.notes.append(
                    f"no root cause is on record for alarm {code} on {asset}"
                )
        else:
            # triage only keeps a root cause it is confident about, so trust it and
            # read it on the machine without an alarm
            if not (mode and self._read_cause_on(asset, mode)):
                note = "no alarm code is known, so no causes were looked up"
                facts.notes.append(note)
        if mode:
            if mode not in self.fixes:
                self.execute("fixes_for", {"failure_mode_id": mode}, "code")
            for fix in self.fixes.get(mode, []):
                if fix.procedure_id not in self.parts:
                    self.execute(
                        "parts_for", {"procedure_id": fix.procedure_id}, "code"
                    )
        if asset not in self.breakdowns:
            args = {"asset_id": asset, "symptom": self.question}
            if code:
                args["code"] = code
            if mode:
                args["failure_mode_id"] = mode
            self.execute("similar_breakdowns", args, "code")
        return facts


# --- the loop --------------------------------------------------------------------------


def graph_agent(
    question: str,
    hints: Hints,
    conn: sqlite3.Connection,
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
) -> GraphFacts:
    """Run the graph agent for one question and return the facts it found.

    hints are triage's picks. conn is a read-only connection to the graph.
    """
    run = _Run(question, hints, conn)
    if settings is None:
        settings = llm.load_settings()
    model = model or chat_model("terra", "low", settings=settings)
    bound = model.bind_tools([tool.schema() for tool in TOOLS.values()])
    config = call_config(PURPOSE, settings)
    messages: list[BaseMessage] = [
        SystemMessage(SYSTEM_PROMPT),
        HumanMessage(
            f"Question: {question}\n\nTriage hints:\n"
            + json.dumps(hints.model_dump(), indent=2)
        ),
    ]
    refused = False
    invalid_seen = 0
    for _ in range(MAX_ROUNDS):
        reply = bound.invoke(messages, config=config)
        messages.append(reply)
        asked = [(c["name"], c["args"], c["id"], False) for c in reply.tool_calls]
        asked += [
            (c["name"], c["args"], c["id"], True) for c in reply.invalid_tool_calls
        ]
        if not asked:
            break
        for name, args, call_id, invalid in asked:
            if invalid:  # the model provider may leave out the id or the name
                call_id = call_id or f"invalid_{invalid_seen}"
                name = name or "unknown"
                invalid_seen += 1
            if run.agent_calls >= MAX_TOOL_CALLS:
                refused = True
                text = (
                    f"Error: the limit of {MAX_TOOL_CALLS} tool calls is reached; "
                    "this call was not run."
                )
            else:
                run.agent_calls += 1
                if invalid:  # the arguments could not be read
                    text = run.fail(
                        name,
                        {"raw": str(args)},
                        "agent",
                        "the arguments are not valid JSON",
                    )
                else:
                    text = run.execute(name, args, "agent")
            messages.append(ToolMessage(content=text, tool_call_id=call_id, name=name))
    else:  # the last reply still asked for tools, and the model never saw the results
        run.facts.notes.append(
            f"{STILL_ASKING} after {MAX_ROUNDS} rounds, so it was not asked again"
        )
    if refused:
        run.facts.notes.append(f"the limit of {MAX_TOOL_CALLS} tool calls was reached")
    return run.complete()
