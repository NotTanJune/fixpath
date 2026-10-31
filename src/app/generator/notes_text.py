"""The wording of the notes: shorthand, typos and unique file names.

shorthand() abbreviates a job title the way a technician would. add_typos() swaps
letters in a few words, but never in a machine, alarm or part reference, so notes
stay readable. unique_name() avoids two files with the same name.
"""

import random
import re
from pathlib import Path

from app.generator import catalogue as cat

TYPO_PROBABILITY = 1 / 12  # per word

MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# Every machine, alarm and part reference. Typos must skip these.
_REFERENCES = sorted(
    {
        *(ref for asset in cat.ASSETS for ref in (asset.id, *asset.aliases)),
        *(ref for spec in cat.ALARMS for ref in (spec.props.code, *spec.props.aliases)),
        *(
            ref
            for spec in cat.PARTS
            for ref in (spec.props.part_no, *spec.props.aliases)
        ),
    },
    key=lambda ref: (-len(ref), ref),
)
_REFERENCE_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(ref) for ref in _REFERENCES) + r")(?!\w)"
)

_SHORTHAND = (
    ("Replace", "Repl"),
    ("Check", "Chk"),
    ("bearing", "brg"),
    (" and ", " & "),
)


def shorthand(text: str) -> str:
    """Abbreviate a job title the way a technician would."""
    for long, short in _SHORTHAND:
        text = text.replace(long, short)
    return text


def add_typos(rng: random.Random, text: str) -> str:
    """Swap two neighbouring letters in about one word in twelve. Short words and
    references are left alone."""
    protected = [match.span() for match in _REFERENCE_PATTERN.finditer(text)]

    def maybe_swap(match: re.Match[str]) -> str:
        word, (start, end) = match.group(), match.span()
        if len(word) < 3 or any(s < end and start < e for s, e in protected):
            return word
        if rng.random() >= TYPO_PROBABILITY:
            return word
        spots = [
            i for i in range(1, len(word) - 1) if word[i].lower() != word[i + 1].lower()
        ]
        if not spots:
            return word
        i = rng.choice(spots)
        return word[:i] + word[i + 1] + word[i] + word[i + 2 :]

    return re.sub(r"[A-Za-z]+", maybe_swap, text)


def unique_name(name: str, taken: set[str]) -> str:
    """The name, or a numbered copy such as "name (2).txt" when it is taken."""
    path, number, candidate = Path(name), 1, name
    while candidate in taken:
        number += 1
        candidate = f"{path.stem} ({number}){path.suffix}"
    return candidate
