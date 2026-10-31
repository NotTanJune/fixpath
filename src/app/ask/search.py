"""Searches the manual text by keyword and by meaning.

manual_search runs both searches over the manual pages, merges the two rankings and
returns the best passages. Incident notes and logs are never returned. manual_docs
picks which manuals to search: the one for a machine's model, or the ones that list
an alarm code. reciprocal_rank_fusion merges ranked lists into one.
"""

import sqlite3
import threading
from collections.abc import Sequence

from app import store
from app.ask.types import Passage
from app.embed import Embedder

POOL = 20  # chunks taken from each method, after the manual filter
RRF_K = 60
_METHODS = ("keyword", "vector")

_default_embedder: Embedder | None = None
_embedder_lock = threading.Lock()


def default_embedder() -> Embedder:
    """The shared embedder. It loads on first use."""
    global _default_embedder
    if _default_embedder is None:
        with _embedder_lock:
            if _default_embedder is None:
                _default_embedder = Embedder()
    return _default_embedder


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], k: int = RRF_K
) -> list[tuple[str, float]]:
    """Merge ranked lists into one. An id scores higher the nearer the top it sits in
    each list. Best first."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda pair: -pair[1])


def manual_docs(
    conn: sqlite3.Connection, asset_id: str | None = None, alarm_code: str | None = None
) -> set[str]:
    """The ids of the manuals to search.

    With a machine, that machine's manual. With only an alarm code, the manuals that
    list it. A machine wins over a code. An unknown code gives every manual. A machine
    with no manual gives none.
    """
    node = asset_id if asset_id is not None else alarm_code
    if node is not None:
        rows = conn.execute(
            "SELECT DISTINCT d.id FROM evidence e JOIN documents d ON d.id = e.doc_id"
            " WHERE e.fact_kind = 'node' AND e.fact_id = ? AND d.doc_type = 'manual'",
            (node.strip().upper(),),
        )
        found = {row["id"] for row in rows}
        if found or asset_id is not None:
            return found
    rows = conn.execute("SELECT id FROM documents WHERE doc_type = 'manual'")
    return {row["id"] for row in rows}


def manual_search(
    conn: sqlite3.Connection,
    query: str,
    asset_id: str | None = None,
    k: int = 5,
    embedder: Embedder | None = None,
    *,
    alarm_code: str | None = None,
) -> list[Passage]:
    """The k best manual passages for the query, from both searches merged."""
    query = query.strip()
    if not query or k <= 0:
        return []
    docs = manual_docs(conn, asset_id, alarm_code)
    if not docs:
        return []
    embedder = embedder or default_embedder()
    # Fetch every chunk, then filter, so notes and logs cannot push out the manuals.
    (total,) = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
    lists = {
        "keyword": store.keyword_search(conn, query, total),
        "vector": store.vector_search(conn, embedder.embed_query(query), total),
    }
    hits: dict[str, dict] = {}
    found_by: dict[str, set[str]] = {}
    rankings = []
    for method in _METHODS:
        ranking = []
        for hit in [h for h in lists[method] if h["doc_id"] in docs][:POOL]:
            hits.setdefault(hit["chunk_id"], hit)
            found_by.setdefault(hit["chunk_id"], set()).add(method)
            ranking.append(hit["chunk_id"])
        rankings.append(ranking)
    paths = {
        row["id"]: row["path"]
        for row in conn.execute(
            "SELECT id, path FROM documents WHERE doc_type = 'manual'"
        )
    }
    return [
        Passage(
            chunk_id=chunk_id,
            doc_path=paths[hits[chunk_id]["doc_id"]],
            page=hits[chunk_id]["page"],
            text=hits[chunk_id]["text"],
            score=score,
            matched_by=[m for m in _METHODS if m in found_by[chunk_id]],
        )
        for chunk_id, score in reciprocal_rank_fusion(rankings)[:k]
    ]
