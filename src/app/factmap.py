"""Build the fact map: every fact and every passage laid out as a point in 3D.

Run it with `uv run --group map python -m app.factmap`. It needs no key and calls no
paid model. Facts and passages are turned into numbers with the same local model, then
placed so that similar items sit close together. The web app reads the result.

`write_map` builds and saves the full map. `place_new` and `append_placed` add the
points of a newly uploaded file without moving the old ones. After that the map is out
of date, and `make map` rebuilds it.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable, Collection
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import KG_PATH, ROOT
from app.embed import DIMENSIONS, MODEL_NAME, Embedder
from app.ingest.load import PassageEmbedder

MAP_PATH = ROOT / "data" / "map" / "fact_map.json"
VECTORS_PATH = MAP_PATH.with_name("vectors.npy")
UMAP_SETTINGS = {
    "n_components": 3,
    "n_neighbors": 15,
    "min_dist": 0.1,
    "metric": "cosine",
    "random_state": 7,
}
LABEL_CHARS = 120  # the longest passage label

# The words used for each kind of link. They match the web app, and a test checks that.
VERBS = {
    "HAS_MACHINE": "has machine",
    "HAS_COMPONENT": "is built from",
    "RAISED": "raised",
    "INDICATES": "points to",
    "AFFECTS": "damages",
    "RESOLVED_BY": "is fixed by",
    "REQUIRES": "needs",
    "FITS": "fits",
    "OCCURRED_ON": "happened on",
    "TRIGGERED_BY": "started with",
    "ROOT_CAUSE": "was caused by",
    "USED": "used",
}
TYPE_WORDS = {
    "AlarmCode": "Alarm code",
    "Asset": "Machine",
    "Component": "Component",
    "FailureMode": "Failure mode",
    "Incident": "Incident",
    "Line": "Line",
    "Part": "Part",
    "Procedure": "Procedure",
}

Projector = Callable[[np.ndarray], np.ndarray]


def node_text(type_: str, label: str) -> str:
    """Describe a node as a short sentence, such as "Part: Main cylinder seal kit"."""
    return f"{TYPE_WORDS.get(type_, type_)}: {label}"


def link_text(src_label: str, rel: str, dst_label: str) -> str:
    """Describe a link as a short sentence, such as "battery fits servo"."""
    verb = VERBS.get(rel, rel.lower().replace("_", " "))
    return f"{src_label} {verb} {dst_label}"


def passage_label(text: str) -> str:
    """Put the text on one line and cut it with "..." if it is long."""
    line = " ".join(text.split())
    if len(line) <= LABEL_CHARS:
        return line
    return line[: LABEL_CHARS - 3].rstrip() + "..."


def scaled(coords: np.ndarray) -> np.ndarray:
    """Centre the points, scale them to fit between -1 and 1, and round them."""
    centred = np.asarray(coords, dtype=np.float64)
    centred = centred - centred.mean(axis=0)
    peak = np.abs(centred).max()
    if peak > 0:
        centred = centred / peak
    return np.round(centred, 4) + 0.0


def _evidence(conn: sqlite3.Connection, kind: str):
    """List the documents and passages that back each fact of this kind."""
    docs: dict[str, set[str]] = defaultdict(set)
    passages: dict[str, set[str]] = defaultdict(set)
    rows = conn.execute(
        "SELECT fact_id, doc_id, chunk_id FROM evidence WHERE fact_kind = ?", (kind,)
    )
    for fact_id, doc_id, chunk_id in rows:
        docs[fact_id].add(doc_id)
        if chunk_id:
            passages[fact_id].add(chunk_id)
    return docs, passages


def vectors_path(map_path: Path | str) -> Path:
    """Return the path of the vectors file that sits beside the map file."""
    return Path(map_path).with_name("vectors.npy")


def collect(
    conn: sqlite3.Connection,
    embedder: PassageEmbedder,
    skip: Collection[str] = frozenset(),
) -> tuple[list[dict], np.ndarray]:
    """Collect the points that need a position, and a vector for each.

    Points the map already has are left out."""
    points: list[dict] = []
    stored: list[np.ndarray] = []
    rows = conn.execute(
        "SELECT c.id, c.doc_id, c.text, c.embedding, d.doc_type FROM chunks c"
        " JOIN documents d ON d.id = c.doc_id ORDER BY c.id"
    )
    for chunk_id, doc_id, text, blob, doc_type in rows:
        if chunk_id in skip:
            continue
        if blob is None:
            raise ValueError(f"passage {chunk_id} has no embedding; run a full ingest")
        stored.append(np.frombuffer(blob, dtype=np.float32))
        points.append(
            {
                "id": chunk_id,
                "kind": "passage",
                "type": doc_type,
                "label": passage_label(text),
                "docs": [doc_id],
            }
        )

    texts: list[str] = []
    node_docs, node_passages = _evidence(conn, "node")
    labels: dict[str, str] = {}
    for node_id, type_, label in conn.execute(
        "SELECT id, type, label FROM nodes ORDER BY id"
    ):
        labels[node_id] = label
        if node_id in skip:
            continue
        texts.append(node_text(type_, label))
        points.append(
            {
                "id": node_id,
                "kind": "node",
                "type": type_,
                "label": label,
                "docs": sorted(node_docs[node_id]),
                "passages": sorted(node_passages[node_id]),
            }
        )
    edge_docs, edge_passages = _evidence(conn, "edge")
    for edge_id, src, rel, dst in conn.execute(
        "SELECT id, src, rel, dst FROM edges ORDER BY id"
    ):
        if edge_id in skip:
            continue
        sentence = link_text(labels[src], rel, labels[dst])
        texts.append(sentence)
        points.append(
            {
                "id": edge_id,
                "kind": "link",
                "type": rel,
                "label": sentence,
                "docs": sorted(edge_docs[edge_id]),
                "passages": sorted(edge_passages[edge_id]),
            }
        )

    facts = (
        embedder.embed_passages(texts)
        if texts
        else np.empty((0, DIMENSIONS), dtype=np.float32)
    )
    passages = (
        np.vstack(stored) if stored else np.empty((0, DIMENSIONS), dtype=np.float32)
    )
    vectors = np.vstack([passages, np.asarray(facts, dtype=np.float32)])
    return points, vectors.astype(np.float32, copy=False)


def layout(
    conn: sqlite3.Connection,
    embedder: PassageEmbedder,
    project: Projector,
    kg_sha256: str,
) -> tuple[dict, np.ndarray]:
    """Build the full map and the vectors behind it."""
    points, vectors = collect(conn, embedder)
    coords = np.asarray(project(vectors))
    if coords.shape != (len(points), 3):
        raise ValueError(
            f"the projection gave shape {coords.shape}, not 3D points for {len(points)}"
        )
    for point, (x, y, z) in zip(points, scaled(coords).tolist(), strict=True):
        point.update(x=x, y=y, z=z)
    kinds = [p["kind"] for p in points]
    fact_map = {
        "model": MODEL_NAME,
        "umap": dict(UMAP_SETTINGS),
        "kg_sha256": kg_sha256,
        "counts": {
            "passages": kinds.count("passage"),
            "nodes": kinds.count("node"),
            "links": kinds.count("link"),
        },
        "points": points,
    }
    return fact_map, vectors


def build_map(
    conn: sqlite3.Connection,
    embedder: PassageEmbedder,
    project: Projector,
    kg_sha256: str,
) -> dict:
    """Build the map without returning the vectors."""
    return layout(conn, embedder, project, kg_sha256)[0]


def to_json(fact_map: dict) -> str:
    return json.dumps(fact_map, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def umap_project(vectors: np.ndarray) -> np.ndarray:
    """Squeeze the vectors into three dimensions with fixed settings."""
    import umap  # only installed with the "map" group

    return umap.UMAP(**UMAP_SETTINGS).fit_transform(vectors)


def write_map(
    kg_path: Path = KG_PATH,
    out: Path = MAP_PATH,
    embedder: PassageEmbedder | None = None,
    project: Projector = umap_project,
) -> dict:
    """Build the map from the graph and save it, with its vectors beside it."""
    kg_path, out = Path(kg_path), Path(out)
    sha = file_sha256(kg_path)
    with closing(sqlite3.connect(f"file:{kg_path}?mode=ro", uri=True)) as conn:
        fact_map, vectors = layout(conn, embedder or Embedder(), project, sha)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_json(fact_map), encoding="utf-8")
    np.save(vectors_path(out), vectors.astype(np.float32, copy=False))
    return fact_map


# --- placing a new file on the map ----------------------------------------------------

NEIGHBOURS = 8
SIMILARITY_POWER = 4


def place(new_vectors, base_vectors, base_xyz, k: int = NEIGHBOURS) -> np.ndarray:
    """Give new vectors a position without moving old points.

    Each new point goes near its most similar old points, with the closest ones counting most."""
    new = np.asarray(new_vectors, dtype=np.float32)
    if not len(new):
        return np.empty((0, 3))
    sims = new @ np.asarray(base_vectors, dtype=np.float32).T
    if not sims.shape[1]:
        raise ValueError("there are no points on the map to place against")
    k = min(k, sims.shape[1])
    top = np.argsort(-sims, axis=1, kind="stable")[:, :k]
    weights = (
        np.clip(np.take_along_axis(sims, top, axis=1), 0, None) ** SIMILARITY_POWER
    )
    weights = np.where(weights.sum(axis=1, keepdims=True) > 0, weights, 1.0)
    xyz = (weights[..., None] * np.asarray(base_xyz)[top]).sum(axis=1) / weights.sum(
        axis=1, keepdims=True
    )
    return np.round(xyz, 4) + 0.0


@dataclass
class Placement:
    """New points that the map does not have yet, with their positions and vectors."""

    passages: list[dict]
    facts: list[dict]
    vectors: np.ndarray

    def event(self) -> dict:
        """Return the event that tells the client about the new points."""
        return {
            "passages": [
                {key: p[key] for key in ("id", "x", "y", "z")} for p in self.passages
            ],
            "facts": [
                {
                    **{
                        key: p[key]
                        for key in ("id", "kind", "type", "label", "x", "y", "z")
                    },
                    "passages": list(p.get("passages", [])),
                }
                for p in self.facts
            ],
        }


def place_new(
    kg_path: Path, map_path: Path, embedder: PassageEmbedder
) -> Placement | None:
    """Position every passage, node and link that the map does not have yet.

    Return None if there is no usable map."""
    map_path = Path(map_path)
    try:
        fact_map = json.loads(map_path.read_text(encoding="utf-8"))
        base = np.load(vectors_path(map_path))
    except (OSError, ValueError):
        return None
    points = fact_map["points"]
    if not points or base.ndim != 2 or len(base) != len(points):
        return None
    known = {point["id"] for point in points}
    with closing(sqlite3.connect(f"file:{kg_path}?mode=ro", uri=True)) as conn:
        found, vectors = collect(conn, embedder, skip=known)
    base_xyz = np.array([[p["x"], p["y"], p["z"]] for p in points])
    for point, (x, y, z) in zip(
        found, place(vectors, base, base_xyz).tolist(), strict=True
    ):
        point.update(x=x, y=y, z=z)
    return Placement(
        [p for p in found if p["kind"] == "passage"],
        [p for p in found if p["kind"] != "passage"],
        vectors.astype(np.float32, copy=False),
    )


def _replace(path: Path, write: Callable) -> None:
    """Write a temporary file first, then swap it in, so a crash leaves the old file."""
    temporary = path.with_name(f".{path.name}.writing")
    try:
        with temporary.open("wb") as handle:
            write(handle)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_placed(
    map_path: Path,
    vectors_path: Path,
    passages: list[dict],
    facts: list[dict],
    vectors: np.ndarray,
) -> None:
    """Add new points and their vectors to the map files.

    Old points stay as they are. The map is then no longer a full layout, and
    `make map` rebuilds it. Raise ValueError, with nothing written, if the counts do
    not match."""
    map_path, vectors_path = Path(map_path), Path(vectors_path)
    fact_map = json.loads(map_path.read_text(encoding="utf-8"))
    base = np.load(vectors_path)
    if base.ndim != 2 or len(base) != len(fact_map["points"]):
        raise ValueError(
            f"{vectors_path.name} has {len(base)} rows for {len(fact_map['points'])}"
            " points; run make map"
        )
    added = [{**point, "placed": True} for point in (*passages, *facts)]
    vectors = np.asarray(vectors, dtype=np.float32)
    if len(vectors) != len(added):
        raise ValueError(f"{len(vectors)} rows of vectors for {len(added)} new points")
    if not added:
        return
    stacked = np.vstack([base, vectors]) if len(base) else vectors
    fact_map["points"].extend(added)
    kinds = [point["kind"] for point in added]
    for kind, key in (("passage", "passages"), ("node", "nodes"), ("link", "links")):
        fact_map["counts"][key] += kinds.count(kind)
    _replace(vectors_path, lambda handle: np.save(handle, stacked))
    _replace(map_path, lambda handle: handle.write(to_json(fact_map).encode("utf-8")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build the 3D fact map")
    parser.add_argument("--kg", type=Path, default=KG_PATH)
    parser.add_argument("--out", type=Path, default=MAP_PATH)
    args = parser.parse_args(argv)
    fact_map = write_map(args.kg, args.out)
    counts = fact_map["counts"]
    print(
        f"{args.out}: {counts['passages']} passages, {counts['nodes']} nodes,"
        f" {counts['links']} links; vectors in {vectors_path(args.out)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
