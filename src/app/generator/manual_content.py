"""The text of the vendor manuals, taken from the catalogue.

render_manuals.py turns this into PDF files. This file holds the fixed wording, the
safety paragraphs and how each cause is described. For each machine model, it
gathers the alarms, failure modes, procedures and parts the catalogue holds, and
builds the table rows from them.

A manual names its machine model, never the plant's own machine tag.

collect_model() gathers the content for one model. The row functions (alarm_row,
troubleshooting_row, used_part_row, spare_part_row) build one table row each.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from app.generator import catalogue as cat

REVISION = "Revision 3"
AUDIENCE = "For qualified maintenance personnel"
LOCKOUT_WARNING = (
    "WARNING: Apply lockout and tagout before starting. Verify zero energy."
)
NO_ISOLATION = "No isolation needed. Work from the HMI only."
NO_PARTS = "Parts required: none."
FIGURE_CAPTION = "Figure 4: seal order"
NO_ACTION = "No action required."
NO_CAUSE = "none"

# The press manual shows the order in which the seal kit goes in. It calls the kit by
# the vendor's own name, with the order number beside it.
SEAL_ORDER = ("wiper", "rod seal", "guide ring", "piston seal")
SEAL_ORDER_PROCEDURE = "PROC-HP-04"
SEAL_KIT_NO = "SEAL-KIT-50"
SEAL_KIT_NAME = "SK-50 seal kit"

# How each manual words the cause of a failure mode.
CAUSE_PHRASING = {
    "FM-SPINDLE-BEARING": "worn spindle bearings",
    "FM-COOLANT-BLOCK": "blocked coolant filter",
    "FM-TOOL-CLAMP": "weak tool clamp springs or faulty clamp sensor",
    "FM-SEAL-WEAR": "worn main cylinder seals",
    "FM-OIL-CONTAM": "contaminated hydraulic oil",
    "FM-RELIEF-DRIFT": "relief valve out of adjustment",
    "FM-GRIPPER-WEAR": "worn gripper fingers",
    "FM-SERVO-OVERLOAD": "axis 2 overload from binding or a failing drive",
    "FM-LIGHT-CURTAIN": "misaligned light curtain",
    "FM-SENSOR-DRIFT": "pressure sensor drift",
    "FM-FIXTURE-SEAL": "worn fixture seals",
}


@dataclass(frozen=True)
class ManualSpec:
    """What a manual says that the catalogue does not: the file name, the title and
    the two safety paragraphs."""

    model: str
    file_name: str
    title: str
    safety: str
    lockout: str


# fmt: off
MANUALS = (
    ManualSpec(
        "KX-500",
        "Kessler_KX-500_Operator_Maintenance_Manual_rev3.pdf",
        "Operator and Maintenance Manual",
        "Only qualified maintenance personnel who have read this manual may service "
        "the KX-500. Wear safety glasses and cut-resistant gloves when you handle "
        "tools, chips and coolant. Never run the machine with the guarding open or a "
        "safety device bypassed. Report damage to the guarding to your supervisor "
        "before you continue.",
        "Before you work inside the guarding or on the spindle, the tool changer or "
        "the coolant system, switch off the main isolator and fit your personal lock "
        "and tag. Release the compressed air to the tool changer and the drawbar, "
        "and wait until the spindle has stopped. Verify zero energy before you "
        "start, and remove your lock only when the work is finished.",
    ),
    ManualSpec(
        "BH-250",
        "BH-250 service manual.pdf",
        "Service Manual",
        "Only qualified maintenance personnel who have read this manual may service "
        "the BH-250. Keep hands and tools out of the die space while the pump runs. "
        "Fit the safety blocks under the ram before you work beneath it. Wear "
        "safety glasses and oil-resistant gloves, and clean up spilled oil at once.",
        "Before you work on the hydraulic system, stop the pump, switch off the main "
        "isolator and fit your personal lock and tag. The system keeps stored "
        "hydraulic energy in the accumulator after the pump stops, so bleed the "
        "accumulator until its gauge reads 0 bar before you open any line. Lower "
        "the ram onto blocks and verify zero energy before you start.",
    ),
    ManualSpec(
        "OR-6",
        "Orbis_OR6_cell_maintenance_guide.pdf",
        "Cell Maintenance Guide",
        "Only qualified maintenance personnel who are trained on the OR-6 may enter "
        "the cell. Never bypass the light curtain or a door interlock, and stay "
        "clear of the arm: it can move without warning while the cell has power. "
        "If anything moves unexpectedly, press the emergency stop and leave the "
        "cell by the safety door.",
        "Move the robot to the service position, switch off the cell isolator and "
        "fit your personal lock and tag. The drive capacitors keep a dangerous "
        "charge after power off, so wait 5 minutes for the drive capacitors to "
        "discharge before you touch a drive. Release the compressed air from the "
        "gripper valve block and verify zero energy before you start.",
    ),
    ManualSpec(
        "VT-LD20",
        "VT-LD20_manual_EN.pdf",
        "Operating and Maintenance Manual",
        "Only qualified maintenance personnel may service the VT-LD20. Wear safety "
        "glasses when you work near the test fixture, because the fixture is "
        "pressurised during a test. Never open the fixture while the tester is "
        "filling or a test is running.",
        "Close the compressed air supply and fit your personal lock and tag on the "
        "tester pneumatic valve. Compressed air stays in the lines and in the "
        "fixture after the supply is closed, so vent the lines and check that the "
        "gauge reads zero before you open any fitting. Verify zero energy before "
        "you start.",
    ),
)
# fmt: on

# --- What the catalogue holds for a model ---------------------------------------------


@dataclass(frozen=True)
class ModelContent:
    """The catalogue entries one manual covers. A model used on several machines has
    one manual for all of them."""

    spec: ManualSpec
    vendor: str
    machine_type: str
    assets: tuple[str, ...]
    components: frozenset[str]
    alarms: tuple[cat.AlarmSpec, ...]
    failure_modes: tuple[cat.FailureModeSpec, ...]
    procedures: tuple[cat.ProcedureSpec, ...]
    parts: tuple[cat.PartSpec, ...]  # every part that fits, by part number

    def section(self, proc_id: str) -> str:
        """The section number of a procedure in the manual."""
        ids = [proc.props.id for proc in self.procedures]
        return f"4.{ids.index(proc_id) + 1}"

    def fixes(self, failure_mode_id: str) -> list[cat.ProcedureSpec]:
        """The procedures that fix a failure mode, main fix first."""
        found = [p for p in self.procedures if p.resolves == failure_mode_id]
        return sorted(found, key=lambda proc: not proc.main_fix)

    def reference(self, proc: cat.ProcedureSpec) -> list[str]:
        """How a table cell points to a procedure: its section and title, then the
        plant's own reference."""
        props = proc.props
        return [
            f"See {self.section(props.id)} {props.title}",
            f"(plant ref {props.id})",
        ]

    def part_numbers(self) -> list[str]:
        """Every part the manual names, as spares or as used in a procedure."""
        needed = {part_no for proc in self.procedures for part_no, _ in proc.parts}
        return sorted({spec.props.part_no for spec in self.parts} | needed)


def collect_model(spec: ManualSpec) -> ModelContent:
    assets = [asset for asset in cat.ASSETS if asset.model == spec.model]
    (vendor,) = {asset.vendor for asset in assets}
    (machine_type,) = {asset.machine_type for asset in assets}
    asset_ids = {asset.id for asset in assets}
    components = frozenset(
        comp.id for comp in cat.COMPONENTS if comp.asset_id in asset_ids
    )
    failure_modes = tuple(
        fm for fm in cat.FAILURE_MODES if components.intersection(fm.affects)
    )
    resolved = {fm.props.id for fm in failure_modes}
    return ModelContent(
        spec=spec,
        vendor=vendor,
        machine_type=machine_type,
        assets=tuple(asset.id for asset in assets),
        components=components,
        alarms=tuple(a for a in cat.ALARMS if asset_ids.intersection(a.machines)),
        failure_modes=failure_modes,
        procedures=tuple(p for p in cat.PROCEDURES if p.resolves in resolved),
        parts=tuple(
            sorted(
                (p for p in cat.PARTS if components.intersection(p.fits)),
                key=lambda part: part.props.part_no,
            )
        ),
    )


# --- The rows of the tables -----------------------------------------------------------

Cell = str | Sequence[str]  # a table cell holds one line or several


def alarm_row(model: ModelContent, alarm: cat.AlarmSpec) -> list[Cell]:
    """One alarm table row. Causes run from most to least likely, and the action
    points to the fixes in the same order. An info alarm has no cause."""
    props = alarm.props
    if not alarm.indicates:
        return [props.code, props.text, props.severity, NO_CAUSE, NO_ACTION]
    ranked = sorted(alarm.indicates, key=lambda pair: -pair[1])
    labels = ("most likely", *["possible"] * (len(ranked) - 1))
    causes = [
        f"{CAUSE_PHRASING[fm_id]} ({label})"
        for (fm_id, _), label in zip(ranked, labels, strict=True)
    ]
    actions = [
        line
        for fm_id, _ in ranked
        for proc in model.fixes(fm_id)
        for line in model.reference(proc)
    ]
    return [props.code, props.text, props.severity, causes, actions]


def troubleshooting_row(
    model: ModelContent, failure_mode: cat.FailureModeSpec
) -> list[Cell]:
    """One troubleshooting row: a symptom, its cause and the fixes."""
    fm_id = failure_mode.props.id
    return [
        cat.SYMPTOMS[fm_id][0],
        CAUSE_PHRASING[fm_id],
        [line for proc in model.fixes(fm_id) for line in model.reference(proc)],
    ]


def used_part_row(part_no: str, qty: int) -> list[Cell]:
    """One row of the parts table of a procedure."""
    description = cat.PART_BY_NO[part_no].props.description
    if part_no == SEAL_KIT_NO:
        description = f"{SEAL_KIT_NAME} (order no. {part_no})"
    return [part_no, description, str(qty)]


def spare_part_row(model: ModelContent, part: cat.PartSpec) -> list[Cell]:
    """One spare parts row: part number, description and where it fits on this model."""
    names = dict.fromkeys(
        cat.COMPONENT_BY_ID[comp].name for comp in part.fits if comp in model.components
    )
    return [part.props.part_no, part.props.description, list(names)]
