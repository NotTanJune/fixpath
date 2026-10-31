"""The incidents as the note writers see them, and where each one is recorded.

incidents_of() turns the world's incidents into a simple form. place() decides which
incidents go in the maintenance export and which in shift handovers, and every
incident lands in at least one. machine_ref(), alarm_ref() and part_ref() pick one of
the ways a note might write a machine, an alarm or a part.
"""

import datetime as dt
import random
from dataclasses import dataclass

from app.generator import catalogue as cat
from app.generator.world import World
from app.ontology import IncidentProps

EXPORT_PROBABILITY = 0.8
HANDOVER_PROBABILITY = 0.7


@dataclass(frozen=True)
class Incident:
    id: str
    start: dt.datetime  # SGT
    shift: str
    asset: str
    alarm: str | None
    failure_mode: str
    procedure: str  # the title of the fix
    parts: tuple[tuple[str, int], ...]  # (part number, quantity)
    downtime: int  # minutes
    symptom: str
    technician: str


def incidents_of(world: World) -> list[Incident]:
    """The incidents, oldest first."""
    props = {
        node.id: IncidentProps.model_validate(node.props)
        for node in world.nodes
        if node.type == "Incident"
    }
    link = {(edge.src, edge.rel): edge.dst for edge in world.edges if edge.src in props}
    return [
        Incident(
            id=detail.id,
            start=detail.start,
            shift=props[detail.id].shift,
            asset=link[detail.id, "OCCURRED_ON"],
            alarm=link.get((detail.id, "TRIGGERED_BY")),
            failure_mode=link[detail.id, "ROOT_CAUSE"],
            procedure=cat.PROCEDURE_BY_ID[detail.procedure_id].props.title,
            parts=tuple((part.part_no, part.qty) for part in detail.parts),
            downtime=props[detail.id].downtime_min,
            symptom=props[detail.id].symptom_text,
            technician=detail.technician,
        )
        for detail in world.incidents_detail
    ]


def place(
    rng: random.Random, incidents: list[Incident]
) -> tuple[list[Incident], list[Incident]]:
    """Split the incidents into those in the export and those in handovers.
    An incident that would land in neither goes to the export."""
    exported, handed_over = [], []
    for incident in incidents:
        to_export = rng.random() < EXPORT_PROBABILITY
        to_handover = rng.random() < HANDOVER_PROBABILITY
        if to_handover:
            handed_over.append(incident)
        if to_export or not to_handover:
            exported.append(incident)
    return exported, handed_over


def machine_ref(rng: random.Random, asset_id: str) -> str:
    return rng.choice([asset_id, *cat.ASSET_BY_ID[asset_id].aliases])


def alarm_ref(rng: random.Random, code: str) -> str:
    return rng.choice([code, *cat.ALARM_BY_CODE[code].props.aliases])


def part_ref(rng: random.Random, part_no: str) -> str:
    return rng.choice(cat.PART_BY_NO[part_no].props.aliases)
