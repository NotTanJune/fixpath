"""Tests for the 3D fact map, which is built from the graph with no keys. They cover
the layout, the saved vectors and the check that the map matches the current graph."""

import json
import re
import sqlite3
from contextlib import closing

import numpy as np
import pytest
from crew_stubs import build_crew_kg
from load_stubs import FakeEmbedder

from app import factmap
from app.config import ROOT


def first_three(vectors: np.ndarray) -> np.ndarray:
    """A stand-in for the layout step: the first three numbers of each vector."""
    return np.asarray(vectors, dtype=np.float64)[:, :3]


@pytest.fixture
def kg(tmp_path):
    path = build_crew_kg(tmp_path / "kg.sqlite")
    with closing(sqlite3.connect(path)) as conn:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM chunks WHERE embedding IS NOT NULL),"
            " (SELECT count(*) FROM nodes), (SELECT count(*) FROM edges),"
            " (SELECT count(*) FROM evidence WHERE chunk_id IS NOT NULL)"
        ).fetchone()
    # the tests below need at least one of each
    assert all(counts), counts
    return path


def open_ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_node_reads_as_its_type_in_words_and_its_label():
    assert factmap.node_text("Part", "Main cylinder seal kit, 50 mm rod") == (
        "Part: Main cylinder seal kit, 50 mm rod"
    )
    assert factmap.node_text("AlarmCode", "Coolant pressure low") == (
        "Alarm code: Coolant pressure low"
    )
    assert factmap.node_text("Asset", "Machining centre 1") == (
        "Machine: Machining centre 1"
    )


def test_a_link_reads_as_a_sentence_of_names():
    assert factmap.link_text(
        "Encoder backup battery 3.6 V", "FITS", "Axis 2 servo"
    ) == ("Encoder backup battery 3.6 V fits Axis 2 servo")
    assert factmap.link_text("a", "INDICATES", "b") == "a points to b"
    assert factmap.link_text("a", "NEW_REL", "b") == "a new rel b"


def test_a_passage_label_is_its_text_on_one_line_cut_at_120_characters():
    assert factmap.passage_label("one\n two   three") == "one two three"
    long = "word " * 60
    label = factmap.passage_label(long)
    assert len(label) <= 120 and label.endswith("...")


def test_the_verbs_are_the_ones_the_web_list_uses():
    source = (ROOT / "web" / "src" / "lib" / "format.ts").read_text()
    block = source.split("const VERBS")[1].split("};")[0]
    web = dict(re.findall(r'^\s+([A-Z_]+): "([^"]+)",', block, re.MULTILINE))
    assert web == factmap.VERBS


def test_scaled_centres_the_points_scales_to_one_and_rounds():
    coords = np.array([[0.0, 2.0, 4.0], [2.0, 4.0, 8.0], [4.0, 6.0, 12.0]])
    out = factmap.scaled(coords)
    assert np.allclose(out.mean(axis=0), 0)
    assert np.abs(out).max() == 1.0
    assert np.array_equal(out, np.round(out, 4))
    assert not np.signbit(out[out == 0]).any()  # no -0.0 in the file


def test_collect_puts_passages_then_nodes_then_links_each_sorted_by_id(kg):
    with closing(open_ro(kg)) as conn:
        points, vectors = factmap.collect(conn, FakeEmbedder())
        chunks = [r[0] for r in conn.execute("SELECT id FROM chunks ORDER BY id")]
        nodes = [r[0] for r in conn.execute("SELECT id FROM nodes ORDER BY id")]
        edges = [r[0] for r in conn.execute("SELECT id FROM edges ORDER BY id")]
    assert [p["id"] for p in points] == chunks + nodes + edges
    assert [p["kind"] for p in points] == (
        ["passage"] * len(chunks) + ["node"] * len(nodes) + ["link"] * len(edges)
    )
    assert vectors.shape == (len(points), 384) and vectors.dtype == np.float32


def test_a_passage_keeps_its_stored_vector(kg):
    with closing(open_ro(kg)) as conn:
        points, vectors = factmap.collect(conn, FakeEmbedder())
        blob = conn.execute(
            "SELECT embedding FROM chunks WHERE id = ?", (points[0]["id"],)
        ).fetchone()[0]
    assert np.array_equal(vectors[0], np.frombuffer(blob, dtype=np.float32))


def test_a_fact_lists_its_documents_and_passages_from_its_evidence(kg):
    with closing(open_ro(kg)) as conn:
        points, _ = factmap.collect(conn, FakeEmbedder())
        row = conn.execute(
            "SELECT fact_kind, fact_id FROM evidence WHERE chunk_id IS NOT NULL LIMIT 1"
        ).fetchone()
        docs = sorted(
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT doc_id FROM evidence WHERE fact_kind = ? AND fact_id = ?",
                (row[0], row[1]),
            )
        )
        chunks = sorted(
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT chunk_id FROM evidence"
                " WHERE fact_kind = ? AND fact_id = ? AND chunk_id IS NOT NULL",
                (row[0], row[1]),
            )
        )
    kind = "node" if row[0] == "node" else "link"
    point = next(p for p in points if p["kind"] == kind and p["id"] == row[1])
    assert point["docs"] == docs and point["passages"] == chunks


def test_a_link_point_is_labelled_with_the_sentence_that_was_embedded(kg):
    with closing(open_ro(kg)) as conn:
        points, _ = factmap.collect(conn, FakeEmbedder())
        labels = dict(conn.execute("SELECT id, label FROM nodes").fetchall())
    link = next(p for p in points if p["kind"] == "link")
    src, rel, dst = link["id"].split("|")
    assert link["type"] == rel
    assert link["label"] == factmap.link_text(labels[src], rel, labels[dst])


def test_build_map_writes_counts_settings_checksum_and_one_point_each(kg):
    with closing(open_ro(kg)) as conn:
        fact_map = factmap.build_map(conn, FakeEmbedder(), first_three, "abc")
    assert fact_map["model"] == "BAAI/bge-small-en-v1.5"
    assert fact_map["umap"] == factmap.UMAP_SETTINGS
    assert fact_map["kg_sha256"] == "abc"
    kinds = [p["kind"] for p in fact_map["points"]]
    assert fact_map["counts"] == {
        "passages": kinds.count("passage"),
        "nodes": kinds.count("node"),
        "links": kinds.count("link"),
    }
    assert all({"x", "y", "z"} <= set(p) for p in fact_map["points"])
    assert max(abs(p[a]) for p in fact_map["points"] for a in "xyz") == 1.0


def test_build_map_rejects_a_projection_of_the_wrong_shape(kg):
    with closing(open_ro(kg)) as conn, pytest.raises(ValueError, match="3D"):
        factmap.build_map(conn, FakeEmbedder(), lambda v: v[:, :2], "abc")


def test_the_same_graph_gives_the_same_bytes(kg, tmp_path):
    first = factmap.write_map(kg, tmp_path / "a.json", FakeEmbedder(), first_three)
    second = factmap.write_map(kg, tmp_path / "b.json", FakeEmbedder(), first_three)
    a = (tmp_path / "a.json").read_bytes()
    assert a == (tmp_path / "b.json").read_bytes() and first == second
    assert a.endswith(b"\n")
    assert json.loads(a)["kg_sha256"] == factmap.file_sha256(kg)


def test_the_committed_map_matches_the_committed_graph():
    """A graph rebuilt without make map fails here."""
    from app.config import KG_PATH

    fact_map = json.loads(factmap.MAP_PATH.read_text(encoding="utf-8"))
    assert fact_map["kg_sha256"] == factmap.file_sha256(KG_PATH), "run make map"
    with closing(open_ro(KG_PATH)) as conn:
        want = {
            "passage": {r[0] for r in conn.execute("SELECT id FROM chunks")},
            "node": {r[0] for r in conn.execute("SELECT id FROM nodes")},
            "link": {r[0] for r in conn.execute("SELECT id FROM edges")},
        }
    got = {kind: set() for kind in want}
    for point in fact_map["points"]:
        got[point["kind"]].add(point["id"])
    assert got == want
    assert fact_map["umap"] == factmap.UMAP_SETTINGS


# --- placement and vectors.npy ---------------------------------------------------------


def unit_rows(*indexes: int) -> np.ndarray:
    """Vectors that are fully distinct from each other: one row per index, 384 wide."""
    rows = np.zeros((len(indexes), 384), dtype=np.float32)
    for row, index in enumerate(indexes):
        rows[row, index] = 1.0
    return rows


BASE_XYZ = np.array(
    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [-1.0, -1.0, -1.0]]
)


def test_a_new_vector_equal_to_a_base_vector_lands_on_that_base_point():
    base = unit_rows(0, 1, 2, 3)
    placed = factmap.place(unit_rows(2), base, BASE_XYZ)
    assert placed.shape == (1, 3)
    assert placed[0].tolist() == [0.0, 0.0, 1.0]


def test_a_mix_of_two_base_vectors_lands_between_them():
    base = unit_rows(0, 1, 2, 3)
    mix = (base[0] + base[1])[None, :] / np.sqrt(2)
    placed = factmap.place(mix, base, BASE_XYZ)
    assert placed[0].tolist() == [0.5, 0.5, 0.0]


def test_a_closer_neighbour_pulls_harder_by_the_fourth_power_of_the_similarity():
    base = unit_rows(0, 1)
    vector = np.zeros((1, 384), dtype=np.float32)
    vector[0, 0], vector[0, 1] = 0.8, 0.6  # sims 0.8 and 0.6
    placed = factmap.place(vector, base, BASE_XYZ[:2])[0]
    weight = np.array([0.8**4, 0.6**4])
    want = (weight[:, None] * BASE_XYZ[:2]).sum(axis=0) / weight.sum()
    assert placed.tolist() == np.round(want, 4).tolist()


def test_placing_never_changes_the_base_points():
    base, xyz = unit_rows(0, 1, 2), BASE_XYZ[:3].copy()
    base_copy, xyz_copy = base.copy(), xyz.copy()
    factmap.place(unit_rows(0, 1), base, xyz)
    assert (base == base_copy).all() and (xyz == xyz_copy).all()


def test_placement_is_deterministic_and_only_uses_the_k_nearest():
    rng = np.random.default_rng(3)
    base = rng.normal(size=(30, 384)).astype(np.float32)
    base /= np.linalg.norm(base, axis=1, keepdims=True)
    xyz = rng.uniform(-1, 1, size=(30, 3))
    new = base[:5] + 0.05
    new /= np.linalg.norm(new, axis=1, keepdims=True)
    a, b = factmap.place(new, base, xyz), factmap.place(new, base, xyz)
    assert a.tobytes() == b.tobytes()
    assert a.shape == (5, 3) and not np.isnan(a).any()
    assert factmap.place(new, base, xyz, k=1).tolist() == np.round(xyz[:5], 4).tolist()


def test_no_new_vectors_give_an_empty_result():
    assert factmap.place(np.empty((0, 384)), unit_rows(0), BASE_XYZ[:1]).shape == (0, 3)


def test_k_larger_than_the_base_uses_all_of_it():
    placed = factmap.place(unit_rows(0), unit_rows(0, 1), BASE_XYZ[:2], k=8)
    assert placed.shape == (1, 3)


def test_a_vector_with_no_similarity_at_all_gets_the_plain_mean_not_nan():
    placed = factmap.place(unit_rows(9), unit_rows(0, 1), BASE_XYZ[:2])
    assert placed[0].tolist() == [0.5, 0.5, 0.0]


def test_write_map_also_writes_the_vectors_in_the_order_of_the_points(kg, tmp_path):
    out = tmp_path / "map" / "fact_map.json"
    fact_map = factmap.write_map(kg, out, FakeEmbedder(), first_three)
    path = factmap.vectors_path(out)
    assert path == tmp_path / "map" / "vectors.npy"
    vectors = np.load(path)
    assert vectors.dtype == np.float32 and vectors.shape == (
        len(fact_map["points"]),
        384,
    )
    with closing(open_ro(kg)) as conn:
        _, again = factmap.collect(conn, FakeEmbedder())
    assert (vectors == again).all()


def test_the_vectors_are_the_same_bytes_every_time(kg, tmp_path):
    factmap.write_map(kg, tmp_path / "a" / "fact_map.json", FakeEmbedder(), first_three)
    factmap.write_map(kg, tmp_path / "b" / "fact_map.json", FakeEmbedder(), first_three)
    assert (tmp_path / "a" / "vectors.npy").read_bytes() == (
        tmp_path / "b" / "vectors.npy"
    ).read_bytes()


def test_collect_can_skip_the_points_that_are_already_on_the_map(kg):
    with closing(open_ro(kg)) as conn:
        points, vectors = factmap.collect(conn, FakeEmbedder())
        skip = {p["id"] for p in points[::2]}
        rest, rest_vectors = factmap.collect(conn, FakeEmbedder(), skip=skip)
    keep = [i for i, p in enumerate(points) if p["id"] not in skip]
    assert [p["id"] for p in rest] == [points[i]["id"] for i in keep]
    assert (rest_vectors == vectors[keep]).all()
    with closing(open_ro(kg)) as conn:
        everything = factmap.collect(
            conn, FakeEmbedder(), skip=set(p["id"] for p in points)
        )
    assert everything[0] == [] and everything[1].shape == (0, 384)


def add_to_graph(path):
    """A new passage, node and link on top of the graph, as a file ingest adds them."""
    from app import store

    conn = store.connect(path)
    try:
        store.add_chunk(
            conn,
            "DOC-aaaaaaaa-0-99",
            "DOC-aaaaaaaa",
            None,
            99,
            "oil again under the ram",
            FakeEmbedder().embed_passages(["oil again"])[0],
        )
        store.upsert_node(
            conn,
            "INC-2026-099",
            "Incident",
            "Oil under the ram again",
            {
                "id": "INC-2026-099",
                "date": "2026-10-20",
                "symptom_text": "oil under the ram again",
            },
        )
        store.upsert_edge(conn, "INC-2026-099", "OCCURRED_ON", "HP-01", {})
    finally:
        conn.close()


@pytest.fixture
def mapped(kg, tmp_path):
    out = tmp_path / "map" / "fact_map.json"
    factmap.write_map(kg, out, FakeEmbedder(), first_three)
    return out


def test_place_new_places_only_what_the_map_does_not_have(kg, mapped):
    add_to_graph(kg)
    placement = factmap.place_new(kg, mapped, FakeEmbedder())
    assert [p["id"] for p in placement.passages] == ["DOC-aaaaaaaa-0-99"]
    assert [p["id"] for p in placement.facts] == [
        "INC-2026-099",
        "INC-2026-099|OCCURRED_ON|HP-01",
    ]
    assert placement.vectors.shape == (3, 384)
    assert all(
        {"x", "y", "z"} <= set(p) for p in (*placement.passages, *placement.facts)
    )
    event = placement.event()
    assert set(event) == {"passages", "facts"}
    assert set(event["passages"][0]) == {"id", "x", "y", "z"}
    assert event["facts"][0]["kind"] == "node" and event["facts"][1]["kind"] == "link"


def test_place_new_with_nothing_new_is_empty(kg, mapped):
    placement = factmap.place_new(kg, mapped, FakeEmbedder())
    assert (placement.passages, placement.facts) == ([], [])
    assert placement.vectors.shape == (0, 384)


def test_place_new_is_none_without_a_usable_map(kg, tmp_path, mapped):
    assert (
        factmap.place_new(kg, tmp_path / "none" / "fact_map.json", FakeEmbedder())
        is None
    )
    np.save(factmap.vectors_path(mapped), np.zeros((2, 384), dtype=np.float32))
    assert factmap.place_new(kg, mapped, FakeEmbedder()) is None
    factmap.vectors_path(mapped).unlink()
    assert factmap.place_new(kg, mapped, FakeEmbedder()) is None


def test_append_placed_adds_flagged_points_and_vectors_and_leaves_the_rest(kg, mapped):
    add_to_graph(kg)
    old_map = json.loads(mapped.read_text())
    old_vectors = np.load(factmap.vectors_path(mapped))
    placement = factmap.place_new(kg, mapped, FakeEmbedder())
    factmap.append_placed(
        mapped,
        factmap.vectors_path(mapped),
        placement.passages,
        placement.facts,
        placement.vectors,
    )
    new_map = json.loads(mapped.read_text())
    assert new_map["points"][: len(old_map["points"])] == old_map["points"]
    added = new_map["points"][len(old_map["points"]) :]
    assert [p["id"] for p in added] == [
        "DOC-aaaaaaaa-0-99",
        "INC-2026-099",
        "INC-2026-099|OCCURRED_ON|HP-01",
    ]
    assert all(p["placed"] is True for p in added)
    assert not any("placed" in p for p in old_map["points"])
    assert new_map["counts"]["passages"] == old_map["counts"]["passages"] + 1
    assert new_map["counts"]["nodes"] == old_map["counts"]["nodes"] + 1
    assert new_map["counts"]["links"] == old_map["counts"]["links"] + 1
    assert new_map["kg_sha256"] == old_map["kg_sha256"]  # now stale, on purpose
    assert new_map["model"] == old_map["model"] and new_map["umap"] == old_map["umap"]
    vectors = np.load(factmap.vectors_path(mapped))
    assert vectors.dtype == np.float32 and vectors.shape == (
        len(new_map["points"]),
        384,
    )
    assert (vectors[: len(old_vectors)] == old_vectors).all()
    assert (vectors[len(old_vectors) :] == placement.vectors).all()
    assert mapped.read_text().endswith("}\n") and mapped.read_text() == factmap.to_json(
        new_map
    )


def test_append_placed_refuses_vectors_that_do_not_match_the_points(kg, mapped):
    np.save(factmap.vectors_path(mapped), np.zeros((2, 384), dtype=np.float32))
    before = mapped.read_bytes()
    with pytest.raises(ValueError, match="make map"):
        factmap.append_placed(
            mapped, factmap.vectors_path(mapped), [], [], np.empty((0, 384))
        )
    assert mapped.read_bytes() == before


def test_append_placed_refuses_a_row_count_that_is_not_the_points(kg, mapped):
    point = {
        "id": "X",
        "kind": "passage",
        "type": "log",
        "label": "x",
        "docs": [],
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
    }
    before = mapped.read_bytes()
    with pytest.raises(ValueError, match="rows"):
        factmap.append_placed(
            mapped, factmap.vectors_path(mapped), [point], [], np.empty((0, 384))
        )
    assert mapped.read_bytes() == before


def test_the_committed_vectors_have_one_row_per_point_of_the_committed_map():
    """The staleness test for vectors.npy: run make map when it fails."""
    fact_map = json.loads(factmap.MAP_PATH.read_text(encoding="utf-8"))
    vectors = np.load(factmap.VECTORS_PATH)
    assert vectors.dtype == np.float32
    assert vectors.shape == (len(fact_map["points"]), 384), "run make map"
