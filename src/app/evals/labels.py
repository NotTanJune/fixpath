"""Decides which pairs are true matches, using the hidden truth, for the merge scoring.

label_entity() says whether a candidate part or machine is the one a mention names.
label_incident() says whether two incident records describe the same breakdown.
When the truth cannot settle a pair, the answer is None and the pair is not scored.
"""

from dataclasses import dataclass

from app.evals.match import as_date
from app.evals.truth import Truth
from app.ingest.normalize import part_key

# The field of a document entry that lists each kind of entity.
ENTITY_FIELDS = {"Part": "parts", "Asset": "assets"}


def label_entity(
    truth: Truth, node_type: str, mention: str, doc_path: str, candidate_id: str
) -> bool | None:
    """Whether the candidate is the true entity that the mention names, or None when the
    truth cannot say. It looks for the mention among the known ids and aliases. If none
    fits, it uses the only entity of that kind listed for the document."""
    key = part_key(mention)
    named = {
        node.id
        for node in truth.of_type(node_type).values()
        if key
        and key
        in {part_key(node.id), *(part_key(a) for a in node.props.get("aliases", []))}
    }
    if named:
        return candidate_id in named
    entry = truth.mentions.get(doc_path)
    listed = getattr(entry, ENTITY_FIELDS[node_type]) if entry is not None else ()
    if len(listed) == 1:
        return candidate_id == listed[0]
    return None


@dataclass(frozen=True)
class Record:
    """One incident record as read from a raw file. The machine is the id it resolved to."""

    doc_path: str
    key: str
    date: str | None
    machine: str | None
    technician: str | None
    downtime_min: int | None


def _technician(truth: Truth, text: str | None) -> str | None:
    """The full name of a technician, from a name or a short form. The technician table
    is checked first because a short form such as NA can also mean no value."""
    if text is None:
        return None
    wanted = text.strip().casefold()
    for person in truth.technicians:
        if wanted in {
            person.name.casefold(),
            *(f.casefold() for f in person.short_forms),
        }:
            return person.name
    return None


def truth_incident(truth: Truth, record: Record) -> str | None:
    """The true incident that a record describes, or None when it cannot be told.

    It starts from the incidents the record's document really mentions. If one is left,
    that is the answer. Otherwise it narrows by machine, date and technician, then picks
    the nearest downtime. A tie, or nothing to choose by, gives None."""
    entry = truth.mentions.get(record.doc_path)
    pool = list(entry.incidents) if entry is not None else []
    if len(pool) <= 1:
        return pool[0] if pool else None
    if record.machine is not None:
        pool = [i for i in pool if truth.machine_of(i) == record.machine]
    day = as_date(record.date)
    if day is not None:
        for window in (0, 1):
            near = [
                i
                for i in pool
                if (when := as_date(truth.nodes[i].props.get("date"))) is not None
                and abs((when - day).days) <= window
            ]
            if near:
                pool = near
                break
        else:
            pool = []
    person = _technician(truth, record.technician)
    if person is not None:
        named = [i for i in pool if truth.incident_details[i].technician == person]
        pool = named or pool
    if len(pool) == 1:
        return pool[0]
    if len(pool) > 1 and isinstance(record.downtime_min, (int, float)):
        gaps = sorted(
            (
                abs(
                    truth.nodes[i].props.get("downtime_min", 10**9)
                    - record.downtime_min
                ),
                i,
            )
            for i in pool
        )
        if gaps[0][0] < gaps[1][0]:
            return gaps[0][1]
    return None


def label_incident(truth: Truth, first: Record, second: Record) -> bool | None:
    """Whether two records describe one true incident, or None if either is unmapped."""
    a, b = truth_incident(truth, first), truth_incident(truth, second)
    if a is None or b is None:
        return None
    return a == b
