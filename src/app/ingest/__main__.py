"""Command line entry point for the ingestion.

Without arguments it rebuilds the graph from every raw file. With --file it adds one
file to the existing graph. Either way it prints what was added and what the run cost.
A known problem, such as a missing key or a spent budget, prints one line and exits
with 1.
"""

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from app.config import KG_PATH, RAW_DIR, load_settings
from app.decide.base import NoDecisionBackend
from app.ingest import graph
from app.ingest.router import UnknownFile
from app.ledger import BudgetExceeded
from app.llm import MissingApiKey, NoCacheInstalled

NO_KEY = (
    "OPENAI_API_KEY is not set and this run needs a model request that is not in the "
    "cache; set the key in .env and run again"
)
NO_BACKEND = (
    "a decision is needed that is not in the cache and no decision backend is set; "
    "set OPENAI_API_KEY or TYPESAFE_API_KEY in .env and run again"
)


def summary(report: dict[str, Any]) -> list[str]:
    """The lines to print for a finished run."""
    totals, run = report["totals"], report["run"]
    cost = totals["cost"]

    def counts(table: dict[str, int]) -> str:
        return ", ".join(f"{key} {value}" for key, value in table.items()) or "none"

    return [
        f"mode: {run['mode']}, {len(report['files'])} files, {run['seconds']} s",
        f"nodes: {counts(totals['nodes'])}",
        f"edges: {counts(totals['edges'])}",
        f"review items: {counts(totals['review'])}",
        f"flags: {totals['flags']['error']} errors, {totals['flags']['warning']} warnings",
        f"model calls: {cost['calls']} ({cost['cache_hits']} from the cache, "
        f"{cost['paid_calls']} paid), cost ${cost['usd']:.4f}",
    ]


def fail(message: str) -> int:
    """Print the message as one line on stderr and return the failure exit code."""
    print(" ".join(message.split()), file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingest")
    parser.add_argument(
        "--file",
        type=Path,
        metavar="PATH",
        help="ingest this one file into the existing graph instead of rebuilding it",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    started = time.monotonic()
    try:
        if args.file is None:
            state = graph.run_full(RAW_DIR, KG_PATH, settings)
        else:
            state = graph.run_file(args.file, KG_PATH, settings)
    except MissingApiKey:
        return fail(NO_KEY)
    except NoDecisionBackend:
        return fail(NO_BACKEND)
    except BudgetExceeded as error:
        return fail(f"{error}; raise BUDGET_USD in .env and run again")
    except NoCacheInstalled as error:
        return fail(str(error))
    except (FileNotFoundError, UnknownFile) as error:
        return fail(str(error))
    if settings.has_langfuse:
        from langfuse import get_client

        get_client().flush()
    print("\n".join(summary(state["report"])))
    print(f"wall time: {time.monotonic() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
