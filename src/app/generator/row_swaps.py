"""Put a few neighbouring rows in the wrong order, as a hand-kept list ends up.

swap_neighbours() does this for the maintenance export and the spares sheet.
"""

import random
from collections.abc import Callable


def swap_neighbours[Row](
    rng: random.Random, rows: list[Row], key: Callable[[Row], object], count: int
) -> None:
    """Swap count pairs of neighbouring rows in place. The rows start in order.

    Rows with equal keys are never swapped, and no row is swapped twice. If there
    are not enough places, it raises ValueError and changes nothing."""
    # A swap at n exchanges rows n and n + 1.
    places = [n for n in range(len(rows) - 1) if key(rows[n]) != key(rows[n + 1])]
    chosen: list[int] = []
    for _ in range(count):
        if not places:
            raise ValueError(
                f"cannot swap {count} pairs of neighbours with different keys and "
                f"no shared row, found {len(chosen)}"
            )
        n = rng.choice(places)
        chosen.append(n)
        places = [m for m in places if abs(m - n) > 1]
    for n in chosen:
        rows[n], rows[n + 1] = rows[n + 1], rows[n]
