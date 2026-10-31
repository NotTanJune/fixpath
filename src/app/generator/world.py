"""Build the hidden true world from the catalogue and a seed, and save it as JSON.

The world holds the machines, parts, incidents, alarm events and technicians as they
really are. The same seed always gives the same world. build_world() creates it and
write_world() saves it. The raw files the other generator modules write are messy
views of this world.
"""

import datetime as dt
import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import TRUTH_DIR
from app.generator import catalogue as cat
from app.ontology import (
    IncidentProps,
    IndicatesProps,
    RaisedProps,
    RequiresProps,
    UsedProps,
)

# --- How the incidents are generated ---------------------------------------------

INCIDENT_COUNT = 60
ALARM_START_PROBABILITY = 0.9
EXTRA_PART_PROBABILITY = 0.15
DOWNTIME_FACTOR_RANGE = (0.8, 2.5)  # multiplies the procedure's time estimate
DOWNTIME_ADDED_MINUTES_RANGE = (20, 120)

# --- How the alarm events are generated ------------------------------------------

BURST_MAX_LINES = 4  # most log lines for the alarm that starts an incident
BURST_WINDOW_SECONDS = 10 * 60  # how long after the start those lines spread
PRECURSOR_PROBABILITY = 0.5
PRECURSOR_HOURS_RANGE = (2, 48)  # how long before the incident a warning appears
BACKGROUND_PER_WEEK = 2  # unrelated warnings per machine
WINDOW_WEEKS = 26

# The first moment after the six months, in Singapore time.
WINDOW_END = cat.PERIOD_END + dt.timedelta(minutes=1)

# --- The world as data ---------------------------------------------------------


class Node(BaseModel):
    id: str
    type: str
    props: dict[str, Any]


class Edge(BaseModel):
    src: str
    rel: str
    dst: str
    props: dict[str, Any] = Field(default_factory=dict)


class Technician(BaseModel):
    name: str
    short_forms: list[str]
    shift: Literal["A", "B", "C"]


class AlarmEvent(BaseModel):
    """One alarm line on a machine. The timestamp is UTC. incident_id is set for
    the lines that belong to an incident."""

    asset: str
    code: str
    timestamp: dt.datetime
    incident_id: str | None = None


class PartUse(BaseModel):
    part_no: str
    qty: int


class IncidentDetail(BaseModel):
    """What the renderers need about an incident beyond its graph facts. start is
    an SGT time with its +08:00 offset."""

    id: str
    start: dt.datetime
    procedure_id: str
    parts: list[PartUse]
    technician: str


class World(BaseModel):
    """The hidden true world. world.json holds exactly these fields."""

    seed: int
    nodes: list[Node]
    edges: list[Edge]
    technicians: list[Technician]
    alarm_events: list[AlarmEvent]
    incidents_detail: list[IncidentDetail]
    stock_old: dict[str, int]


# --- Nodes and edges from the catalogue ----------------------------------------


def _node(node_type: str, node_id: str, props: BaseModel) -> Node:
    return Node(id=node_id, type=node_type, props=props.model_dump(mode="json"))


def _edge(src: str, rel: str, dst: str, props: BaseModel | None = None) -> Edge:
    dumped = {} if props is None else props.model_dump(mode="json")
    return Edge(src=src, rel=rel, dst=dst, props=dumped)


def _catalogue_nodes() -> list[Node]:
    nodes = [_node("Line", cat.LINE.id, cat.LINE)]
    nodes += [_node("Asset", asset.id, asset) for asset in cat.ASSETS]
    nodes += [_node("Component", comp.id, comp) for comp in cat.COMPONENTS]
    nodes += [_node("AlarmCode", spec.props.code, spec.props) for spec in cat.ALARMS]
    nodes += [
        _node("FailureMode", spec.props.id, spec.props) for spec in cat.FAILURE_MODES
    ]
    nodes += [_node("Procedure", spec.props.id, spec.props) for spec in cat.PROCEDURES]
    nodes += [_node("Part", spec.props.part_no, spec.props) for spec in cat.PARTS]
    return nodes


def _catalogue_edges() -> list[Edge]:
    edges = [_edge(cat.LINE.id, "HAS_MACHINE", asset.id) for asset in cat.ASSETS]
    edges += [_edge(comp.asset_id, "HAS_COMPONENT", comp.id) for comp in cat.COMPONENTS]
    for failure_mode in cat.FAILURE_MODES:
        edges += [
            _edge(failure_mode.props.id, "AFFECTS", comp_id)
            for comp_id in failure_mode.affects
        ]
    for procedure in cat.PROCEDURES:
        edges.append(_edge(procedure.resolves, "RESOLVED_BY", procedure.props.id))
        edges += [
            _edge(procedure.props.id, "REQUIRES", part_no, RequiresProps(qty=qty))
            for part_no, qty in procedure.parts
        ]
    for part in cat.PARTS:
        edges += [_edge(part.props.part_no, "FITS", comp_id) for comp_id in part.fits]
    return edges


def _indicates_edges(support: Mapping[tuple[str, str], int]) -> list[Edge]:
    """One link per alarm and failure mode pairing in the catalogue. Support counts
    the incidents that began with that alarm and had that cause."""
    return [
        _edge(
            spec.props.code,
            "INDICATES",
            failure_mode,
            IndicatesProps(
                likelihood=likelihood,
                support=support.get((spec.props.code, failure_mode), 0),
            ),
        )
        for spec in cat.ALARMS
        for failure_mode, likelihood in spec.indicates
    ]


# --- Incidents -----------------------------------------------------------------

_MAIN_FIX = {spec.resolves: spec for spec in cat.PROCEDURES if spec.main_fix}


@dataclass(frozen=True)
class _Incident:
    id: str
    start: dt.datetime  # Singapore time
    asset_id: str
    failure_mode_id: str
    alarm_code: str | None
    procedure: cat.ProcedureSpec
    parts: tuple[tuple[str, int], ...]
    downtime_min: int
    technician: str
    symptom_text: str

    @property
    def shift(self) -> Literal["A", "B", "C"]:
        return cat.shift_for_hour(self.start.hour)

    @property
    def resolution_text(self) -> str:
        used = ", ".join(f"{part_no} x{qty}" for part_no, qty in self.parts)
        title, proc_id = self.procedure.props.title, self.procedure.props.id
        return f"Done {title} ({proc_id}), used {used or 'no parts'}"


def _weighted_choice(rng: random.Random, weights: Sequence[tuple[str, float]]) -> str:
    names = [name for name, _ in weights]
    return rng.choices(names, weights=[weight for _, weight in weights])[0]


def _round_to_5(minutes: float) -> int:
    return 5 * int(minutes / 5 + 0.5)


def affected_component(failure_mode_id: str, asset_id: str) -> str:
    """The one component of the machine that the failure mode damages. Any other
    count is a mistake in the catalogue, so it raises an error."""
    found = [
        comp
        for comp in cat.FAILURE_MODE_BY_ID[failure_mode_id].affects
        if cat.COMPONENT_BY_ID[comp].asset_id == asset_id
    ]
    if len(found) != 1:
        raise ValueError(
            f"{failure_mode_id} must affect exactly one component of {asset_id}, "
            f"but it affects {', '.join(found) if found else 'none'}"
        )
    return found[0]


def _alarms_on(
    asset_id: str,
    *,
    severity: Literal["info", "warning", "critical"] | None = None,
    failure_mode_id: str | None = None,
) -> list[cat.AlarmSpec]:
    """The alarms a machine can raise, optionally only one severity or only those that
    point to one failure mode."""
    return [
        spec
        for spec in cat.ALARMS
        if asset_id in spec.machines
        and (severity is None or spec.props.severity == severity)
        and (
            failure_mode_id is None
            or any(fm == failure_mode_id for fm, _ in spec.indicates)
        )
    ]


def _incident_starts(rng: random.Random) -> list[dt.datetime]:
    """Random start times spread evenly over the six months, oldest first."""
    last_minute = int((cat.PERIOD_END - cat.PERIOD_START) / dt.timedelta(minutes=1))
    return sorted(
        cat.PERIOD_START + dt.timedelta(minutes=rng.randint(0, last_minute))
        for _ in range(INCIDENT_COUNT)
    )


def _make_incident(rng: random.Random, number: int, start: dt.datetime) -> _Incident:
    asset_id = _weighted_choice(rng, cat.MACHINE_WEIGHTS)
    failure_mode_id = _weighted_choice(rng, cat.FAILURE_MODE_WEIGHTS[asset_id])

    alarm_code = None
    if rng.random() < ALARM_START_PROBABILITY:
        indicating = [
            (spec.props.code, dict(spec.indicates)[failure_mode_id])
            for spec in _alarms_on(asset_id, failure_mode_id=failure_mode_id)
        ]
        alarm_code = _weighted_choice(rng, indicating)

    procedure = _MAIN_FIX[failure_mode_id]
    parts = list(procedure.parts)
    if rng.random() < EXTRA_PART_PROBABILITY:
        # Add one more part that fits the damaged component, other than the parts
        # the procedure already uses.
        component = affected_component(failure_mode_id, asset_id)
        used = {part_no for part_no, _ in parts}
        candidates = [
            spec.props.part_no
            for spec in cat.PARTS
            if component in spec.fits and spec.props.part_no not in used
        ]
        parts.append((rng.choice(candidates), 1))

    factor = rng.uniform(*DOWNTIME_FACTOR_RANGE)
    added = rng.uniform(*DOWNTIME_ADDED_MINUTES_RANGE)
    downtime_min = _round_to_5(procedure.props.est_minutes * factor + added)

    shift = cat.shift_for_hour(start.hour)
    crew = [tech.name for tech in cat.TECHNICIANS if tech.shift == shift]
    technician = rng.choice(crew)
    symptom_text = rng.choice(cat.SYMPTOMS[failure_mode_id])

    return _Incident(
        id=f"INC-2026-{number:03d}",
        start=start,
        asset_id=asset_id,
        failure_mode_id=failure_mode_id,
        alarm_code=alarm_code,
        procedure=procedure,
        parts=tuple(parts),
        downtime_min=downtime_min,
        technician=technician,
        symptom_text=symptom_text,
    )


def _generate_incidents(rng: random.Random) -> list[_Incident]:
    return [
        _make_incident(rng, number, start)
        for number, start in enumerate(_incident_starts(rng), start=1)
    ]


def _incident_nodes(incidents: Sequence[_Incident]) -> list[Node]:
    return [
        _node(
            "Incident",
            incident.id,
            IncidentProps(
                id=incident.id,
                date=incident.start.date(),
                shift=incident.shift,
                downtime_min=incident.downtime_min,
                symptom_text=incident.symptom_text,
                technician=incident.technician,
                resolution_text=incident.resolution_text,
            ),
        )
        for incident in incidents
    ]


def _incident_edges(incidents: Sequence[_Incident]) -> list[Edge]:
    edges: list[Edge] = []
    for incident in incidents:
        edges.append(_edge(incident.id, "OCCURRED_ON", incident.asset_id))
        if incident.alarm_code is not None:
            edges.append(_edge(incident.id, "TRIGGERED_BY", incident.alarm_code))
        edges.append(_edge(incident.id, "ROOT_CAUSE", incident.failure_mode_id))
        edges += [
            _edge(incident.id, "USED", part_no, UsedProps(qty=qty))
            for part_no, qty in incident.parts
        ]
    return edges


def _incident_details(incidents: Sequence[_Incident]) -> list[IncidentDetail]:
    return [
        IncidentDetail(
            id=incident.id,
            start=incident.start,
            procedure_id=incident.procedure.props.id,
            parts=[
                PartUse(part_no=part_no, qty=qty)
                for part_no, qty in sorted(incident.parts)
            ],
            technician=incident.technician,
        )
        for incident in incidents
    ]


# --- Alarm events --------------------------------------------------------------


def _event(
    asset_id: str, code: str, moment: dt.datetime, incident_id: str | None
) -> AlarmEvent:
    return AlarmEvent(
        asset=asset_id,
        code=code,
        timestamp=moment.astimezone(dt.timezone.utc),
        incident_id=incident_id,
    )


def _poisson(rng: random.Random, mean: float) -> int:
    """A random count with the given average, using Knuth's method."""
    limit = math.exp(-mean)
    count, product = 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def _incident_events(
    rng: random.Random, incidents: Sequence[_Incident]
) -> list[AlarmEvent]:
    """The alarm lines around each incident that has an alarm. The alarm repeats a few
    times in the first minutes, and sometimes an earlier warning comes hours before.
    Lines outside the six months are left out."""
    events: list[AlarmEvent] = []
    for incident in incidents:
        if incident.alarm_code is None:
            continue
        lines = rng.randint(1, BURST_MAX_LINES)
        offsets = [0, *rng.sample(range(1, BURST_WINDOW_SECONDS + 1), lines - 1)]
        for offset in sorted(offsets):
            moment = incident.start + dt.timedelta(seconds=offset)
            if moment < WINDOW_END:
                events.append(
                    _event(incident.asset_id, incident.alarm_code, moment, incident.id)
                )

        warnings = [
            spec.props.code
            for spec in _alarms_on(
                incident.asset_id,
                severity="warning",
                failure_mode_id=incident.failure_mode_id,
            )
        ]
        if rng.random() < PRECURSOR_PROBABILITY and warnings:
            code = rng.choice(warnings)
            low, high = (hours * 3600 for hours in PRECURSOR_HOURS_RANGE)
            seconds = round(rng.uniform(low, high))
            moment = incident.start - dt.timedelta(seconds=seconds)
            if moment >= cat.PERIOD_START:
                events.append(_event(incident.asset_id, code, moment, incident.id))
    return events


def _background_events(rng: random.Random) -> list[AlarmEvent]:
    """Unrelated warnings on each machine, about two a week, spread over the six
    months."""
    window_seconds = int((WINDOW_END - cat.PERIOD_START).total_seconds())
    events: list[AlarmEvent] = []
    for asset in cat.ASSETS:
        warnings = [
            spec.props.code for spec in _alarms_on(asset.id, severity="warning")
        ]
        for _ in range(_poisson(rng, BACKGROUND_PER_WEEK * WINDOW_WEEKS)):
            moment = cat.PERIOD_START + dt.timedelta(
                seconds=rng.randrange(window_seconds)
            )
            events.append(_event(asset.id, rng.choice(warnings), moment, None))
    return events


def _info_events() -> list[AlarmEvent]:
    """Routine info alarms, such as the daily warm-up. Each fires once a day on every
    machine that raises it."""
    events: list[AlarmEvent] = []
    day = cat.PERIOD_START.date()
    while day <= cat.PERIOD_END.date():
        for spec in cat.ALARMS:
            if spec.props.severity != "info":
                continue
            moment = dt.datetime.combine(
                day, cat.INFO_SCHEDULE[spec.props.code], tzinfo=cat.SGT
            )
            events += [
                _event(asset_id, spec.props.code, moment, None)
                for asset_id in spec.machines
            ]
        day += dt.timedelta(days=1)
    return events


def _raised_edges(events: Sequence[AlarmEvent]) -> list[Edge]:
    """One link for each machine and alarm code that has events, with a count and
    the first and last time seen."""
    stamps: dict[tuple[str, str], list[dt.datetime]] = {}
    for event in events:
        stamps.setdefault((event.asset, event.code), []).append(event.timestamp)
    return [
        _edge(
            asset_id,
            "RAISED",
            code,
            RaisedProps(count=len(times), first_seen=min(times), last_seen=max(times)),
        )
        for (asset_id, code), times in stamps.items()
    ]


# --- Build and write -----------------------------------------------------------


def build_world(seed: int) -> World:
    """Build the world for a seed. The same seed always gives the same world, with
    every list in a fixed order."""
    rng = random.Random(seed)
    incidents = _generate_incidents(rng)
    support = Counter(
        (incident.alarm_code, incident.failure_mode_id)
        for incident in incidents
        if incident.alarm_code is not None
    )

    events = _incident_events(rng, incidents) + _background_events(rng) + _info_events()

    nodes = _catalogue_nodes() + _incident_nodes(incidents)
    edges = (
        _catalogue_edges()
        + _indicates_edges(support)
        + _incident_edges(incidents)
        + _raised_edges(events)
    )
    technicians = [
        Technician(name=tech.name, short_forms=list(tech.short_forms), shift=tech.shift)
        for tech in cat.TECHNICIANS
    ]
    return World(
        seed=seed,
        nodes=sorted(nodes, key=lambda node: (node.type, node.id)),
        edges=sorted(edges, key=lambda edge: (edge.src, edge.rel, edge.dst)),
        technicians=sorted(technicians, key=lambda tech: tech.name),
        alarm_events=sorted(
            events,
            key=lambda event: (
                event.timestamp,
                event.asset,
                event.code,
                event.incident_id or "",
            ),
        ),
        incidents_detail=sorted(
            _incident_details(incidents), key=lambda record: record.id
        ),
        stock_old={spec.props.part_no: spec.qty_old for spec in cat.PARTS},
    )


def world_to_json(world: World) -> str:
    """The world as stable, readable JSON text."""
    text = json.dumps(
        world.model_dump(mode="json"), sort_keys=True, indent=2, ensure_ascii=False
    )
    return text + "\n"


def write_world(world: World, path: Path = TRUTH_DIR / "world.json") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(world_to_json(world), encoding="utf-8", newline="\n")
    return path
