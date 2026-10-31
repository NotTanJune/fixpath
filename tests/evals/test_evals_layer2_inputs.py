"""Tests for what layer 2 is fed: the incident records, the first quotes of mentions,
the aliases known at decision time and the hard negative pairs."""

import pytest
from eval_stubs import make_graph, make_truth, mentions, node
from langchain_core.globals import get_llm_cache, set_llm_cache
from resolve_stubs import ALIASES, edge as draft_edge, extraction, node as draft_node

from app import llm_cache
from app.evals.layer2 import EntityPair, resolver_negatives, sibling_negatives
from app.evals.replay import (
    AskedEntity,
    aliases_at,
    first_mentions,
    records_from,
    replay_extractions,
)

EXPORT = "maintenance/export.csv"
NOTE = "handovers/a.txt"


def test_records_come_from_incident_drafts_with_the_machine_resolved_to_its_asset_id():
    csv = extraction(
        EXPORT,
        "maintenance_note",
        [
            draft_node(
                "Incident",
                "WO-1",
                doc=EXPORT,
                canonical=False,
                props={"date": "2026-04-14", "technician": "NA", "downtime_min": None},
            ),
            draft_node(
                "Incident",
                "WO-1",
                doc=EXPORT,
                canonical=False,
                props={"date": "2026-04-14", "technician": "NA", "downtime_min": 90},
            ),
            draft_node("Part", "SK-50", doc=EXPORT, canonical=False),
        ],
        [
            draft_edge(
                ("Incident", "WO-1"), "OCCURRED_ON", ("Asset", "hyd press"), doc=EXPORT
            )
        ],
    )
    note = extraction(
        NOTE,
        "handover",
        [
            draft_node(
                "Incident",
                f"{NOTE}#1",
                doc=NOTE,
                canonical=False,
                props={"date": "2026-04-23"},
            )
        ],
        [
            draft_edge(
                ("Incident", f"{NOTE}#1"), "OCCURRED_ON", ("Asset", "Nowhere"), doc=NOTE
            )
        ],
    )
    found = records_from([csv, note], ALIASES)
    row = found[EXPORT, "WO-1"]
    assert (row.date, row.machine, row.technician, row.downtime_min) == (
        "2026-04-14",
        "HP-01",
        "NA",
        90,
    )
    assert found[NOTE, f"{NOTE}#1"].machine is None
    assert set(found) == {(EXPORT, "WO-1"), (NOTE, f"{NOTE}#1")}


def test_the_first_occurrence_of_a_mention_is_the_first_in_processing_order():
    first = extraction(
        EXPORT,
        "maintenance_note",
        [draft_node("Part", "SK-50", doc=EXPORT, canonical=False, quote="node quote")],
        [
            draft_edge(
                ("Incident", "WO-1"),
                "USED",
                ("Part", "SK-50"),
                doc=EXPORT,
                quote="edge quote",
            ),
            draft_edge(
                ("Incident", "WO-1"),
                "USED",
                ("Part", "HLP46"),
                doc=EXPORT,
                quote="hlp quote",
            ),
        ],
    )
    later = extraction(
        NOTE,
        "handover",
        [],
        [
            draft_edge(
                ("Incident", "k"), "USED", ("Part", "HLP46"), doc=NOTE, quote="later"
            )
        ],
    )
    found = first_mentions([first, later])
    assert found["Part", "SK-50"] == (EXPORT, "node quote")
    assert found["Part", "HLP46"] == (EXPORT, "hlp quote")
    assert ("Incident", "WO-1") not in found


def test_canonical_nodes_are_not_mentions_and_an_edge_looks_at_its_destination_first():
    items = extraction(
        "inventory/spares.xlsx",
        "inventory",
        [
            draft_node(
                "Part", "SEAL-KIT-50", doc="inventory/spares.xlsx", canonical=True
            )
        ],
        [
            draft_edge(
                ("Part", "P-SRC"),
                "FITS",
                ("Part", "P-DST"),
                doc="inventory/spares.xlsx",
                quote="fits",
            )
        ],
    )
    found = first_mentions([items])
    assert ("Part", "SEAL-KIT-50") not in found
    assert found["Part", "P-DST"] == ("inventory/spares.xlsx", "fits")
    assert found["Part", "P-SRC"] == ("inventory/spares.xlsx", "fits")


MERGES = [
    {
        "type": "Part",
        "mention": "SK-50",
        "candidate": "SEAL-KIT-50",
        "decision": "merge",
    },
    {"type": "Part", "mention": "RV", "candidate": "SEAL-RV-25", "decision": "merge"},
    {
        "type": "Part",
        "mention": "SK50",
        "candidate": "SEAL-KIT-50",
        "decision": "queue",
    },
    {
        "type": "Part",
        "mention": "kit-50",
        "candidate": "SEAL-KIT-50",
        "decision": "merge",
    },
]


def parts_graph(extra=()):
    return make_graph(
        [
            node(
                "SEAL-KIT-50",
                "Part",
                aliases=["Base", "SK-50", "KIT-50"],
                description="Seal kit",
            ),
            node("SEAL-RV-25", "Part", aliases=["RV"], description="Relief valve seal"),
            *extra,
        ],
        [],
    )


def test_the_aliases_at_decision_time_leave_out_the_ones_this_and_later_merges_added():
    graph = parts_graph()
    assert aliases_at(graph, "SEAL-KIT-50", MERGES, 0) == ["Base"]
    assert aliases_at(graph, "SEAL-KIT-50", MERGES, 2) == ["Base", "SK-50"]
    assert aliases_at(graph, "SEAL-KIT-50", MERGES, 3) == ["Base", "SK-50"]
    assert aliases_at(graph, "SEAL-KIT-50", MERGES, 4) == ["Base", "SK-50", "KIT-50"]
    # a merge into another candidate does not touch this one
    assert aliases_at(graph, "SEAL-RV-25", MERGES, 0) == []
    assert aliases_at(graph, "SEAL-RV-25", MERGES, 2) == ["RV"]


# Truth aliases that the mentions of the tests below are spelled with.
EXTRA_ALIASES = {"SEAL-01": ["SEAL 03 kit"], "OIL-HLP46": ["HLP oil"]}


def family_world():
    ids = [f"SEAL-{n:02d}" for n in range(1, 9)]
    parts = [node(i, "Part", aliases=[], description=f"Seal number {i}") for i in ids]
    parts += [node("OIL-HLP46", "Part", aliases=[], description="Hydraulic oil")]
    truth = make_truth(
        [
            node(i, "Part", aliases=[f"alias {i}", *EXTRA_ALIASES.get(i, [])])
            for i in [*ids, "OIL-HLP46"]
        ],
        [],
        doc_mentions={"handovers/a.txt": mentions(parts=["SEAL-01", "OIL-HLP46"])},
    )
    return truth, make_graph(parts, [])


def pair(mention, candidate, index=0, doc="handovers/a.txt"):
    return EntityPair(index, "Part", doc, mention, candidate, 0.9, True)


def siblings(truth, graph, pairs, asked=()):
    return sibling_negatives(truth, graph, pairs, set(asked))


def test_siblings_are_every_other_part_of_the_family_of_the_true_candidate():
    truth, graph = family_world()
    found = siblings(truth, graph, [pair("SEAL 03 kit", "SEAL-01")])
    assert [(n.kind, n.candidate) for n in found] == [
        ("sibling", f"SEAL-{n:02d}") for n in range(2, 9)
    ]
    assert all(
        (n.type, n.doc_path, n.mention) == ("Part", "handovers/a.txt", "SEAL 03 kit")
        for n in found
    )


def test_a_sibling_the_resolver_already_asked_about_is_left_out():
    truth, graph = family_world()
    asked = [("Part", "SEAL 03 kit", "handovers/a.txt", "SEAL-02")]
    found = siblings(truth, graph, [pair("SEAL 03 kit", "SEAL-01")], asked)
    assert "SEAL-02" not in {n.candidate for n in found} and len(found) == 6
    # the same candidate for another mention is still a sibling of that mention
    other = siblings(truth, graph, [pair("alias SEAL-01", "SEAL-01")], asked)
    assert "SEAL-02" in {n.candidate for n in other}


def test_siblings_are_in_pair_order_and_do_not_depend_on_the_order_the_pairs_come_in():
    truth, graph = family_world()
    pairs = [pair("SEAL 03 kit", "SEAL-01", index=n) for n in (4, 1, 3)]
    first = siblings(truth, graph, pairs)
    again = siblings(truth, graph, list(reversed(pairs)))
    assert first == again
    assert [n.index for n in first] == sorted(n.index for n in first)


def test_only_true_pairs_have_siblings_and_a_family_of_one_has_none():
    truth, graph = family_world()
    wrong = EntityPair(0, "Part", "handovers/a.txt", "x", "SEAL-01", 0.9, False)
    unknown = EntityPair(0, "Part", "handovers/a.txt", "x", "SEAL-01", 0.9, None)
    assert siblings(truth, graph, [wrong, unknown]) == []
    assert siblings(truth, graph, [pair("HLP oil", "OIL-HLP46")]) == []


def test_a_part_the_truth_says_the_mention_names_is_never_picked_as_a_sibling():
    truth, graph = family_world()
    # the truth gives SEAL-02 the alias "twin", and so does SEAL-01
    truth.nodes["SEAL-02"].props["aliases"].append("twin")
    truth.nodes["SEAL-01"].props["aliases"].append("twin")
    found = siblings(truth, graph, [pair("twin", "SEAL-01")])
    assert "SEAL-02" not in {n.candidate for n in found} and len(found) == 6


def asked_entity(mention, candidate, p, doc="handovers/a.txt", type="Part"):
    state = {
        "mention": {"type": type, "text": mention, "quote": "q", "doc_path": doc},
        "candidate": {"id": candidate},
    }
    return AskedEntity(state, p, "jev")


def test_resolver_negatives_are_the_asked_candidates_the_truth_says_are_wrong():
    truth, _ = family_world()
    asked = [
        asked_entity("alias SEAL-01", "SEAL-01", 0.95),
        asked_entity("alias SEAL-01", "SEAL-03", 0.3),
        asked_entity("alias SEAL-01", "SEAL-02", 0.1),
        asked_entity("alias SEAL-01", "SEAL-03", 0.3),
    ]
    merges = [
        {"type": "Part", "doc_path": "handovers/a.txt", "mention": "alias SEAL-01"}
    ]
    found = resolver_negatives(truth, asked, merges, logged=set())
    assert [(n.kind, n.candidate, n.p, n.backend, n.index) for n in found] == [
        ("resolver", "SEAL-02", 0.1, "jev", 0),
        ("resolver", "SEAL-03", 0.3, "jev", 0),
    ]


def test_a_logged_question_and_a_question_the_truth_cannot_label_are_not_negatives():
    truth, _ = family_world()
    asked = [
        asked_entity("alias SEAL-01", "SEAL-03", 0.3),
        asked_entity("zzz", "SEAL-03", 0.4, doc="handovers/none.txt"),
    ]
    logged = {("Part", "alias SEAL-01", "handovers/a.txt", "SEAL-03")}
    assert resolver_negatives(truth, asked, [], logged) == []


@pytest.fixture
def no_model_cache(monkeypatch, tmp_path):
    previous = get_llm_cache()
    set_llm_cache(None)
    monkeypatch.setattr(llm_cache, "CACHE_PATH", tmp_path / "default.sqlite")
    yield
    set_llm_cache(previous)


def test_replaying_the_extractors_keeps_a_model_cache_that_is_already_installed(
    no_model_cache, tmp_path
):
    mine = llm_cache.enable_cache(tmp_path / "copy.sqlite")
    replay_extractions(tmp_path / "raw")
    assert get_llm_cache() is mine
    assert mine.path == tmp_path / "copy.sqlite"


def test_replaying_the_extractors_installs_the_default_cache_when_there_is_none(
    no_model_cache, tmp_path
):
    assert get_llm_cache() is None
    replay_extractions(tmp_path / "raw")
    installed = get_llm_cache()
    assert installed is not None and installed.path == tmp_path / "default.sqlite"
