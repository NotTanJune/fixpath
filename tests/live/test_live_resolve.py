"""Live test for the resolver and the linker. It makes real, paid model calls.

It runs only when the RUN_LIVE setting is 1. Every extractor reads every raw file, then
the resolver and linker run with the real decider. Unlike the other live tests, this one
writes to the committed caches on purpose, so a later full pipeline run replays it for
free. It also writes to the real spend ledger, and stops when the ledger passes 5
dollars. The tests print what they found and check that the result is consistent. They
never compare it with the hidden answer key.
"""

import os
import sqlite3
from collections import Counter
from contextlib import closing

import pytest
from langchain_core.globals import get_llm_cache, set_llm_cache

from app import ledger, llm_cache
from app.config import RAW_DIR, load_settings
from app.decide import get_decider
from app.ingest import parsers, questions, router
from app.ingest.link import link
from app.ingest.resolve import resolve
from app.ingest.types import Extraction, Resolution
from app.ontology import validate_id

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="set RUN_LIVE=1 to call the real services",
)

STOP_USD = 5.0
ALIASES = parsers.build_alias_table(RAW_DIR / "inventory" / "spares_2026-10.xlsx")

STATE: dict = {}  # what one test hands to the next: extractions, then resolutions
FIRST_ROW: list[int] = []  # the newest ledger id before the first live call


def say(capsys, text: str = "") -> None:
    """Print a line so the run shows it, even though pytest hides output."""
    with capsys.disabled():
        print(f"  {text}")


def guard_budget() -> None:
    spent = ledger.spent_usd()
    if spent > STOP_USD:
        pytest.fail(f"the ledger is at ${spent:.4f}, past the ${STOP_USD:.2f} stop")


class GuardedDecider:
    """The real decider, with a spending check before every question."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.asked = 0

    def ask(self, state, questions, purpose):
        guard_budget()
        self.asked += 1
        return self.inner.ask(state, questions, purpose)


@pytest.fixture(scope="module", autouse=True)
def committed_cache():
    """Switch on the model cache at its default path, the committed one."""
    previous = get_llm_cache()
    llm_cache.enable_cache()
    ledger.spent_usd()  # make sure the ledger file exists
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        FIRST_ROW.append(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()[0]
        )
    yield
    set_llm_cache(previous)


def ledger_rows() -> list[sqlite3.Row]:
    with closing(sqlite3.connect(ledger.LEDGER_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM calls WHERE id > ? ORDER BY id", (FIRST_ROW[0],)
        ).fetchall()


def table(counter: Counter) -> str:
    return ", ".join(f"{key}={count}" for key, count in sorted(counter.items()))


# --- Extraction ---


def test_every_file_is_extracted(capsys):
    guard_budget()
    paths = router.ordered(p for p in RAW_DIR.rglob("*") if p.is_file())
    extractions: list[Extraction] = []
    for path in paths:
        extractor = router.extractor_for(path)
        assert extractor is not None, f"no extractor for {path}"
        extractions.append(extractor(path, ALIASES))
        guard_budget()
    STATE["extractions"] = extractions

    by_type = Counter()
    by_doc = Counter()
    for extraction in extractions:
        by_doc[extraction.doc_type] += 1
        for node in extraction.nodes:
            by_type[node.ref.type] += 1
    nodes_by_doc = Counter()
    for extraction in extractions:
        nodes_by_doc[extraction.doc_type] += len(extraction.nodes)
    say(capsys)
    say(capsys, f"{len(extractions)} files: {table(by_doc)}")
    say(capsys, f"candidate nodes by type: {table(by_type)}")
    say(capsys, f"candidate nodes by doc type: {table(nodes_by_doc)}")
    assert len(extractions) == len(paths)


# --- Resolution ---


def test_resolve(capsys):
    guard_budget()
    decider = GuardedDecider(get_decider(load_settings()))
    resolution = resolve(STATE["extractions"], ALIASES, decider)
    STATE["resolved"] = resolution
    STATE["asked"] = decider.asked

    node_ids = {n.id for n in resolution.nodes}
    assert len(node_ids) == len(resolution.nodes), "a node id is used twice"
    assert all(e.src in node_ids and e.dst in node_ids for e in resolution.edges)
    for node in resolution.nodes:
        assert validate_id(node.type, node.id), node.id
        assert node.sources, node.id
    keys = [(e.src, e.rel, e.dst) for e in resolution.edges]
    assert keys == sorted(keys) and len(keys) == len(set(keys))
    assert Resolution.model_validate_json(resolution.model_dump_json()) == resolution

    say(capsys)
    say(capsys, f"{decider.asked} questions asked")
    say(capsys, f"nodes by type: {table(Counter(n.type for n in resolution.nodes))}")
    say(capsys, f"edges by relation: {table(Counter(e.rel for e in resolution.edges))}")
    decisions = Counter((m.type, m.decision) for m in resolution.merges)
    say(capsys, "merge decisions by type and decision:")
    for (node_type, decision), count in sorted(decisions.items()):
        say(capsys, f"  {node_type} {decision}: {count}")
    parts = sorted(
        (m for m in resolution.merges if m.type == "Part" and m.decision == "merge"),
        key=lambda m: m.p,
    )
    say(capsys, "the 10 lowest-p part merges:")
    for m in parts[:10]:
        say(capsys, f"  p={m.p:.3f} {m.mention!r} -> {m.candidate} ({m.doc_path})")
    incidents = [n for n in resolution.nodes if n.type == "Incident"]
    say(capsys, f"incidents after clustering: {len(incidents)}")
    say(
        capsys,
        f"review items by kind: {table(Counter(i.kind for i in resolution.review))}",
    )
    reasons = Counter((i.kind, i.payload.get("reason")) for i in resolution.review)
    for (kind, reason), count in sorted(reasons.items(), key=str):
        say(capsys, f"  {kind}: {reason}: {count}")


# --- Linking ---


def test_link(capsys):
    guard_budget()
    resolved: Resolution = STATE["resolved"]
    decider = GuardedDecider(get_decider(load_settings()))
    linked = link(resolved, decider)
    STATE["linked"] = linked

    new_edges = [e for e in linked.edges if e.rel == "ROOT_CAUSE"]
    assert all(e.evidence[0].extractor == "linker" for e in new_edges)
    assert all(e.evidence[0].confidence >= questions.GATES["link"] for e in new_edges)
    node_ids = {n.id for n in linked.nodes}
    assert all(e.src in node_ids and e.dst in node_ids for e in linked.edges)
    assert all(e.props["support"] >= 0 for e in linked.edges if e.rel == "INDICATES")

    added = linked.review[len(resolved.review) :]
    mid = [
        i
        for i in added
        if i.kind == "link" and i.payload["reason"] == "unsure root cause"
    ]
    other = Counter(
        i.payload["reason"] for i in added if i not in mid and i.kind == "link"
    )
    asked_incidents = decider.asked
    unusable = sum(
        count
        for reason, count in other.items()
        if reason.startswith(("no decision", "decider error"))
    )
    below = asked_incidents - len(new_edges) - len(mid) - unusable
    say(capsys)
    say(capsys, f"{asked_incidents} root-cause questions asked")
    say(capsys, "root-cause links by gate band:")
    say(capsys, f"  linked (p >= {questions.GATES['link']}): {len(new_edges)}")
    say(
        capsys,
        f"  review ({questions.GATES['link_review']} <= p < {questions.GATES['link']}): {len(mid)}",
    )
    say(capsys, f"  nothing (p < {questions.GATES['link_review']} or unknown): {below}")
    say(capsys, f"  other link review items: {table(other)}")
    conflicts = [i for i in added if i.kind == "conflict"]
    say(capsys, f"history pairs no manual lists: {len(conflicts)}")
    for item in conflicts:
        say(capsys, f"  {item.payload}")
    supported = sum(
        1 for e in linked.edges if e.rel == "INDICATES" and e.props["support"]
    )
    say(capsys, f"INDICATES edges with support above 0: {supported}")


# --- Cost ---


def test_the_run_stays_under_the_stop(capsys):
    rows = ledger_rows()
    assert rows, "the earlier tests paid for calls"
    total = sum(row["usd"] for row in rows)
    say(capsys)
    for row in rows:
        say(
            capsys,
            f"ledger #{row['id']} model={row['model']} purpose={row['purpose']!r} "
            f"input={row['input_tokens']} cached={row['cached_input_tokens']} "
            f"output={row['output_tokens']} usd={row['usd']:.6f} "
            f"cache_hit={row['cache_hit']}",
        )
    by_purpose = Counter()
    cost = Counter()
    for row in rows:
        by_purpose[row["purpose"]] += 1
        cost[row["purpose"]] += row["usd"]
    for purpose in sorted(by_purpose):
        say(capsys, f"{purpose}: {by_purpose[purpose]} calls, ${cost[purpose]:.6f}")
    say(capsys, f"{len(rows)} ledger rows, this run ${total:.6f}")
    say(capsys, f"ledger total ${ledger.spent_usd():.6f}")
    assert ledger.spent_usd() <= STOP_USD
