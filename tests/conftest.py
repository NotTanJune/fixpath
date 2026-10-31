"""Shared support for the tests of the data generator.

FixedRandom forces every coin flip to land the same way. The world fixture is the world
the repo ships, and the incident and note fixtures are built from it for the tests that
read handovers and the maintenance export.
"""

import datetime as dt
import random
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest

from app.generator import DEFAULT_SEED, catalogue
from app.generator.render_notes import render_notes
from app.generator.world import World, build_world

SEED = DEFAULT_SEED  # the content tests use the shipped world
EXPORT_NAME = "maint_log_export_2026.csv"
GRIPPER_PART = "GRP-FNG-12"


class FixedRandom(random.Random):
    """A generator that returns one fixed number, so a test can force every coin flip.
    It can also return a few chosen numbers first. Picks and shuffles still vary."""

    _randbelow = random.Random._randbelow_with_getrandbits

    def __init__(self, value: float, first=(), seed: int = 0):
        super().__init__(seed)
        self.value = value
        self._first = iter(first)

    def random(self) -> float:
        return next(self._first, self.value)


@pytest.fixture(scope="module")
def world() -> World:
    return build_world(SEED)


ALARM = {spec.props.code: spec.props for spec in catalogue.ALARMS}
ASSET_REFS = {asset.id: [asset.id, *asset.aliases] for asset in catalogue.ASSETS}
ASSET_OF_REF = {ref: asset for asset, refs in ASSET_REFS.items() for ref in refs}
ALARM_VARIANTS = {code: [code, *props.aliases] for code, props in ALARM.items()}
PART = {spec.props.part_no: spec.props for spec in catalogue.PARTS}
PART_OF_ALIAS = {
    alias: part_no for part_no, props in PART.items() for alias in props.aliases
}


@dataclass(frozen=True)
class Truth:
    """One incident as the world records it."""

    id: str
    start: dt.datetime
    shift: str
    asset: str
    alarm: str | None
    failure_mode: str
    symptom: str
    downtime: int
    technician: str
    procedure: str
    parts: dict[str, int]

    @property
    def slot(self) -> tuple[dt.date, str]:
        return self.start.date(), self.shift


def incident_truths(world: World) -> dict[str, Truth]:
    nodes = {node.id: node for node in world.nodes if node.type == "Incident"}
    edge = {(e.src, e.rel): e.dst for e in world.edges if e.src in nodes}
    titles = {spec.props.id: spec.props.title for spec in catalogue.PROCEDURES}
    return {
        detail.id: Truth(
            id=detail.id,
            start=detail.start,
            shift=nodes[detail.id].props["shift"],
            asset=edge[detail.id, "OCCURRED_ON"],
            alarm=edge.get((detail.id, "TRIGGERED_BY")),
            failure_mode=edge[detail.id, "ROOT_CAUSE"],
            symptom=nodes[detail.id].props["symptom_text"],
            downtime=nodes[detail.id].props["downtime_min"],
            technician=detail.technician,
            procedure=titles[detail.procedure_id],
            parts={part.part_no: part.qty for part in detail.parts},
        )
        for detail in world.incidents_detail
    }


@pytest.fixture(scope="module")
def truth(world) -> dict[str, Truth]:
    return incident_truths(world)


@pytest.fixture(scope="module")
def notes(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("notes")
    return out, render_notes(world, random.Random(SEED + 2), out)


def render_notes_with(world: World, value: float, out: Path):
    return out, render_notes(world, FixedRandom(value), out)


def descents(keys) -> int:
    """Count the places where a key is followed by a smaller one."""
    return sum(1 for first, second in pairwise(keys) if first > second)


def repl(title: str) -> str:
    """A shorthand version of a procedure title."""
    return (
        title.replace("Replace", "Repl")
        .replace("Check", "Chk")
        .replace("bearing", "brg")
        .replace(" and ", " & ")
    )
