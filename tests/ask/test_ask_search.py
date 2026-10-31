"""Tests of the manual search, which combines keyword and meaning-based matching over the
manual chunks.
"""

import pytest
from ask_stubs import FakeEmbedder, open_kg
from llm_stubs import isolated  # noqa: F401  (an autouse fixture)

from app.ask import search


@pytest.fixture
def conn(tmp_path):
    connection = open_kg(tmp_path)
    yield connection
    connection.close()


def chunk_ids(passages):
    return [p.chunk_id for p in passages]


# --- merging the two rankings ---


def test_fusion_sums_one_over_sixty_plus_rank_and_breaks_ties_by_first_appearance():
    fused = search.reciprocal_rank_fusion([["a", "b", "c"], ["c", "a"]])
    assert [item for item, _ in fused] == ["a", "c", "b"]
    scores = dict(fused)
    assert scores["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert scores["b"] == pytest.approx(1 / 62)


def test_fusion_of_nothing_is_empty():
    assert search.reciprocal_rank_fusion([]) == []
    assert search.reciprocal_rank_fusion([[], []]) == []


# --- manual search ---


def test_the_passages_are_fused_from_both_methods_with_the_methods_recorded(
    conn, monkeypatch
):
    monkeypatch.setattr(search, "POOL", 2)
    embedder = FakeEmbedder({"main cylinder seal": 2})
    found = search.manual_search(conn, "main cylinder seal", embedder=embedder)
    # keyword matches: HP-5 (three words), HP-3 (two); meaning matches: HP-5, then the first zero score
    assert chunk_ids(found) == ["HP-5", "HP-3", "CNC-2"]
    assert [p.matched_by for p in found] == [
        ["keyword", "vector"],
        ["keyword"],
        ["vector"],
    ]
    assert found[0].score == pytest.approx(2 / 61)
    assert found[1].score == pytest.approx(1 / 62)
    assert embedder.asked == ["main cylinder seal"]


def test_a_passage_holds_the_chunk_text_page_and_document_path(conn):
    (top, *_) = search.manual_search(
        conn, "main cylinder seal", embedder=FakeEmbedder({"main cylinder seal": 2})
    )
    assert (top.chunk_id, top.doc_path, top.page) == ("HP-5", "manuals/hp.pdf", 5)
    assert (
        top.text == "Replace main cylinder seal kit. Lock out the main isolator first."
    )


def test_only_manual_chunks_are_returned(conn):
    # NOTE-1 matches "floor" and the meaning-based query, but it is a maintenance note
    found = search.manual_search(
        conn, "oil on the floor", k=20, embedder=FakeEmbedder({"oil on the floor": 1})
    )
    assert "NOTE-1" not in chunk_ids(found)
    assert "HP-3" in chunk_ids(found)


def test_the_machine_filter_keeps_the_manual_of_its_model(conn):
    embedder = FakeEmbedder({"E-217": 1})
    everything = search.manual_search(conn, "E-217", k=20, embedder=embedder)
    assert {"HP-3", "CNC-4"} <= set(chunk_ids(everything))
    only_hp = search.manual_search(
        conn, "E-217", asset_id="HP-01", k=20, embedder=embedder
    )
    assert chunk_ids(only_hp) and all(c.startswith("HP-") for c in chunk_ids(only_hp))
    only_cnc = search.manual_search(
        conn, "E-217", asset_id="CNC-01", k=20, embedder=embedder
    )
    assert chunk_ids(only_cnc) and all(
        c.startswith("CNC-") for c in chunk_ids(only_cnc)
    )


def test_a_machine_with_no_manual_gets_no_passages(conn):
    embedder = FakeEmbedder()
    assert (
        search.manual_search(conn, "E-217", asset_id="XX-99", embedder=embedder) == []
    )
    assert embedder.asked == []


def test_a_chunk_that_ranks_low_for_a_machine_is_still_found(conn, monkeypatch):
    # Each method picks its top results after the filter, so chunks of other documents
    # cannot push the machine's manual out of the top.
    monkeypatch.setattr(search, "POOL", 1)
    found = search.manual_search(
        conn, "E-217", asset_id="CNC-01", embedder=FakeEmbedder({"E-217": 1})
    )
    assert "CNC-4" in chunk_ids(found)


def test_k_limits_the_result_and_a_blank_query_finds_nothing(conn):
    embedder = FakeEmbedder({"seal": 2})
    assert len(search.manual_search(conn, "seal", k=2, embedder=embedder)) == 2
    assert search.manual_search(conn, "seal", k=0, embedder=embedder) == []
    assert search.manual_search(conn, "   ", embedder=embedder) == []


def test_the_default_embedder_is_built_lazily_and_shared(conn, monkeypatch):
    built = []

    class Default(FakeEmbedder):
        def __init__(self):
            super().__init__({"seal": 2})
            built.append(self)

    monkeypatch.setattr(search, "Embedder", Default)
    monkeypatch.setattr(search, "_default_embedder", None)
    assert built == []
    search.manual_search(conn, "seal")
    search.manual_search(conn, "seal")
    assert len(built) == 1 and built[0].asked == ["seal", "seal"]


def test_manual_docs_lists_every_manual_or_those_that_support_the_machine(conn):
    assert search.manual_docs(conn) == {"DOC-aaaaaaaa", "DOC-bbbbbbbb"}
    assert search.manual_docs(conn, "HP-01") == {"DOC-aaaaaaaa"}
    assert search.manual_docs(conn, "cnc-01") == {"DOC-bbbbbbbb"}
    assert search.manual_docs(conn, "XX-99") == set()


# --- the manual of an alarm code ---


def test_manual_docs_of_an_alarm_are_the_manuals_whose_alarm_table_holds_it(conn):
    assert search.manual_docs(conn, alarm_code="e-214 ") == {"DOC-bbbbbbbb"}
    # no manual holds it, so every manual is searched
    assert search.manual_docs(conn, alarm_code="E-999") == search.manual_docs(conn)


def test_a_machine_beats_an_alarm_code_in_manual_docs(conn):
    assert search.manual_docs(conn, "HP-01", alarm_code="E-214") == {"DOC-aaaaaaaa"}


def test_manual_search_takes_the_alarm_code_to_the_manual_that_holds_it(conn):
    embedder = FakeEmbedder({"alarm": 2})
    found = search.manual_search(conn, "alarm", embedder=embedder, alarm_code="E-214")
    assert found and all(p.chunk_id.startswith("CNC-") for p in found)


def test_the_default_embedder_is_built_once_even_when_two_threads_ask_at_the_same_time(
    monkeypatch,
):
    import threading
    import time

    built = []

    class Slow:
        def __init__(self):
            time.sleep(0.05)  # long enough for the other thread to arrive
            built.append(self)

    monkeypatch.setattr(search, "Embedder", Slow)
    monkeypatch.setattr(search, "_default_embedder", None)
    found = []
    gate = threading.Barrier(4)

    def ask():
        gate.wait()
        found.append(search.default_embedder())

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(built) == 1 and len({id(e) for e in found}) == 1 and len(found) == 4
