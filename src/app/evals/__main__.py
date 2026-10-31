"""Command line entry point for the evals, started with python -m app.evals.

It runs the five scoring layers and the optional bake-off, then writes the scores and
their costs to data/evals/results/latest.json. Layers that are not run keep their old
results in that file. Most runs replay saved model answers, so a rerun costs little.

run() runs the chosen layers and writes the results file. push_results() sends the
stored scores to Langfuse. write_crews() adds the per-step accuracy, time and cost of
the two crews. time_crews() runs a small, capped sample of live calls to measure speed.
Known failures, such as a missing key or a spent budget, print one line and exit with 1.
"""

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langfuse.api.core.api_error import ApiError

from app import ledger
from app.config import KG_PATH, RAW_DIR, ROOT, TRUTH_DIR, Settings, load_settings
from app.decide import Decider, get_decider
from app.decide.base import CacheOnlyDecider, DecisionCache, NoDecisionBackend
from app.evals import (
    bakeoff,
    crews,
    crews_time,
    golden,
    layer1,
    layer2,
    layer3,
    layer4,
    layer5,
    push,
    replay,
)
from app.evals.golden import GOLDEN_DIR
from app.evals.match import match
from app.evals.truth import load_graph, load_truth
from app.ingest.report import default_report_path, ledger_mark, ledger_rows_since
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey, NoCacheInstalled

RESULTS_PATH = ROOT / "data" / "evals" / "results" / "latest.json"
LAYERS = (1, 2, 3, 4, 5)
# Layer 5 only reads the traces of layer 4, so it makes no model call.
COSTED = ("layer1", "layer2", "layer3", "layer4", "bakeoff")

LIMITS = [
    "Every golden question re-asks a recorded incident whose text is in the ingested "
    "data, so the golden results are not hold-out results: they show how the system "
    "answers questions it has already seen the records of, not how it generalises."
]

NO_KEY = (
    "OPENAI_API_KEY is not set and this run needs a model request that is not in the "
    "cache; set the key in .env and run again"
)
NO_BACKEND = (
    "a decision is needed that is not in the cache and no decision backend is set; "
    "set OPENAI_API_KEY or TYPESAFE_API_KEY in .env and run again"
)


def parse_layers(text: str) -> tuple[int, ...]:
    """The layers named by a comma separated list, sorted and without repeats."""
    try:
        found = {int(part) for part in text.split(",")}
    except ValueError:
        raise ValueError(
            f"layers must be numbers from {LAYERS}; got {text!r}"
        ) from None
    if not found or not found <= set(LAYERS):
        raise ValueError(f"layers must be numbers from {LAYERS}; got {text!r}")
    return tuple(sorted(found))


def cost_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The calls, cache hits, dollars and tokens of the ledger rows of a run."""
    by_purpose: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cache_hits": 0, "usd": 0.0}
    )
    for row in rows:
        entry = by_purpose[row["purpose"]]
        entry["calls"] += 1
        entry["cache_hits"] += int(row["cache_hit"])
        entry["usd"] += row["usd"]
    hits = sum(int(row["cache_hit"]) for row in rows)
    return {
        "calls": len(rows),
        "cache_hits": hits,
        "paid_calls": len(rows) - hits,
        "usd": round(sum(row["usd"] for row in rows), 6),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "by_purpose": {
            purpose: {**entry, "usd": round(entry["usd"], 6)}
            for purpose, entry in sorted(by_purpose.items())
        },
    }


def run(
    layers: tuple[int, ...],
    *,
    truth_dir: Path = TRUTH_DIR,
    kg_path: Path = KG_PATH,
    report_path: Path | None = None,
    raw_dir: Path = RAW_DIR,
    out_path: Path = RESULTS_PATH,
    golden_dir: Path = GOLDEN_DIR,
    settings: Settings | None = None,
    decider: Decider | None = None,
    deciders: dict[str, Any] | None = None,
    with_bakeoff: bool = False,
    with_timing: bool = False,
    today: str | None = None,
) -> dict[str, Any]:
    """Run the chosen layers, write the results file and return its content.

    Each layer that ran gets a fresh cost entry. Layers that did not run keep their old
    sections and costs. deciders maps each bake-off backend to a decider.
    """
    truth, graph = load_truth(truth_dir), load_graph(kg_path)
    results: dict[str, Any] = {}
    if Path(out_path).is_file():
        try:
            results = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except ValueError:
            results = {}
    # Keep the costs of layers that ran earlier. An old whole-file total is dropped.
    costs = {
        key: value
        for key, value in (results.get("cost") or {}).items()
        if key in COSTED and isinstance(value, dict)
    }
    wanted = set(layers)
    needs_decider = bool(wanted & {2, 3, 4, 5}) or with_bakeoff
    if needs_decider:
        settings = settings or load_settings()
        decider = decider or get_decider(settings)
    matching = match(truth, graph) if wanted & {3, 4} or with_bakeoff else None
    extractions = aliases = None
    if 1 in layers:
        mark = ledger_mark()
        results["layer1"] = layer1.evaluate(truth, graph)
        costs["layer1"] = cost_summary(ledger_rows_since(mark))
    if 2 in layers or with_bakeoff:
        extractions, aliases = replay.replay_extractions(raw_dir, settings)
        report_file = Path(report_path or default_report_path(kg_path))
        report = json.loads(report_file.read_text(encoding="utf-8"))
    if 2 in layers:
        mark = ledger_mark()
        # This replay reads only the saved decisions. A question that no longer
        # matches the cache stops the run instead of costing money.
        results["layer2"] = layer2.evaluate(
            truth,
            graph,
            report,
            extractions,
            aliases,
            decider,
            replay_decider=CacheOnlyDecider(DecisionCache()),
        )
        costs["layer2"] = cost_summary(ledger_rows_since(mark))
    triage_items = []
    if 3 in layers or with_bakeoff:
        triage_items = golden.load("triage", golden_dir)
    if 3 in layers:
        mark = ledger_mark()
        found = layer3.run_triage(triage_items, kg_path, decider)
        results["layer3"] = layer3.evaluate(triage_items, found, matching)
        costs["layer3"] = cost_summary(ledger_rows_since(mark))
    runs: list[layer4.Run] = []
    judged: list[layer4.Judged | None] = []
    if wanted & {4, 5} or with_bakeoff:
        mark = ledger_mark()
        answer_items = golden.load("answers", golden_dir)
        runs = layer4.run_answers(answer_items, kg_path, settings, decider)
        judges = 4 in layers or with_bakeoff
        judged = [layer4.judge_run(r, settings) if judges else None for r in runs]
        if 4 in layers:
            results["layer4"] = layer4.evaluate(truth, runs, judged, matching)
            costs["layer4"] = cost_summary(ledger_rows_since(mark))
        if 5 in layers:
            results["layer5"] = layer5.evaluate(runs, results.get("layer5"), today)
    if with_bakeoff:
        mark = ledger_mark()
        deciders = deciders or bakeoff.make_deciders(settings)
        cases = bakeoff.merge_cases(
            truth,
            graph,
            matching,
            report,
            extractions,
            aliases,
            CacheOnlyDecider(DecisionCache()),
        )
        section = bakeoff.evaluate(
            cases,
            runs,
            judged,
            triage_items,
            kg_path,
            matching,
            deciders,
            previous=results.get("bakeoff"),
        )
        if with_timing:
            section["latency"] = _timing(
                triage_items, kg_path, deciders, settings, today
            )
        results["bakeoff"] = section
        costs["bakeoff"] = cost_summary(ledger_rows_since(mark))
    if wanted & {3, 4, 5} or with_bakeoff:
        results["limits"] = LIMITS
    results["cost"] = dict(sorted(costs.items()))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return results


def _timing(
    triage_items: list[dict[str, Any]],
    kg_path: Path,
    deciders: dict[str, Any],
    settings: Settings,
    today: str | None,
) -> dict[str, Any]:
    """Time fresh calls to each backend, using a temporary cache that is deleted after."""
    asks = bakeoff.triage_asks(triage_items, kg_path, deciders["jev"])
    with tempfile.TemporaryDirectory(prefix="evals-timing-") as folder:
        return bakeoff.timing_pass(
            asks,
            lambda name, cache: bakeoff.live_decider(name, cache, settings),
            Path(folder),
            today=today,
        )


def langfuse_client(settings: Settings) -> Any:
    """The Langfuse client for the keys and host in the settings."""
    from langfuse import Langfuse

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_host,
    )


NO_LANGFUSE = (
    "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set; set them in .env to push"
)


def push_results(settings: Settings) -> int:
    """Push the stored layer 3 and 4 results with the golden sets. Runs nothing else."""
    if not settings.has_langfuse:
        return fail(NO_LANGFUSE)
    try:
        results = json.loads(Path(RESULTS_PATH).read_text(encoding="utf-8"))
        summary = push.push(
            results,
            golden.load("triage", GOLDEN_DIR),
            golden.load("answers", GOLDEN_DIR),
            langfuse_client(settings),
            run_name=datetime.now(UTC).strftime("evals-%Y%m%dT%H%M%SZ"),
        )
    except ApiError as error:
        return fail(
            f"Langfuse refused the push (HTTP {error.status_code}); "
            "check the keys and host"
        )
    except (OSError, ValueError) as error:
        return fail(str(error))
    print(
        f"pushed {summary['datasets']} datasets, {summary['items']} items and "
        f"{summary['scores']} scores"
    )
    return 0


def write_crews() -> int:
    """Write the crews block into the results file. Makes no model call."""
    try:
        block = crews.write_block(
            results=RESULTS_PATH,
            report=default_report_path(KG_PATH),
            kg=KG_PATH,
            ledger=ledger.LEDGER_PATH,
            timing=crews_time.TIMING_PATH,
        )
    except KeyError as error:
        return fail(f"the results file has no {error}; run the layers first")
    except (OSError, ValueError) as error:
        return fail(str(error))
    steps = len(block["ingestion"]["steps"]) + len(block["troubleshooting"]["steps"])
    print(f"wrote the crews block ({steps} steps) to {RESULTS_PATH.relative_to(ROOT)}")
    return 0


NO_CAP = "the timed run is not started: {reason}"


def time_crews() -> int:
    """Run the timed sample of the crews, live, under the cap, and write the timing file."""
    spent = ledger.spent_usd()
    # The budget of this process only: the ledger total now plus the cap of the run.
    # Limit this process to what is already spent plus the cap for this run.
    os.environ["BUDGET_USD"] = repr(crews_time.cap_for(spent))
    try:
        result = crews_time.run_timing(load_settings(), spent=spent)
    except crews_time.CapNotEnforced as error:
        return fail(NO_CAP.format(reason=error))
    except RuntimeError as error:
        if isinstance(error, (BudgetExceeded, MissingApiKey, NoDecisionBackend)):
            raise
        return fail(NO_CAP.format(reason=error))
    print("\n".join(crews_time.summary(result)))
    print(f"wrote {crews_time.TIMING_PATH.relative_to(ROOT)}")
    return 0


def fail(message: str) -> int:
    print(" ".join(message.split()), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evals")
    parser.add_argument("--layers", default="1,2", help="comma separated, from 1 to 5")
    parser.add_argument(
        "--bakeoff",
        action="store_true",
        help="also run the JEV against Decisions bake-off",
    )
    parser.add_argument(
        "--time",
        action="store_true",
        help="with --bakeoff, run the timing pass (fresh calls on a temporary cache)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="push the golden sets and stored scores to Langfuse (runs nothing else)",
    )
    parser.add_argument(
        "--crews",
        action="store_true",
        help="write the crews block of the results file (no model call, no key)",
    )
    parser.add_argument(
        "--time-crews",
        action="store_true",
        help="run the timed sample of the two crews: a few real calls under a $0.10 cap",
    )
    args = parser.parse_args(argv)
    if args.crews:
        return write_crews()
    if args.time_crews:
        try:
            return time_crews()
        except MissingApiKey:
            return fail(NO_KEY)
        except NoDecisionBackend:
            return fail(NO_BACKEND)
        except BudgetExceeded as error:
            return fail(f"{error}; the timed run stopped at its cap")
        except (ValueError, NoCacheInstalled) as error:
            return fail(str(error))
    if args.push:
        try:
            return push_results(load_settings())
        except ValueError as error:
            return fail(str(error))
    try:
        layers = parse_layers(args.layers)
        results = run(
            layers, with_bakeoff=args.bakeoff, with_timing=args.time and args.bakeoff
        )
    except ValueError as error:
        return fail(str(error))
    except MissingApiKey:
        return fail(NO_KEY)
    except NoDecisionBackend:
        return fail(NO_BACKEND)
    except BudgetExceeded as error:
        return fail(f"{error}; raise BUDGET_USD in .env and run again")
    except NoCacheInstalled as error:
        return fail(str(error))
    ran = [f"layer{n}" for n in layers] + (["bakeoff"] if args.bakeoff else [])
    ran = [key for key in ran if key in results["cost"]]
    print(
        f"layers: {', '.join(map(str, layers))}; wrote {RESULTS_PATH.relative_to(ROOT)}"
    )
    for key in ran:
        cost = results["cost"][key]
        label = key.replace("layer", "layer ")
        print(
            f"{label} model calls: {cost['calls']} ({cost['cache_hits']} from the "
            f"cache, {cost['paid_calls']} paid), cost ${cost['usd']:.6f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
