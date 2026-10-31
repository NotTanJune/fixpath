"""Tests for the helper that swaps a few neighbouring rows so they are out of order."""

import random
from itertools import pairwise

import pytest
from conftest import FixedRandom, descents

from app.generator.row_swaps import swap_neighbours


def rows_with_keys(keys):
    """Rows that can be told apart even when their keys are equal: (key, number)."""
    return [(key, number) for number, key in enumerate(keys)]


def key_of(row):
    return row[0]


def swapped_spots(before, after):
    """The places n where the rows n and n + 1 changed places."""
    return [
        n
        for n in range(len(before) - 1)
        if after[n] == before[n + 1] and after[n + 1] == before[n]
    ]


@pytest.mark.parametrize("seed", range(30))
def test_it_swaps_the_asked_number_of_separate_pairs_and_moves_nothing_else(seed):
    before = rows_with_keys(range(40))
    after = list(before)
    swap_neighbours(random.Random(seed), after, key=key_of, count=3)
    spots = swapped_spots(before, after)
    assert len(spots) == 3
    assert all(later - earlier >= 2 for earlier, later in pairwise(spots))
    assert sum(1 for was, now in zip(before, after) if was != now) == 6
    assert descents(key_of(row) for row in after) == 3


@pytest.mark.parametrize("seed", range(30))
def test_it_never_swaps_two_rows_that_have_the_same_key(seed):
    before = rows_with_keys([1, 1, 1, 2, 2, 3, 3, 3, 3, 4, 5, 5, 6, 6, 6, 7])
    after = list(before)
    swap_neighbours(random.Random(seed), after, key=key_of, count=3)
    spots = swapped_spots(before, after)
    assert len(spots) == 3
    assert all(key_of(before[n]) != key_of(before[n + 1]) for n in spots)
    assert descents(key_of(row) for row in after) == 3


def test_when_the_keys_differ_in_one_place_only_it_swaps_there():
    before = rows_with_keys([1, 1, 1, 1, 2, 2, 2, 2])
    for seed in range(10):
        after = list(before)
        swap_neighbours(random.Random(seed), after, key=key_of, count=1)
        assert swapped_spots(before, after) == [3]


def test_every_place_where_the_keys_differ_can_be_chosen():
    seen = set()
    for seed in range(200):
        before = rows_with_keys(range(6))
        after = list(before)
        swap_neighbours(random.Random(seed), after, key=key_of, count=1)
        seen |= set(swapped_spots(before, after))
    assert seen == {0, 1, 2, 3, 4}


def test_the_same_seed_gives_the_same_swaps_and_other_seeds_give_other_swaps():
    def result(seed):
        rows = rows_with_keys(range(40))
        swap_neighbours(random.Random(seed), rows, key=key_of, count=3)
        return rows

    assert result(5) == result(5)
    assert len({tuple(result(seed)) for seed in range(20)}) > 10


def test_it_works_with_a_generator_whose_random_never_changes():
    before = rows_with_keys(range(40))
    after = list(before)
    swap_neighbours(FixedRandom(0.5, seed=4), after, key=key_of, count=3)
    assert len(swapped_spots(before, after)) == 3
    assert descents(key_of(row) for row in after) == 3


@pytest.mark.parametrize("seed", range(10))
def test_a_key_may_be_a_tuple_and_equal_tuples_stay_put(seed):
    keys = [("A", 1), ("A", 1), ("A", 2), ("B", 1), ("B", 1), ("B", 2), ("C", 1)]
    before = rows_with_keys(keys)
    after = list(before)
    swap_neighbours(random.Random(seed), after, key=key_of, count=2)
    spots = swapped_spots(before, after)
    assert len(spots) == 2
    assert all(key_of(before[n]) != key_of(before[n + 1]) for n in spots)


def test_asking_for_no_swaps_changes_nothing_and_draws_nothing():
    rng = random.Random(3)
    state = rng.getstate()
    rows = rows_with_keys(range(10))
    swap_neighbours(rng, rows, key=key_of, count=0)
    assert rows == rows_with_keys(range(10))
    assert rng.getstate() == state


@pytest.mark.parametrize(
    ("keys", "count"),
    [
        ([], 1),
        ([1], 1),
        ([1, 1, 1], 1),
        ([1, 2, 3], 2),
        ([1, 2, 3, 4, 5], 3),
    ],
)
def test_it_refuses_when_the_rows_have_no_room_for_that_many_pairs(keys, count):
    rows = rows_with_keys(keys)
    with pytest.raises(ValueError, match=rf"{count} pairs"):
        swap_neighbours(random.Random(0), rows, key=key_of, count=count)
    assert rows == rows_with_keys(keys)
