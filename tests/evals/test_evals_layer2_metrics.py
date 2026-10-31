"""Tests for the layer 2 maths: the merge gate, the gate sweep, the reliability table
and the gate recommendation."""

import pytest

from app.evals.metrics import (
    GATE_SWEEP,
    Scored,
    confusion,
    gate_sweep,
    metrics_at,
    recommend,
    reliability,
    summarize,
)

# Four true pairs at p 0.95, 0.85, 0.6 and None, and three false pairs at 0.92, 0.55, 0.1.
ITEMS = [
    Scored(0.95, True),
    Scored(0.85, True),
    Scored(0.6, True),
    Scored(None, True),
    Scored(0.92, False),
    Scored(0.55, False),
    Scored(0.1, False),
]


def test_the_confusion_counts_at_a_gate_treat_no_decision_as_a_miss():
    assert confusion(ITEMS, 0.9) == (1, 1, 3)
    assert confusion(ITEMS, 0.5) == (3, 2, 1)
    # a probability equal to the gate merges
    assert confusion([Scored(0.9, True)], 0.9) == (1, 0, 0)


def test_metrics_at_a_gate_have_the_counts_and_the_ratios():
    result = metrics_at(ITEMS, 0.9)
    assert result["gate"] == 0.9
    assert (result["tp"], result["fp"], result["fn"]) == (1, 1, 3)
    assert result["precision"] == pytest.approx(0.5)
    assert result["recall"] == pytest.approx(0.25)
    assert result["f1"] == pytest.approx(2 / (2 + 1 + 3))


def test_the_gate_sweep_runs_from_0_5_to_0_95_in_steps_of_0_05():
    assert GATE_SWEEP[0] == 0.5 and GATE_SWEEP[-1] == 0.95 and len(GATE_SWEEP) == 10
    sweep = gate_sweep(ITEMS)
    assert [row["gate"] for row in sweep] == list(GATE_SWEEP)
    by_gate = {row["gate"]: row for row in sweep}
    assert by_gate[0.5]["tp"] == 3
    assert by_gate[0.95]["tp"] == 1 and by_gate[0.95]["fp"] == 0
    assert by_gate[0.95]["precision"] == 1.0
    assert by_gate[0.9]["fp"] == 1


def test_the_sweep_gates_are_exact_to_two_decimals():
    assert 0.7 in GATE_SWEEP and 0.85 in GATE_SWEEP and 0.55 in GATE_SWEEP


def test_the_reliability_table_has_ten_bins_with_count_mean_p_and_observed_rate():
    items = [
        Scored(0.05, False),
        Scored(0.15, False),
        Scored(0.95, True),
        Scored(0.91, True),
        Scored(1.0, False),
        Scored(None, True),
    ]
    table = reliability(items)
    bins = table["bins"]
    assert len(bins) == 10
    assert (bins[0]["low"], bins[0]["high"]) == (0.0, 0.1)
    assert bins[0]["count"] == 1 and bins[0]["observed"] == 0.0
    assert bins[1]["count"] == 1
    last = bins[9]
    assert last["count"] == 3
    assert last["mean_p"] == pytest.approx((0.95 + 0.91 + 1.0) / 3)
    assert last["observed"] == pytest.approx(2 / 3)
    assert (
        bins[4]["count"] == 0
        and bins[4]["mean_p"] is None
        and bins[4]["observed"] is None
    )
    # the pair with no probability is left out
    assert sum(b["count"] for b in bins) == 5


def test_the_expected_calibration_error_on_a_known_set():
    # bin 9: p 0.9, 0.9 and observed 1 of 2 -> |0.5 - 0.9| = 0.4, weight 2/4
    # bin 1: p 0.1, 0.1 and observed 0 of 2 -> |0 - 0.1| = 0.1, weight 2/4
    items = [
        Scored(0.9, True),
        Scored(0.9, False),
        Scored(0.1, False),
        Scored(0.1, False),
    ]
    assert reliability(items)["ece"] == pytest.approx(0.5 * 0.4 + 0.5 * 0.1)


def test_a_well_calibrated_set_has_no_error_and_an_empty_set_has_none():
    items = [Scored(1.0, True)] * 3
    assert reliability(items)["ece"] == pytest.approx(0.0)
    assert reliability([])["ece"] is None


def test_the_recommendation_is_the_highest_gate_that_keeps_the_best_recall_among_the_precise_gates():
    items = (
        [Scored(0.99, True)] * 49
        + [Scored(0.99, False)]
        + [Scored(0.7, True)] * 10
        + [Scored(0.65, False)] * 5
    )
    # at 0.5 and 0.65: precision (59/64) < 0.98; at 0.7: 59/60 = 0.983 qualifies with the best
    # recall (59 of 59); from 0.75 up: 49/50 = 0.98 qualifies with a lower recall
    result = recommend(items)
    assert result["gate"] == 0.7
    assert (result["tp"], result["fp"], result["fn"]) == (59, 1, 0)
    assert result["precision"] == pytest.approx(59 / 60)
    assert result["unbound"] is False
    assert "gap" not in result


def test_an_empty_gap_between_the_false_and_the_true_pairs_picks_the_top_of_the_best_recall_run():
    # false pairs reach 0.41, true pairs start at 0.72: gates 0.5 to 0.7 all have P = R = 1
    items = [
        Scored(0.41, False),
        Scored(0.2, False),
        Scored(0.72, True),
        Scored(0.97, True),
    ]
    result = recommend(items)
    assert result["gate"] == 0.7
    assert (result["tp"], result["fp"], result["fn"]) == (2, 0, 0)
    assert result["unbound"] is True
    assert result["gap"] == {"max_false_p": 0.41, "min_true_p": 0.72}
    assert result["sample"] == {"true": 2, "false": 2}


def test_the_gap_runs_from_the_top_false_pair_to_the_lowest_true_pair_at_the_gate():
    # a true pair at 0.6 that the gate loses anyway is not part of the gap around the gate
    items = [
        Scored(0.88, False),
        Scored(0.6, True),
        Scored(0.91, True),
        Scored(0.97, True),
    ]
    result = recommend(items)
    assert result["gate"] == 0.9
    assert result["gap"] == {"max_false_p": 0.88, "min_true_p": 0.91}


def test_a_gate_with_a_false_pair_at_or_above_it_is_bound_by_the_data():
    items = [Scored(0.85, False)] + [Scored(0.9, True)] * 3 + [Scored(0.96, True)] * 50
    # 0.9 is the highest gate that keeps all 53 true pairs, and the false pair at 0.85 is below it
    result = recommend(items)
    assert result["gate"] == 0.9 and result["unbound"] is True
    assert result["gap"] == {"max_false_p": 0.85, "min_true_p": 0.9}
    # here one false pair sits at the best recall gate, so the gate does hold something back
    bound = [Scored(0.9, False)] + [Scored(0.95, True)] * 60 + [Scored(0.9, True)]
    result = recommend(bound)
    assert result["gate"] == 0.9
    assert result["fp"] == 1 and result["unbound"] is False


def test_a_refused_pair_never_merges_whatever_its_probability():
    items = [Scored(0.95, False, refused=True), Scored(0.95, True), Scored(0.92, True)]
    assert confusion(items, 0.9) == (2, 0, 0)
    assert confusion(items, 0.95) == (1, 0, 1)
    # a refused false pair is not what the gate holds back, so it is not in the gap
    result = recommend(items)
    assert result["gate"] == 0.9 and result["unbound"] is True
    assert result["gap"] == {"max_false_p": None, "min_true_p": 0.92}


def test_no_recommendation_when_no_gate_reaches_the_precision():
    items = [Scored(0.99, True), Scored(0.99, False)]
    result = recommend(items)
    assert result["gate"] is None
    assert "reason" in result


def test_a_gate_that_merges_nothing_is_not_recommended():
    result = recommend([Scored(0.3, True), Scored(0.2, False)])
    assert result["gate"] is None


def test_summarize_reports_the_gate_bands_the_sweep_the_reliability_and_the_recommendation():
    def band(p):
        if p is None:
            return "undecided"
        return "merge" if p >= 0.9 else "queue" if p >= 0.5 else "none"

    report = summarize(ITEMS, gate=0.9, band=band, unlabeled=2)
    assert report["gate"] == 0.9
    assert report["pairs"] == {"labeled": 7, "true": 4, "false": 3, "unlabeled": 2}
    assert report["at_gate"]["tp"] == 1 and report["at_gate"]["fp"] == 1
    assert report["bands"]["merge"] == {"true": 1, "false": 1}
    assert report["bands"]["queue"] == {"true": 2, "false": 1}
    assert report["bands"]["none"] == {"true": 0, "false": 1}
    assert report["bands"]["undecided"] == {"true": 1, "false": 0}
    assert len(report["sweep"]) == 10
    assert len(report["reliability"]["bins"]) == 10
    assert "recommendation" in report
    assert report["max_false_p"] == 0.92 and report["min_true_p"] == 0.6
    assert report["sample"] == {"true": 4, "false": 3}


def test_summarize_reports_none_for_the_p_range_of_a_kind_with_no_false_pair():
    report = summarize(
        [Scored(0.9, True), Scored(None, True)],
        gate=0.75,
        band=lambda p: "undecided" if p is None else "link",
        unlabeled=0,
    )
    assert report["max_false_p"] is None and report["min_true_p"] == 0.9
    assert report["sample"] == {"true": 2, "false": 0}
    assert report["recommendation"]["unbound"] is True
