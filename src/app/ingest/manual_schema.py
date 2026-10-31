"""Defines what the model returns when it reads a service manual, and the prompt for it.

ManualExtraction is the answer: the machine, its components, root causes, alarms,
procedures and spare parts. MANUAL_SYSTEM is the instruction text sent to the model.
"""

from typing import Literal

from pydantic import BaseModel

CONFIDENCE = 0.9  # a vendor manual, with quotes taken from its own text
PURPOSE = "extract manual"
EFFORT = "medium"
LIKELIHOOD = {"most likely": 0.7, "possible": 0.3}  # the manual's prior for a cause
CODE_LENGTH = 8


class ComponentItem(BaseModel):
    """A part of the machine that can fail, and a short code the model picks."""

    name: str
    code: str


class FailureModeItem(BaseModel):
    """A root cause: the phrase as the manual writes it, a short name for it, the
    component it damages and the procedures that the troubleshooting table gives as
    its remedy."""

    phrase: str
    short_name: str
    component_name: str
    procedure_refs: list[str]


class CauseItem(BaseModel):
    """One likely cause of an alarm, and how likely the manual says it is."""

    phrase: str
    rank: Literal["most likely", "possible"]


class AlarmItem(BaseModel):
    """A row of the alarm table."""

    code: str
    message: str
    severity: Literal["info", "warning", "critical"]
    causes: list[CauseItem]
    procedure_ref: str | None


class PartRequirement(BaseModel):
    """A part that a procedure needs, from the order table under it."""

    part_no: str
    description: str
    qty: int


class ProcedureItem(BaseModel):
    """A maintenance procedure of section 4."""

    plant_ref: str
    title: str
    requires_loto: bool
    minutes: int
    steps: list[str]
    parts: list[PartRequirement]


class SparePartItem(BaseModel):
    """A row of the spare parts list. fits holds the component names as written."""

    part_no: str
    description: str
    fits: list[str]


class ManualExtraction(BaseModel):
    """What the model reads from one manual."""

    vendor: str
    model: str
    components: list[ComponentItem]
    failure_modes: list[FailureModeItem]
    alarms: list[AlarmItem]
    procedures: list[ProcedureItem]
    spare_parts: list[SparePartItem]


MANUAL_SYSTEM = f"""\
You read the service manual of one machine from a precision machining plant. The text
has page markers such as [page 3]. A manual has a cover page with the vendor and the
model, an alarm table, a troubleshooting table, procedures with their order tables,
and a spare parts list.

Copy part numbers, alarm codes, plant references and phrases exactly as they are
written in the manual. Never correct or reword them. Never make up a value that the
manual does not state.

vendor and model are on the cover page. components are the parts of the machine that
can fail: the distinct values of the Fits column of the spare parts list, named as
written. Give each a code of at most {CODE_LENGTH} upper case letters and digits that
you pick, such as SPINDLE, CYL or J2.

failure_modes are the root causes. Take one for each distinct probable cause of the
troubleshooting table. phrase is the cause exactly as the table writes it, without
the words (most likely) or (possible). short_name is a short noun phrase for it, such
as "spindle bearing wear". component_name is the component it damages, written as in
components. procedure_refs holds the plant references of the remedies that the
troubleshooting table lists for that cause, in order.

alarms holds every row of the alarm table. code is the code as written. message and
severity are as written. causes holds each likely cause of that alarm, in the order
listed: phrase is the cause exactly as written, and rank is "most likely" for a cause
marked (most likely) and "possible" for a cause marked (possible). An alarm with the
cause "none" has no causes. procedure_ref is the plant reference of the first action
of the alarm, or null when the action is "No action required".

procedures holds every procedure of section 4. plant_ref is the plant reference,
requires_loto says whether lockout is required, minutes is the time, steps are the
numbered steps without their numbers, and parts are the rows of the order table under
the procedure, with the quantity. A procedure with "Parts required: none" has no
parts.

spare_parts holds every row of the spare parts list. fits holds each component name
on the row, as written.\
"""
