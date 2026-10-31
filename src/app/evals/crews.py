"""Builds the crews block of the results file: accuracy, time and cost for each step of
the ingestion crew and the troubleshooting crew.

Nothing in the block is typed in by hand. Accuracy comes from the other scoring layers
in the results file, counts come from the graph and the pipeline report, costs come from
the spend ledger, and times come from the timing file made by the timed crew run. The
ledger is not committed, so the block keeps the average costs it used and can be rebuilt
without the ledger.

build() assembles the block from loaded data. read_ledger() and ledger_means() read the
spend ledger and average it. write_block() reads all the files, builds the block and
writes it into the results file.
"""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from app.evals.layer5 import percentile
from app.ingest.router import FOLDER_TYPES

# With fewer samples than this, the 95th percentile is just the largest value, so it is left out.
P95_FROM = 20
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
NO_MODEL = "code, no model call"
# The ledger purposes the block uses. Only their averages are kept.
PURPOSES = (
    "extract handover",
    "extract note",
    "extract manual",
    "merge Part",
    "merge Incident",
    "link root cause",
)

ROUTER_NOTE = "Scored against the folder each file sits in, which is the plant's own label for it."
TEXT_NOTE = (
    "Alarm codes and parts only. Machine mentions are left out because routine lines "
    "name machines that had no breakdown, so they are not facts to find."
)
MANUAL_AGENT_NOTE = (
    "No score of its own: its passages are scored through the synthesizer's citations, "
    "which cite them."
)
CRITICAL_PATH_NOTE = (
    "The graph agent and the manual agent run in parallel, so the steps do not add up "
    "to the time of an answer."
)


# --- small helpers --------------------------------------------------------------------


def _day(text: str) -> str:
    return str(text)[:10]


def fmt_date(day: str) -> str:
    """An ISO date as '28 Oct 2026'."""
    when = date.fromisoformat(_day(day))
    return f"{when.day} {MONTHS[when.month - 1]} {when.year}"


def _span(first: str, last: str) -> str:
    return (
        fmt_date(first) if first == last else f"{fmt_date(first)} to {fmt_date(last)}"
    )


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _need(table: dict[str, Any], key: str, where: str = "the timing file") -> Any:
    if key not in table:
        raise ValueError(f"{where} has no timing for {key}; run --time-crews again")
    return table[key]


def _seconds(entries: Iterable[dict[str, Any]]) -> list[float]:
    return [float(entry["seconds"]) for entry in entries]


# --- the ledger -------------------------------------------------------------------------


def read_ledger(path: Path) -> list[dict[str, Any]] | None:
    """The rows of the ledger, read-only; None when the file is missing."""
    path = Path(path)
    if not path.is_file():
        return None
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    ) as conn:
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY id")]
        except sqlite3.OperationalError:
            return []


def ledger_means(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The average cost of the paid calls for each purpose, with the number of calls and
    the first and last dates."""
    paid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row["cache_hit"] and row["usd"] > 0:
            paid[row["purpose"]].append(row)
    return {
        purpose: {
            "usd": sum(r["usd"] for r in found) / len(found),
            "n": len(found),
            "first": min(_day(r["ts"]) for r in found),
            "last": max(_day(r["ts"]) for r in found),
        }
        for purpose, found in sorted(paid.items())
    }


def pooled(means: dict[str, dict[str, Any]], *purposes: str) -> dict[str, Any]:
    """The mean over the paid calls of several purposes together."""
    found = [means[p] for p in purposes if p in means]
    if not found:
        raise ValueError(
            f"the ledger has no paid call for {' or '.join(repr(p) for p in purposes)}"
        )
    calls = sum(m["n"] for m in found)
    return {
        "usd": sum(m["usd"] * m["n"] for m in found) / calls,
        "n": calls,
        "first": min(m["first"] for m in found),
        "last": max(m["last"] for m in found),
    }


def _ledger_cost(mean: dict[str, Any], per: str) -> dict[str, Any]:
    return {
        "usd": mean["usd"],
        "per": per,
        "n": mean["n"],
        "source": f"ledger mean of {_count(mean['n'], 'paid call')}, "
        f"{_span(mean['first'], mean['last'])}",
    }


def _free(per: str) -> dict[str, Any]:
    return {"usd": 0.0, "per": per, "n": None, "source": NO_MODEL}


# --- accuracy ---------------------------------------------------------------------------


def _fraction(
    label: str, k: int, n: int, precision: float | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"label": label, "k": k, "n": n}
    if precision is not None:
        out["precision"] = precision
    return out


def _found(label: str, sections: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """How many expected items were found, and what share of the found items were
    right."""
    tp = sum(s["tp"] for s in sections)
    fn = sum(s["fn"] for s in sections)
    fp = sum(s["fp"] for s in sections)
    return _fraction(label, tp, tp + fn, tp / (tp + fp) if tp + fp else None)


def _gate(label: str, section: dict[str, Any]) -> dict[str, Any]:
    return _found(label, [section])


def _documents(
    results: dict[str, Any], types: Iterable[str], categories: Iterable[str] | None
):
    """The scores of the given document types. With no categories, use all of them."""
    documents = results["layer1"]["documents"]
    found = []
    for doc_type in types:
        table = documents[doc_type]
        names = (
            categories
            if categories is not None
            else [k for k, v in table.items() if isinstance(v, dict)]
        )
        found += [table[name] for name in names]
    return found


def router_agreement(documents: Iterable[tuple[str, str]]) -> tuple[int, int]:
    """How many documents have the type of their folder, and how many there are."""
    rows = list(documents)
    right = sum(
        1 for path, doc_type in rows if FOLDER_TYPES.get(path.split("/")[0]) == doc_type
    )
    return right, len(rows)


# --- time -------------------------------------------------------------------------------


def _spread(
    values: Sequence[float], per: str, source: str, step_s: float | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"p50_s": round(percentile(values, 50), 6)}
    if len(values) >= P95_FROM:
        out["p95_s"] = round(percentile(values, 95), 6)
    out |= {"per": per, "n": len(values), "source": source}
    if step_s is not None:
        out["step_s"] = round(step_s, 6)
    return out


# --- the ingestion crew -----------------------------------------------------------------


def _step(step_id, name, model, does, accuracy, cost, time, **extra) -> dict[str, Any]:
    return {
        "id": step_id,
        "name": name,
        "model": model,
        "does": does,
        "accuracy": accuracy,
        "cost": cost,
        "time": time,
        **extra,
    }


def _questions(timed: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    return timed["questions"], int(timed.get("cached", 0))


def _jev_step(timed, per, source, mean, label) -> tuple[dict[str, Any], dict[str, Any]]:
    """The cost and time of a step that asks JEV questions: average per question, plus
    the whole step in the timed run."""
    questions, cached = _questions(timed)
    if not questions:
        raise ValueError(
            "the timing file holds no live JEV question for this step; "
            "run --time-crews again"
        )
    cost = _ledger_cost(mean, per) | {
        "step_usd": sum(q["usd"] for q in questions),
        "step_questions": len(questions),
        "step_cached": cached,
    }
    time = _spread(
        _seconds(questions),
        per,
        f"{source}, {_count(len(questions), label)}",
        timed["seconds"],
    )
    return cost, time


def _ingestion(results, report, documents, means, timing) -> dict[str, Any]:
    ran = timing["measured_at"]
    source = f"timed run, {fmt_date(ran)}"
    ingest = _need(timing, "ingestion")

    right, total = router_agreement(documents)
    router_files = _need(ingest, "router")["files"]
    router = _step(
        "router",
        "Router",
        "code",
        "Decides what kind of file each one is, from its extension, its first lines and its folder.",
        _fraction("files read as the type their folder says", right, total),
        _free("file"),
        _spread(
            _seconds(router_files),
            "file",
            f"{source}, {_count(len(router_files), 'file')}",
            sum(_seconds(router_files)),
        ),
        note=ROUTER_NOTE,
    )

    parser_files = _need(ingest, "parsers")["files"]
    parsers = _step(
        "parsers",
        "Parsers",
        "code",
        "Read the machine logs, the spares lists and the maintenance export with fixed rules.",
        _found(
            "alarm codes, machines and parts found",
            _documents(
                results,
                ("log", "inventory", "maintenance_export"),
                ("alarm_codes", "assets", "parts"),
            ),
        ),
        _free("file"),
        _spread(
            _seconds(parser_files),
            "file",
            f"{source}, {_count(len(parser_files), 'file')}",
            sum(_seconds(parser_files)),
        ),
    )

    text_files = _need(ingest, "text_reader")["files"]
    text_cost = _ledger_cost(pooled(means, "extract handover", "extract note"), "note")
    text_cost["by_kind"] = {
        "handover": means["extract handover"]["usd"],
        "note": means["extract note"]["usd"],
    }
    text = _step(
        "text_reader",
        "Text reader",
        "Sol",
        "Reads shift handovers and loose notes into breakdown records.",
        _found(
            "alarm codes and parts found",
            _documents(
                results, ("handover", "maintenance_note"), ("alarm_codes", "parts")
            ),
        ),
        text_cost,
        _spread(
            _seconds(text_files), "note", f"{source}, {_count(len(text_files), 'note')}"
        ),
        note=TEXT_NOTE,
    )

    manual_files = _need(ingest, "manual_reader")["files"]
    manual = _step(
        "manual_reader",
        "Manual reader",
        "Sol",
        "Reads a vendor PDF manual into alarm codes, failure modes, procedures and parts.",
        _found(
            "alarm codes, machines, failure modes, parts and procedures found",
            _documents(results, ("manual",), None),
        ),
        _ledger_cost(pooled(means, "extract manual"), "manual"),
        _spread(
            _seconds(manual_files),
            "manual",
            f"{source}, {_count(len(manual_files), 'manual')}",
        ),
    )

    layer2 = results["layer2"]
    merge_cost, merge_time = _jev_step(
        _need(ingest, "resolver"),
        "JEV question",
        source,
        pooled(means, "merge Part", "merge Incident"),
        "JEV question",
    )
    resolver = _step(
        "resolver",
        "Resolver",
        "rules + JEV",
        "Merges the same machine, part or breakdown named in different files: rules and "
        "fuzzy matching first, JEV for the doubtful ones.",
        _gate("same-entity merges found at the gate", layer2["same_entity"]["at_gate"]),
        merge_cost,
        merge_time,
        also=[
            _gate(
                "same-incident merges found at the gate",
                layer2["same_incident"]["decision_level"]["at_gate"],
            )
        ],
    )

    link_cost, link_time = _jev_step(
        _need(ingest, "linker"),
        "JEV question",
        source,
        pooled(means, "link root cause"),
        "JEV question",
    )
    linker = _step(
        "linker",
        "Linker",
        "JEV",
        "Picks the root cause of each breakdown from the listed causes.",
        _gate("root causes linked right at the gate", layer2["root_cause"]["at_gate"]),
        link_cost,
        link_time,
    )

    totals = report["totals"]
    loaded = report["run"]["loaded"]
    validate_s = float(_need(ingest, "validator")["seconds"])
    load_s = float(_need(ingest, "loader")["seconds"])
    both = validate_s + load_s
    validate_load = _step(
        "validate_load",
        "Validator and loader",
        "code",
        "Checks every node and link against the ontology, then writes the graph and the text chunks.",
        None,
        _free("run"),
        _spread([both], "run", f"{source}, 1 run", both)
        | {"parts": {"validate_s": round(validate_s, 6), "load_s": round(load_s, 6)}},
        outcome={
            "errors": totals["flags"]["error"],
            "warnings": totals["flags"]["warning"],
            "nodes": loaded["nodes"],
            "links": loaded["edges"],
        },
        note=f"No accuracy figure: the validator raised {totals['flags']['error']} errors "
        f"and {totals['flags']['warning']} warnings, and the loader wrote "
        f"{loaded['nodes']} nodes and {loaded['edges']} links.",
    )

    return {
        "measured_at": {
            "timed_run": ran,
            "ledger_from": min(m["first"] for m in means.values()),
            "ledger_to": max(m["last"] for m in means.values()),
        },
        "steps": [router, parsers, text, manual, resolver, linker, validate_load],
        "graph": _graph(report, means, ingest, ran),
    }


def _graph(report, means, ingest, ran) -> dict[str, Any]:
    """The graph totals, and what a fresh ingest of the same files would cost."""
    loaded, totals = report["run"]["loaded"], report["totals"]
    kinds = defaultdict(int)
    for entry in report["files"]:
        kinds[entry["doc_type"], entry["extractor"]] += 1
    handovers = kinds["handover", "sol-text"]
    notes = kinds["maintenance_note", "sol-text"]
    manuals = kinds["manual", "sol-manual"]
    asked = {
        purpose: entry["calls"]
        for purpose, entry in totals["cost"]["by_purpose"].items()
        if not purpose.startswith("extract")
    }
    usd = (
        handovers * means["extract handover"]["usd"]
        + notes * means["extract note"]["usd"]
        + manuals * means["extract manual"]["usd"]
        + sum(calls * pooled(means, purpose)["usd"] for purpose, calls in asked.items())
    )
    return {
        "documents": loaded["documents"],
        "nodes": loaded["nodes"],
        "links": loaded["edges"],
        "chunks": loaded["chunks"],
        "evidence": loaded["evidence"],
        "review_items": loaded["review"],
        "validator_errors": totals["flags"]["error"],
        "validator_warnings": totals["flags"]["warning"],
        "pipeline_s": ingest["pipeline_seconds"],
        "pipeline_note": f"Resolve, link, validate and load over all files on {fmt_date(ran)}, "
        "with the extractions replayed from the cache and the JEV questions asked live.",
        "fresh_ingest_estimate": {
            "usd": usd,
            "basis": f"{_count(handovers, 'handover')}, {_count(notes, 'note')} and "
            f"{_count(manuals, 'manual')} read by Sol, and {sum(asked.values())} merge and "
            "link questions put to JEV, each at the ledger mean per paid call",
        },
    }


# --- the troubleshooting crew ---------------------------------------------------------------


def _answer_cost(
    answers: list[dict[str, Any]], node: str, source: str
) -> dict[str, Any]:
    costs = [a["nodes"][node]["usd"] for a in answers if node in a["nodes"]]
    if not costs:
        raise ValueError(
            f"the timing file has no answer with a {node} node; run --time-crews again"
        )
    return {
        "usd": sum(costs) / len(costs),
        "per": "answer",
        "n": len(costs),
        "source": source,
    }


def _troubleshooting(results, timing) -> dict[str, Any]:
    live = results["layer5"]["live"]
    layer3, layer4 = results["layer3"], results["layer4"]
    live_source = f"live answers, {fmt_date(live['measured_at'])}"
    answers = _need(timing, "answers")
    timed_source = f"timed run, {fmt_date(timing['measured_at'])}, {_count(len(answers), 'answer')}"
    nodes = live["latency"]["nodes"]

    def time(name):
        node = nodes[name]
        return {
            "p50_s": node["p50"],
            "p95_s": node["p95"],
            "per": "answer",
            "n": node["n"],
            "source": live_source,
        }

    def cost(name):
        return _answer_cost(answers, name, timed_source)

    judge = layer4["judge"]
    lockout = layer4["lockout"]
    over = lockout["over_warning"]
    steps = [
        _step(
            "triage",
            "Triage",
            "JEV",
            "Picks the machine, the alarm, the root cause, the intent and the safety flag.",
            _fraction(
                "questions with all five fields right",
                layer3["all_fields"]["correct"],
                layer3["all_fields"]["n"],
            ),
            cost("triage"),
            time("triage"),
            also=[
                _fraction(
                    "all five fields right, of the questions it acted on",
                    layer3["on_act"]["all_fields"]["correct"],
                    layer3["on_act"]["all_fields"]["n"],
                )
            ],
        ),
        _step(
            "graph_agent",
            "Graph agent",
            "Terra",
            "Reads the graph with six read-only tools: causes, fixes, parts and past breakdowns.",
            _fraction(
                "questions with the right root cause first",
                layer4["root_cause"]["top1"]["correct"],
                layer4["root_cause"]["top1"]["n"],
            ),
            cost("graph_agent"),
            time("graph_agent"),
        ),
        _step(
            "manual_agent",
            "Manual agent",
            "Terra",
            "Searches the manuals by keyword and by vector for the passages that apply.",
            None,
            cost("manual_agent"),
            time("manual_agent"),
            note=MANUAL_AGENT_NOTE,
        ),
        _step(
            "synthesize",
            "Synthesizer",
            "Sol",
            "Writes the summary and the claims, each with the passages it cites.",
            _fraction(
                "kept claims the judge finds supported",
                judge["citation_precision"]["supported"],
                judge["citation_precision"]["kept"],
            ),
            cost("synthesize"),
            time("synthesize"),
        ),
        _step(
            "verify",
            "Verifier",
            "JEV",
            "Puts one question to JEV for each claim against the passages it cites.",
            _fraction(
                "verdicts on kept claims that agree with the judge",
                judge["verifier_agreement"]["agree"],
                judge["verifier_agreement"]["n"],
            ),
            cost("verify"),
            time("verify"),
        ),
        _step(
            "assemble",
            "Assemble with the lockout block",
            "code",
            "Builds the card from the graph in code and adds the lockout block from the "
            "machine's own manual.",
            _fraction(
                "lockout blocks shown where the fix needs isolation",
                lockout["caught"],
                lockout["n"],
            ),
            _free("answer") | {"n": len(answers), "source": timed_source},
            time("assemble"),
            also=[
                _fraction(
                    "questions with the right fix shown",
                    layer4["fix"]["shown"]["correct"],
                    layer4["fix"]["shown"]["n"],
                ),
                _fraction(
                    "questions with the right parts, quantities and bins",
                    layer4["parts"]["correct"],
                    layer4["parts"]["n"],
                ),
            ],
            note=f"The lockout block was also shown on {over['warned']} of "
            f"{_count(over['negatives'], 'question')} whose fix needs none.",
        ),
    ]

    totals = [sum(n["usd"] for n in a["nodes"].values()) for a in answers]
    cost_live = live["cost_per_answer"]
    traps = layer4["traps_passed"]
    whole = {
        "id": "answer",
        "name": "Whole answer",
        "accuracy": _fraction(
            "trap questions handled right", traps["passed"], traps["n"]
        ),
        "cost": {
            "usd": cost_live["mean"],
            "p50_usd": cost_live["p50"],
            "p95_usd": cost_live["p95"],
            "per": "answer",
            "n": live["answers"],
            "source": live_source,
        },
        "time": {
            "p50_s": live["latency"]["answer"]["p50"],
            "p95_s": live["latency"]["answer"]["p95"],
            "per": "answer",
            "n": live["latency"]["answer"]["n"],
            "source": live_source,
        },
        "timed_run": {
            "answers": len(answers),
            "cost_usd": sum(totals) / len(totals),
            "p50_s": percentile(_seconds(answers), 50),
            "source": timed_source,
        },
    }
    if results["layer5"].get("cost_note"):
        whole["note"] = results["layer5"]["cost_note"]
    return {
        "measured_at": {
            "live_answers": live["measured_at"],
            "timed_run": timing["measured_at"],
        },
        "steps": steps,
        "answer": whole,
        "note": CRITICAL_PATH_NOTE,
    }


# --- the block --------------------------------------------------------------------------------


def build(
    results: dict[str, Any],
    report: dict[str, Any],
    documents: Iterable[tuple[str, str]],
    ledger: dict[str, dict[str, Any]],
    timing: dict[str, Any],
) -> dict[str, Any]:
    """Build the crews block from the results file, the pipeline report, the documents in
    the graph, the ledger averages and the timing file. The inputs are not changed."""
    return {
        "ledger": {p: ledger[p] for p in PURPOSES if p in ledger},
        "ingestion": _ingestion(results, report, list(documents), ledger, timing),
        "troubleshooting": _troubleshooting(results, timing),
    }


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_block(
    *,
    results: Path,
    report: Path,
    kg: Path,
    ledger: Path,
    timing: Path,
) -> dict[str, Any]:
    """Build the block from the files and write it into the results file. Other sections
    stay as they are. Without a ledger, the averages kept in the last block are used.
    Raises an error, and writes nothing, if the timing file or the averages are missing."""
    results_path = Path(results)
    if not Path(timing).is_file():
        raise ValueError(
            f"{timing} is not here; run python -m app.evals --time-crews first"
        )
    data = json.loads(results_path.read_text(encoding="utf-8"))
    rows = read_ledger(ledger)
    if rows is not None:
        means = ledger_means(rows)
    elif data.get("crews", {}).get("ledger"):
        means = data["crews"]["ledger"]
    else:
        raise ValueError(
            f"there is no spend ledger at {ledger} and the results hold no ledger means yet"
        )
    with closing(
        sqlite3.connect(f"{Path(kg).resolve().as_uri()}?mode=ro", uri=True)
    ) as conn:
        documents = conn.execute(
            "SELECT path, doc_type FROM documents ORDER BY path"
        ).fetchall()
    block = build(
        data,
        json.loads(Path(report).read_text(encoding="utf-8")),
        documents,
        means,
        json.loads(Path(timing).read_text(encoding="utf-8")),
    )
    data["crews"] = block
    results_path.write_text(_dump(data), encoding="utf-8", newline="\n")
    return block
