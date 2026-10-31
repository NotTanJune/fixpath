"""Compares two decision services, JEV and OpenAI Decisions, on the same questions.

Both services answer every question once, and the answers are saved, so a rerun costs
nothing. The questions are five kinds of check: whether two mentions name the same
thing, whether two records describe the same incident, which root cause fits an
incident, how a ticket is triaged, and whether a claim in a crew answer is supported.
Each kind gets an accuracy, a calibration score and a cost per 1,000 asks for each
service. A sign test decides who leads each kind. If the difference is not clear, the
verdict says so. The pipeline was tuned on JEV, so the results carry a note about that
bias.

merge_cases() collects the questions, evaluate() scores both services and builds the
results section, and timing_pass() measures how long fresh calls take. Latency is kept
from the last timed run unless a new one is made.
"""

import datetime as dt
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from math import comb
from pathlib import Path
from typing import Any

from app.ask.triage import PURPOSE as TRIAGE_PURPOSE
from app.ask.verify import MIN_P, support_mass
from app.config import DECISIONS_MODEL, JEV_MODEL, Settings
from app.decide import Answer, Decider, DecisionError, Question
from app.decide.base import CachedDecider, DecisionCache, NoDecisionBackend
from app.decide.jev import JevBackend
from app.decide.openai_decisions import DecisionsBackend
from app.evals import layer2, layer3, layer4, replay
from app.evals.labels import ENTITY_FIELDS, Record, label_entity, label_incident
from app.evals.match import Matching
from app.evals.metrics import GATE_SWEEP, Scored, ratio, reliability
from app.evals.truth import Graph, Truth
from app.ingest import questions
from app.ingest.link import link
from app.ingest.normalize import AliasTable
from app.ingest.resolve import resolve
from app.ingest.types import Extraction
from app.ledger import cost, record

BACKENDS = ("jev", "decisions")
MODELS = {"jev": JEV_MODEL, "decisions": DECISIONS_MODEL}
TUNED_ON = "jev"
BIAS = (
    "The gates and the prompts of the pipeline were tuned on JEV, so JEV is read with "
    "that bias in its favour: Decisions is scored at the gate that was set for JEV. "
    "The best gate of each backend is chosen from a sweep on the same questions it is "
    "scored on, so it is in sample and flatters the backend. The questions come from "
    "the same distribution the gates were tuned on, not from a hold-out. The verify "
    "labels come from one LLM judge, and a task is led only when a paired sign test "
    "says so."
)
SIGNIFICANT = 0.05
LATENCY_CAVEAT = (
    "The latency pass is one run of 30 triage asks per backend, not interleaved "
    "(JEV first, then Decisions), at one time of day; it covers triage asks only."
)
PURPOSE = "bakeoff"
TASKS = ("same_entity", "same_incident", "root_cause", "triage", "verify")
TIMING_ASKS = 30

QuestionKey = tuple[str, str, str, str]


# --- the two backends -----------------------------------------------------------------


class ReplayDecider:
    """Serves saved answers for one service and never calls it. A missing answer raises
    an error, so a run without keys cannot spend money."""

    def __init__(self, cache: DecisionCache, backend: str):
        self.cache = cache
        self.name = backend
        self.model = MODELS[backend]

    def ask(
        self, state: str | dict, questions_: dict[str, Question], purpose: str
    ) -> dict[str, Answer]:
        entry = self.cache.get(
            DecisionCache.key(self.name, self.model, state, questions_)
        )
        if entry is None:
            raise NoDecisionBackend(
                f"no cached {self.name} answer for a {purpose} question; run the "
                f"bake-off with the {self.name} key set"
            )
        usage = entry.usage
        record(
            self.model,
            purpose,
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.output_tokens,
            True,
        )
        return entry.answers


class Metered:
    """Wraps a decider to count its asks and price them from the saved token counts."""

    def __init__(self, inner: Decider, cache: DecisionCache):
        self.inner = inner
        self.cache = cache
        self.name = inner.name
        self.model = inner.model
        self.asks = 0
        self.usd = 0.0

    def ask(
        self, state: str | dict, questions_: dict[str, Question], purpose: str
    ) -> dict[str, Answer]:
        answers = self.inner.ask(state, questions_, purpose)
        self.asks += 1
        entry = self.cache.get(
            DecisionCache.key(self.name, self.model, state, questions_)
        )
        if entry is not None:
            usage = entry.usage
            self.usd += cost(
                self.model,
                usage.input_tokens,
                usage.cached_input_tokens,
                usage.output_tokens,
            )
        return answers

    def snapshot(self) -> tuple[int, float]:
        return self.asks, self.usd


def make_deciders(
    settings: Settings, cache: DecisionCache | None = None
) -> dict[str, Metered]:
    """One counting decider per service. With a key it calls the service and saves new
    answers. Without a key it only replays saved answers."""
    cache = cache or DecisionCache()
    inner: dict[str, Decider] = {}
    if settings.has_typesafe:
        inner["jev"] = CachedDecider(
            JevBackend(settings.typesafe_api_key), cache, settings
        )
    else:
        inner["jev"] = ReplayDecider(cache, "jev")
    if settings.has_openai:
        inner["decisions"] = CachedDecider(
            DecisionsBackend(settings.openai_api_key), cache, settings
        )
    else:
        inner["decisions"] = ReplayDecider(cache, "decisions")
    return {name: Metered(inner[name], cache) for name in BACKENDS}


# --- the cases ------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """One question for both services, with its correct answer in label."""

    task: str
    state: Any
    questions: dict[str, Question]
    qid: str
    label: Any
    meta: dict[str, Any] = field(default_factory=dict)


def entity_key(state: dict[str, Any]) -> QuestionKey:
    mention, candidate = state["mention"], state["candidate"]
    return (mention["type"], mention["text"], mention["doc_path"], candidate["id"])


def entity_cases(truth: Truth, asks: Iterable[layer4.Ask]) -> list[Case]:
    """The same_entity asks that the truth can label, each question once."""
    found: dict[QuestionKey, Case] = {}
    for ask in asks:
        if "same_entity" not in ask.questions:
            continue
        key = entity_key(ask.state)
        type_, text, doc_path, candidate = key
        if key in found or type_ not in ENTITY_FIELDS:
            continue
        label = label_entity(truth, type_, text, doc_path, candidate)
        if label is None:
            continue
        found[key] = Case(
            "same_entity",
            ask.state,
            dict(ask.questions),
            "same_entity",
            label,
            {"type": type_, "mention": text, "candidate": candidate},
        )
    return list(found.values())


def sibling_cases(
    truth: Truth,
    graph: Graph,
    merges: list[dict[str, Any]],
    first: dict[tuple[str, str], tuple[str, str]],
    asked: set[QuestionKey],
) -> list[Case]:
    """Wrong-candidate questions the resolver never asked, built like layer 2 builds
    them."""
    pairs, _rules, _none = layer2.entity_pairs(truth, merges)
    cases = []
    for negative in layer2.sibling_negatives(truth, graph, pairs, asked):
        state = layer2.entity_state_for(
            graph,
            merges,
            first,
            index=negative.index,
            type=negative.type,
            mention=negative.mention,
            doc_path=negative.doc_path,
            candidate=negative.candidate,
        )
        cases.append(
            Case(
                "same_entity",
                state,
                {"same_entity": questions.SAME_ENTITY},
                "same_entity",
                False,
                {
                    "type": negative.type,
                    "mention": negative.mention,
                    "candidate": negative.candidate,
                },
            )
        )
    return cases


def incident_cases(
    truth: Truth,
    asks: Iterable[layer4.Ask],
    merges: list[dict[str, Any]],
    records: dict[tuple[str, str], Record],
) -> list[Case]:
    """Pairs each same_incident question with the logged decision and the true label.

    The questions and the decisions must come from the same replay. If their order
    differs, the pairing would be wrong, so a mismatch raises an error."""
    logged = [
        m
        for m in merges
        if m["type"] == "Incident"
        and m["decision"] != "rule"
        and m["candidate"] is not None
    ]
    asked = [a for a in asks if "same_incident" in a.questions]
    if len(asked) != len(logged):
        raise ValueError(
            f"{len(asked)} same_incident questions were asked but the pipeline report "
            f"has {len(logged)} logged decisions; rebuild the report from the same "
            "caches"
        )
    cases = []
    for ask, m in zip(asked, logged, strict=True):
        answer = ask.answers.get("same_incident")
        p = questions.probability(answer) if answer is not None else None
        if p is not None and m["p"] is not None and abs(p - m["p"]) > 1e-9:
            raise ValueError(
                f"a same_incident answer of {p} is paired with a logged decision of "
                f"{m['p']}; the asks and the decisions are not in the same order"
            )
        doc, _, key = m["candidate"].partition("::")
        one, two = records.get((m["doc_path"], m["mention"])), records.get((doc, key))
        label = label_incident(truth, one, two) if one and two else None
        if label is None:
            continue
        cases.append(
            Case(
                "same_incident",
                ask.state,
                dict(ask.questions),
                "same_incident",
                label,
                {
                    "doc_path": m["doc_path"],
                    "mention": m["mention"],
                    "candidate": m["candidate"],
                },
            )
        )
    return cases


def _incident_index(graph: Graph) -> dict[tuple, list[str]]:
    """The graph incidents by what a root_cause question says about them."""
    index: dict[tuple, list[str]] = {}
    for incident_id, node in graph.of_type("Incident").items():
        p = node.props
        key = (
            graph.machine_of(incident_id),
            p.get("date"),
            p.get("symptom_text"),
            p.get("resolution_text"),
            p.get("downtime_min"),
            p.get("technician"),
            p.get("shift"),
        )
        index.setdefault(key, []).append(incident_id)
    return index


def root_cause_cases(
    truth: Truth, graph: Graph, matching: Matching, asks: Iterable[layer4.Ask]
) -> list[Case]:
    """The root-cause questions of the linker, labeled with the failure modes that
    match the true cause. A question is skipped if its incident cannot be found exactly
    once or the truth has no cause for it."""
    index = _incident_index(graph)
    modes = graph.of_type("FailureMode")
    cases = []
    for ask in asks:
        if "root_cause" not in ask.questions:
            continue
        s = ask.state["incident"]
        key = (
            s["machine"],
            s["date"],
            s["symptom"],
            s["action"],
            s["downtime_min"],
            s["technician"],
            s["shift"],
        )
        found = index.get(key, [])
        truth_id = matching.truth_id(found[0]) if len(found) == 1 else None
        expected = truth.root_cause_of(truth_id) if truth_id else None
        if expected is None:
            continue
        accepted = frozenset(
            g for g, t in matching.ids.items() if t == expected and g in modes
        )
        cases.append(
            Case(
                "root_cause",
                ask.state,
                dict(ask.questions),
                "root_cause",
                accepted,
                {"incident": found[0]},
            )
        )
    return cases


def merge_cases(
    truth: Truth,
    graph: Graph,
    matching: Matching,
    report: dict[str, Any],
    extractions: list[Extraction],
    aliases: AliasTable,
    replay_decider: Decider,
) -> list[Case]:
    """Collect the merge and link questions by running the pipeline again on saved
    answers only."""
    recorder = layer4.Recorder(replay_decider, None)
    resolution = resolve(extractions, aliases, recorder)
    link(resolution, recorder)
    merges = layer2.merge_log(report)
    replayed = [m.model_dump() for m in resolution.merges]
    entities = entity_cases(truth, recorder.asks)
    asked = {entity_key(a.state) for a in recorder.asks if "same_entity" in a.questions}
    return [
        *entities,
        *sibling_cases(truth, graph, merges, replay.first_mentions(extractions), asked),
        *incident_cases(
            truth, recorder.asks, replayed, replay.records_from(extractions, aliases)
        ),
        *root_cause_cases(truth, graph, matching, recorder.asks),
    ]


# --- scoring --------------------------------------------------------------------------

Result = tuple[Case, Answer | None]


def _metrics(
    correct: list[bool], scored: list[Scored], undecided: int
) -> dict[str, Any]:
    return {
        "accuracy": ratio(sum(correct), len(correct)),
        "ece": reliability(scored)["ece"],
        "undecided": undecided,
    }


def _binary_p(answer: Answer | None) -> float | None:
    return (
        questions.probability(answer)
        if answer is not None and answer.kind == "noul"
        else None
    )


def binary_flags(results: Sequence[Result], gate: float) -> list[bool]:
    """Whether each answer of a same_entity or same_incident task is right at a gate."""
    flags = []
    for case, answer in results:
        p = _binary_p(answer)
        flags.append((p is not None and p >= gate) == case.label)
    return flags


def score_binary(results: Sequence[Result], gate: float) -> dict[str, Any]:
    """Score a yes or no task. A yes probability at or above the gate means merge."""
    ps = [_binary_p(answer) for _case, answer in results]
    scored = [
        Scored(p, case.label)
        for (case, _answer), p in zip(results, ps, strict=True)
        if p is not None
    ]
    undecided = sum(p is None for p in ps)
    return {
        "n": len(results),
        **_metrics(binary_flags(results, gate), scored, undecided),
    }


def _chosen(answer: Answer | None) -> str | None:
    return answer.choice if answer is not None and answer.kind == "choice" else None


def root_cause_flags(results: Sequence[Result]) -> list[bool]:
    return [_chosen(answer) in case.label for case, answer in results]


def score_root_cause(results: Sequence[Result]) -> dict[str, Any]:
    """Score a root-cause task. An answer is right if the chosen option is accepted."""
    scored, undecided = [], 0
    for case, answer in results:
        p = questions.probability(answer) if answer is not None else None
        undecided += p is None
        if p is not None:
            scored.append(Scored(p, _chosen(answer) in case.label))
    return {
        "n": len(results),
        **_metrics(root_cause_flags(results), scored, undecided),
    }


def verify_flags(
    rows: Sequence[tuple[bool, Answer | None]], gate: float = MIN_P
) -> list[bool]:
    """Whether each claim is judged right. A claim is kept when the support is at or
    above the gate, and that is right when it matches the judge."""
    flags = []
    for label, answer in rows:
        p = (
            questions.probability(answer)
            if answer is not None and answer.kind == "choice"
            else None
        )
        kept = p is not None and support_mass(answer) >= gate
        flags.append(kept == label)
    return flags


def score_verify(rows: Sequence[tuple[bool, Answer | None]]) -> dict[str, Any]:
    """Score a verify task. A claim is kept when the support is at or above the keep
    gate, and that is right when the judge calls the claim supported."""
    scored, undecided = [], 0
    for label, answer in rows:
        p = (
            questions.probability(answer)
            if answer is not None and answer.kind == "choice"
            else None
        )
        undecided += p is None
        if p is not None:
            scored.append(Scored(support_mass(answer), label))
    return {"n": len(rows), **_metrics(verify_flags(rows), scored, undecided)}


def triage_flags(section: dict[str, Any]) -> list[bool]:
    """The scored field checks of a layer 3 section, pooled, in item order."""
    return [
        row["correct"][name]
        for row in section["items"]
        for name in layer3.FIELDS
        if row["correct"][name] is not None
    ]


def score_triage(section: dict[str, Any]) -> dict[str, Any]:
    """Score the triage task: accuracy over all scored fields, plus calibration."""
    scored, undecided = [], 0
    for row in section["items"]:
        for name in layer3.CALIBRATED:
            p, right = row["confidence"].get(name), row["correct"][name]
            if right is not None and p is not None:
                scored.append(Scored(p, right))
        undecided += row["confidence"].get("machine") is None
    return {
        "n": section["n"],
        **_metrics(triage_flags(section), scored, undecided),
    }


# --- the sign test and the best gate --------------------------------------------------


def sign_test(first_only: int, second_only: int) -> float:
    """The exact two-sided p value of a paired sign test, using a fair coin. It is 1.0
    when the two services never differ."""
    total = first_only + second_only
    if not total:
        return 1.0
    tail = sum(comb(total, i) for i in range(min(first_only, second_only) + 1))
    return min(1.0, 2 * tail / 2**total)


def paired(one: Sequence[bool], two: Sequence[bool]) -> dict[str, Any]:
    """The sign test of two services' per-question results, which must line up."""
    jev_only = sum(a and not b for a, b in zip(one, two, strict=True))
    decisions_only = sum(b and not a for a, b in zip(one, two, strict=True))
    return {
        "jev_only": jev_only,
        "decisions_only": decisions_only,
        "n": jev_only + decisions_only,
        "p": sign_test(jev_only, decisions_only),
    }


def best_gate(
    flags_at: Callable[[float], list[bool]], shared: float
) -> tuple[dict[str, Any], list[bool]]:
    """The gate with the highest accuracy for one service. On a tie, take the gate
    nearest the shared one. The gate is picked on the same questions it is scored on, so
    the accuracy looks better than it would on new questions."""
    swept = {gate: flags_at(gate) for gate in GATE_SWEEP}
    top = max(sum(flags) for flags in swept.values())
    gate = min(
        (g for g, flags in swept.items() if sum(flags) == top),
        key=lambda g: (abs(g - shared), g),
    )
    flags = swept[gate]
    return {
        "gate": gate,
        "correct": sum(flags),
        "n": len(flags),
        "accuracy": ratio(sum(flags), len(flags)),
        "in_sample": True,
    }, flags


def _led_by(sign: dict[str, Any]) -> str | None:
    if sign["p"] >= SIGNIFICANT:
        return None
    return "jev" if sign["jev_only"] > sign["decisions_only"] else "decisions"


def verdict(
    row: dict[str, Any], sign: dict[str, Any], at_best: dict[str, Any] | None
) -> dict[str, Any]:
    """Who leads a task, if anyone. A lead must be clear at the shared gate and, for
    gated tasks, at each service's best gate too."""
    one, two = row["jev"]["accuracy"], row["decisions"]["accuracy"]
    saturated = one == 1.0 and two == 1.0
    if one is None or two is None:
        word = "no data"
    elif saturated:
        word = "saturated"
    else:
        led = _led_by(sign)
        if at_best is not None and _led_by(at_best) != led:
            led = None
        word = led or "no clear difference"
    return {
        "sign_test": sign,
        "sign_test_best_gate": at_best,
        "saturated": saturated,
        "verdict": word,
        "leader": word if word in BACKENDS else "tie",
    }


# --- the table ------------------------------------------------------------------------


def _ask_case(decider: Decider, case: Case) -> Answer | None:
    try:
        return decider.ask(case.state, case.questions, f"{PURPOSE} {case.task}").get(
            case.qid
        )
    except DecisionError:
        return None


def _priced(
    metered: Metered, before: tuple[int, float], metrics: dict[str, Any]
) -> dict[str, Any]:
    asks, usd = metered.asks - before[0], metered.usd - before[1]
    return {
        **metrics,
        "asks": asks,
        "cost_per_1000_usd": usd / asks * 1000 if asks else None,
        "usd": usd,
    }


def evaluate(
    cases: Sequence[Case],
    runs: Sequence[layer4.Run],
    judged: Sequence[layer4.Judged | None],
    triage_items: Sequence[dict[str, Any]],
    kg_path: Path,
    matching: Matching,
    deciders: dict[str, Metered],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bake-off section by asking every task of both services. The old
    latency numbers are kept from previous."""
    gates = {
        "same_entity": questions.GATES["merge_entity"],
        "same_incident": questions.GATES["merge_incident"],
        "verify": MIN_P,
    }
    tasks: dict[str, dict[str, Any]] = {task: {} for task in TASKS}
    counts: dict[str, int] = {}
    # per task and backend: the flags at the shared gate and at the backend's best gate
    shared: dict[str, dict[str, list[bool]]] = {task: {} for task in TASKS}
    best: dict[str, dict[str, list[bool]]] = {task: {} for task in TASKS}

    def sweep(task: str, name: str, flags_at: Callable[[float], list[bool]]):
        entry, flags = best_gate(flags_at, gates[task])
        tasks[task][name]["best_gate"] = entry
        best[task][name] = flags

    for task in ("same_entity", "same_incident", "root_cause"):
        subset = [c for c in cases if c.task == task]
        counts[task] = len(subset)
        for name, decider in deciders.items():
            before = decider.snapshot()
            results = [(case, _ask_case(decider, case)) for case in subset]
            if task == "root_cause":
                metrics = score_root_cause(results)
                shared[task][name] = root_cause_flags(results)
            else:
                metrics = score_binary(results, gates[task])
                shared[task][name] = binary_flags(results, gates[task])
            tasks[task][name] = {**_priced(decider, before, metrics), "best_gate": None}
            if task != "root_cause":
                sweep(task, name, lambda g, r=results: binary_flags(r, g))
    counts["triage"] = len(triage_items)
    for name, decider in deciders.items():
        before = decider.snapshot()
        found = (
            layer3.run_triage(triage_items, kg_path, decider) if triage_items else []
        )
        section = layer3.evaluate(triage_items, found, matching)
        tasks["triage"][name] = {
            **_priced(decider, before, score_triage(section)),
            "best_gate": None,
        }
        shared["triage"][name] = triage_flags(section)
    labeled = [
        (run, j)
        for run, j in zip(runs, judged, strict=True)
        if j is not None and j.labels
    ]
    for name, decider in deciders.items():
        before = decider.snapshot()
        rows = []
        for run, j in labeled:
            for ask in run.asks:
                wanted = [c for c in ask.state.get("claims", []) if c["id"] in j.labels]
                if not wanted:
                    continue
                try:
                    answers = decider.ask(ask.state, ask.questions, f"{PURPOSE} verify")
                except DecisionError:
                    answers = {}
                rows += [(j.labels[c["id"]], answers.get(c["id"])) for c in wanted]
        tasks["verify"][name] = {
            **_priced(decider, before, score_verify(rows)),
            "best_gate": None,
        }
        shared["verify"][name] = verify_flags(rows)
        sweep("verify", name, lambda g, r=rows: verify_flags(r, g))
    counts["verify"] = tasks["verify"]["jev"]["n"]
    table = {}
    for task, per_backend in tasks.items():
        row = {"n": counts[task], **per_backend}
        sign = paired(shared[task]["jev"], shared[task]["decisions"])
        at_best = (
            paired(best[task]["jev"], best[task]["decisions"]) if best[task] else None
        )
        table[task] = {**row, **verdict(row, sign, at_best)}
    latency = (previous or {}).get("latency")
    return {
        "tuned_on": TUNED_ON,
        "bias": BIAS,
        "tasks": table,
        "summary": _summary(table, deciders),
        "latency": {**latency, "caveat": LATENCY_CAVEAT} if latency else None,
    }


def _summary(
    table: dict[str, dict[str, Any]], deciders: dict[str, Metered]
) -> dict[str, Any]:
    live = {task: row for task, row in table.items() if row["n"]}
    led = {"jev": 0, "decisions": 0, "tie": 0}
    for row in live.values():
        led[row["leader"]] += 1
    means = {}
    for name in BACKENDS:
        values = [
            row[name]["accuracy"]
            for row in live.values()
            if row[name]["accuracy"] is not None
        ]
        means[name] = sum(values) / len(values) if values else None
    totals = {}
    for name in BACKENDS:
        asks = sum(row[name]["asks"] for row in table.values())
        usd = sum(row[name]["usd"] for row in table.values())
        totals[name] = {
            "asks": asks,
            "cost_per_1000_usd": usd / asks * 1000 if asks else None,
        }
    better = (
        "jev"
        if led["jev"] > led["decisions"]
        else "decisions"
        if led["decisions"] > led["jev"]
        else "no clear difference"
    )
    return {
        "tasks_led": led,
        "mean_accuracy": means,
        "better_by_accuracy": better,
        "totals": totals,
    }


# --- the timing pass ------------------------------------------------------------------


def triage_asks(
    items: Sequence[dict[str, Any]],
    kg_path: Path,
    decider: Decider,
    count: int = TIMING_ASKS,
) -> list[tuple[Any, dict[str, Question]]]:
    """Record the triage questions of the first golden items, so the timing pass can
    send them fresh. The decider used here only replays saved answers."""
    recorder = layer4.Recorder(decider, TRIAGE_PURPOSE)
    layer3.run_triage(list(items)[:count], kg_path, recorder)
    return [(ask.state, ask.questions) for ask in recorder.asks]


def live_decider(name: str, cache: DecisionCache, settings: Settings) -> Decider:
    """A live decider for one service. Raises an error if its key is not set."""
    key = settings.typesafe_api_key if name == "jev" else settings.openai_api_key
    if not key:
        raise NoDecisionBackend(f"the timing pass needs the {name} API key")
    backend = JevBackend(key) if name == "jev" else DecisionsBackend(key)
    return CachedDecider(backend, cache, settings)


def timing_pass(
    asks: Sequence[tuple[Any, dict[str, Question]]],
    make_decider: Callable[[str, DecisionCache], Decider],
    folder: Path,
    *,
    clock: Callable[[], float] = time.perf_counter,
    today: str | None = None,
) -> dict[str, Any]:
    """Time one fresh call per question for each service. Each service gets its own empty
    cache, so no answer comes from disk. A failed call stops the pass."""
    from app.evals.layer5 import percentile

    section: dict[str, Any] = {
        "measured_at": today or dt.datetime.now(dt.UTC).date().isoformat(),
        "n": len(asks),
        "what": "wall time of one fresh decider ask, in seconds, on a temporary cache",
        "caveat": LATENCY_CAVEAT,
    }
    for name in BACKENDS:
        decider = make_decider(
            name, DecisionCache(Path(folder) / f"timing_{name}.sqlite")
        )
        seconds = []
        for state, questions_ in asks:
            start = clock()
            decider.ask(state, questions_, f"{PURPOSE} timing")
            seconds.append(clock() - start)
        section[name] = {
            "p50": percentile(seconds, 50),
            "p95": percentile(seconds, 95),
            "mean": sum(seconds) / len(seconds) if seconds else None,
            "n": len(seconds),
        }
    return section
