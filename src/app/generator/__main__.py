"""Command line for the generator.

It builds the true world for a seed, then writes the messy raw files that world
produces: machine logs, shift handovers, the maintenance export, stores files and
vendor manuals. It also writes a record of what each raw file truly mentions.
main() reads the options and runs the steps. render_raw() replaces the raw folders
with a fresh render.
"""

import argparse
import random
import shutil
import sys
from pathlib import Path

from app.config import DATA_DIR, ROOT, TRUTH_DIR
from app.generator import DEFAULT_SEED
from app.generator.mentions import Mentions, merge, write_mentions
from app.generator.render_inventory import render_inventory
from app.generator.render_logs import render_logs
from app.generator.render_manuals import render_manuals
from app.generator.render_notes import render_notes
from app.generator.world import World, build_world, write_world

# The raw folders this generator owns. It clears and recreates only these.
RAW_FOLDERS = ("logs", "handovers", "maintenance", "inventory", "manuals")

# Each renderer has its own random stream, so a change to one never alters another.
RENDERERS = (
    (render_logs, 1),
    (render_notes, 2),
    (render_inventory, 3),
    (render_manuals, 4),
)


def _shown(path: Path) -> str:
    """The path relative to the repo root, when it is inside the repo."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _truth_dir(out: Path) -> Path:
    return out / TRUTH_DIR.relative_to(DATA_DIR)


def render_raw(world: World, out: Path) -> Mentions:
    """Replace the raw folders with a fresh render of the world, save what each file
    mentions, and return that record."""
    for name in RAW_FOLDERS:
        folder = out / "raw" / name
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
    mentions = merge(
        *(
            render(world, random.Random(world.seed + offset), out)
            for render, offset in RENDERERS
        )
    )
    write_mentions(mentions, _truth_dir(out) / "mentions.json")
    return mentions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.generator",
        description="Build the seeded true world and the messy raw files it produces.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="seed for every random choice (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR,
        help="data directory to write into (default: the repo data directory)",
    )
    args = parser.parse_args(argv)

    world = build_world(args.seed)
    path = write_world(world, _truth_dir(args.out) / "world.json")
    print(
        f"wrote {_shown(path)} (seed {world.seed}: {len(world.nodes)} nodes, "
        f"{len(world.edges)} edges, {len(world.alarm_events)} alarm events)"
    )
    mentions = render_raw(world, args.out)
    print(f"rendered {len(mentions)} raw files under {_shown(args.out / 'raw')}")
    print(f"wrote {_shown(_truth_dir(args.out) / 'mentions.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
