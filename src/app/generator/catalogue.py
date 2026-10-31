"""The hand-written facts about Line 3 of a fictional Singapore machining plant.

This file lists the machines and their parts, the failure modes, alarm codes, repair
procedures, spare parts and technicians. It also holds the odds for which machine
and which failure an incident gets. world.py builds the random world from these
tables and never invents a new machine, part or alarm. The small spec classes add
links the basic models lack, such as which parts a failure mode damages.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.ontology import (
    AlarmCodeProps,
    AssetProps,
    ComponentProps,
    FailureModeProps,
    LineProps,
    PartProps,
    ProcedureProps,
)

# --- Time ----------------------------------------------------------------------

# Singapore time is always 8 hours ahead of UTC. A fixed offset avoids relying on the
# computer's time zone data.
SGT = dt.timezone(dt.timedelta(hours=8), "SGT")

# The six months the world covers. PERIOD_END is the last minute an incident can
# begin.
PERIOD_START = dt.datetime(2026, 4, 1, 0, 0, tzinfo=SGT)
PERIOD_END = dt.datetime(2026, 9, 30, 23, 59, tzinfo=SGT)

STOCK_AS_OF = dt.date(2026, 10, 15)
STOCK_OLD_AS_OF = dt.date(2026, 6, 30)


def shift_for_hour(hour: int) -> Literal["A", "B", "C"]:
    """The shift on duty at a given hour of Singapore time. A is 07:00 to 15:00, B is
    15:00 to 23:00 and C is 23:00 to 07:00."""
    if 7 <= hour < 15:
        return "A"
    if 15 <= hour < 23:
        return "B"
    return "C"


# --- Specs: an ontology property model plus the relationships around it ---------


@dataclass(frozen=True)
class FailureModeSpec:
    props: FailureModeProps
    affects: tuple[str, ...]  # the components it damages


@dataclass(frozen=True)
class AlarmSpec:
    props: AlarmCodeProps
    machines: tuple[str, ...]  # machines that can raise the code
    indicates: tuple[tuple[str, float], ...]  # failure modes it points to, with odds


@dataclass(frozen=True)
class ProcedureSpec:
    props: ProcedureProps
    resolves: str  # the failure mode it fixes
    main_fix: bool  # false for a quick check
    parts: tuple[tuple[str, int], ...]  # part numbers with quantities


@dataclass(frozen=True)
class PartSpec:
    props: PartProps
    fits: tuple[str, ...]  # the components it fits
    qty_old: int  # quantity in the June stock count


@dataclass(frozen=True)
class TechnicianSpec:
    name: str
    first_name: str  # what people call them, as used in file names
    short_forms: tuple[str, ...]
    shift: Literal["A", "B", "C"]


# --- Line, machines and components ---------------------------------------------

LINE = LineProps(id="LINE-03", name="Line 3, brake caliper housings")

# fmt: off
ASSETS = (
    AssetProps(
        id="CNC-01", name="Machining centre 1", machine_type="5-axis machining centre",
        vendor="Kessler Werkzeugmaschinen", model="KX-500",
        aliases=["CNC1", "CNC 1", "MC-1", "Kessler 1"],
    ),
    AssetProps(
        id="CNC-02", name="Machining centre 2", machine_type="5-axis machining centre",
        vendor="Kessler Werkzeugmaschinen", model="KX-500",
        aliases=["CNC2", "CNC 2", "MC-2", "Kessler 2"],
    ),
    # "Press 2" is deliberate: the press was press number 2 on the old line.
    AssetProps(
        id="HP-01", name="Hydraulic press", machine_type="250 t hydraulic press",
        vendor="Bramwell Hydraulics", model="BH-250",
        aliases=["Press 2", "HP1", "hyd press", "the press", "PR-2"],
    ),
    AssetProps(
        id="RW-01", name="Robot workcell",
        machine_type="Load, unload and deburr robot cell",
        vendor="Orbis Robotics", model="OR-6",
        aliases=["robot cell", "RC-1", "robot", "deburr cell"],
    ),
    AssetProps(
        id="LT-01", name="Leak tester", machine_type="Pressure-decay leak tester",
        vendor="Veltra Test Systems", model="VT-LD20",
        aliases=["leak tester", "LT1", "leak test", "VT station"],
    ),
)

COMPONENTS = (
    *(
        ComponentProps(id=f"{asset}-{suffix}", name=name, asset_id=asset)
        for asset in ("CNC-01", "CNC-02")
        for suffix, name in (
            ("SPINDLE", "Spindle"),
            ("COOLANT", "Coolant system"),
            ("ATC", "Tool changer"),
        )
    ),
    ComponentProps(id="HP-01-PUMP", name="Hydraulic pump", asset_id="HP-01"),
    ComponentProps(id="HP-01-CYL", name="Main cylinder and seals", asset_id="HP-01"),
    ComponentProps(id="HP-01-VALVE", name="Pressure relief valve", asset_id="HP-01"),
    ComponentProps(id="RW-01-GRIPPER", name="Gripper", asset_id="RW-01"),
    ComponentProps(id="RW-01-J2", name="Axis 2 servo", asset_id="RW-01"),
    ComponentProps(id="RW-01-SAFETY", name="Light curtain", asset_id="RW-01"),
    ComponentProps(id="LT-01-SENSOR", name="Pressure sensor", asset_id="LT-01"),
    ComponentProps(id="LT-01-FIXTURE", name="Test fixture and seals", asset_id="LT-01"),
    ComponentProps(id="LT-01-VALVE", name="Fill valve", asset_id="LT-01"),
)
# fmt: on

# The serial number of each machine, as shown in its log header and the stores
# machine list.
SERIALS: dict[str, str] = {
    "CNC-01": "KX5-10442",
    "CNC-02": "KX5-10457",
    "HP-01": "BH250-0091",
    "RW-01": "OR6-2231",
    "LT-01": "VT20-5512",
}


def _cnc(suffix: str) -> tuple[str, str]:
    """The same component on both machining centres."""
    return (f"CNC-01-{suffix}", f"CNC-02-{suffix}")


# --- Failure modes --------------------------------------------------------------


def _failure_mode(
    fm_id: str, name: str, mechanism: str, affects: tuple[str, ...]
) -> FailureModeSpec:
    return FailureModeSpec(
        props=FailureModeProps(id=fm_id, name=name, mechanism=mechanism),
        affects=affects,
    )


# fmt: off
FAILURE_MODES = (
    _failure_mode("FM-SPINDLE-BEARING", "Spindle bearing wear", "Wear", _cnc("SPINDLE")),
    _failure_mode("FM-COOLANT-BLOCK", "Coolant filter blocked", "Blockage", _cnc("COOLANT")),
    _failure_mode("FM-TOOL-CLAMP", "Tool clamp failure", "Mechanical failure", _cnc("ATC")),
    _failure_mode("FM-SEAL-WEAR", "Cylinder seal wear", "Wear", ("HP-01-CYL",)),
    _failure_mode("FM-OIL-CONTAM", "Hydraulic oil contamination", "Contamination", ("HP-01-PUMP",)),
    _failure_mode("FM-RELIEF-DRIFT", "Relief valve drift", "Out of adjustment", ("HP-01-VALVE",)),
    _failure_mode("FM-GRIPPER-WEAR", "Gripper finger wear", "Wear", ("RW-01-GRIPPER",)),
    _failure_mode("FM-SERVO-OVERLOAD", "Axis servo overload", "Overheating", ("RW-01-J2",)),
    _failure_mode("FM-LIGHT-CURTAIN", "Light curtain misalignment", "Misalignment", ("RW-01-SAFETY",)),
    _failure_mode("FM-SENSOR-DRIFT", "Pressure sensor drift", "Calibration drift", ("LT-01-SENSOR",)),
    _failure_mode("FM-FIXTURE-SEAL", "Test fixture seal leak", "Leakage", ("LT-01-FIXTURE",)),
)
# fmt: on

# Two ways to describe the symptom of each failure mode.
SYMPTOMS: dict[str, tuple[str, str]] = {
    "FM-SPINDLE-BEARING": (
        "spindle running hot and noisy at high rpm",
        "grinding noise from spindle, vibration alarm",
    ),
    "FM-COOLANT-BLOCK": (
        "coolant pressure dropping, parts coming out hot",
        "no coolant at the nozzle, chips packing in the bore",
    ),
    "FM-TOOL-CLAMP": (
        "tool not clamping after change, machine stops",
        "tool change takes too long then faults",
    ),
    "FM-SEAL-WEAR": (
        "oil leaking under the ram, pressure dropping",
        "press losing pressure mid-stroke, oil on the floor",
    ),
    "FM-OIL-CONTAM": (
        "oil looks dark and milky, pump noisy",
        "pressure hunting, oil running hot",
    ),
    "FM-RELIEF-DRIFT": (
        "press cannot reach tonnage, relief valve hissing",
        "pressure spikes above setpoint then drops",
    ),
    "FM-GRIPPER-WEAR": (
        "robot dropping parts at the deburr station",
        "part-present signal flickering, gripper slipping",
    ),
    "FM-SERVO-OVERLOAD": (
        "axis 2 trips on overcurrent during fast moves",
        "robot arm jerky on axis 2, drive alarm",
    ),
    "FM-LIGHT-CURTAIN": (
        "cell stops randomly, light curtain fault",
        "muting fault after the guard was knocked",
    ),
    "FM-SENSOR-DRIFT": (
        "good parts failing leak test, reference off",
        "master part reading out of tolerance",
    ),
    "FM-FIXTURE-SEAL": (
        "leak rate high on every part, fixture hissing",
        "fill takes too long, seals look flattened",
    ),
}

# --- Alarm codes ----------------------------------------------------------------


def alarm_aliases(code: str, machines: Sequence[str]) -> list[str]:
    """The other ways a note or log might write an alarm code. Every code has a
    dashless form, and some machines have extra forms of their own."""
    prefix, number = code.split("-")
    aliases = [prefix + number]
    if "HP-01" in machines:
        aliases += [f"ERR {number}", f"Err:{number}"]
    if any(machine.startswith("CNC-") for machine in machines):
        aliases.append(f"ALM {code}")
    if "LT-01" in machines:
        aliases.append(f"{prefix}{int(number)}")
    return list(dict.fromkeys(aliases))


def _alarm(
    code: str,
    text: str,
    severity: Literal["info", "warning", "critical"],
    machines: tuple[str, ...],
    indicates: tuple[tuple[str, float], ...] = (),
) -> AlarmSpec:
    return AlarmSpec(
        props=AlarmCodeProps(
            code=code,
            text=text,
            severity=severity,
            aliases=alarm_aliases(code, machines),
        ),
        machines=machines,
        indicates=indicates,
    )


_CNCS = ("CNC-01", "CNC-02")
_PRESS = ("HP-01",)
_ROBOT = ("RW-01",)
_TESTER = ("LT-01",)

# fmt: off
ALARMS = (
    _alarm("SP-001", "Warm-up cycle complete", "info", _CNCS),
    _alarm("SP-104", "Spindle temperature high", "critical", _CNCS,
           (("FM-SPINDLE-BEARING", 0.8), ("FM-COOLANT-BLOCK", 0.2))),
    _alarm("SP-117", "Spindle vibration above limit", "warning", _CNCS,
           (("FM-SPINDLE-BEARING", 0.9),)),
    _alarm("CL-210", "Coolant pressure low", "warning", _CNCS,
           (("FM-COOLANT-BLOCK", 0.85),)),
    _alarm("CL-214", "Coolant flow interrupted", "critical", _CNCS,
           (("FM-COOLANT-BLOCK", 0.9),)),
    _alarm("TC-305", "Tool clamp not confirmed", "critical", _CNCS,
           (("FM-TOOL-CLAMP", 0.85),)),
    _alarm("TC-311", "Tool change timeout", "warning", _CNCS,
           (("FM-TOOL-CLAMP", 0.6),)),
    _alarm("E-200", "Pump start", "info", _PRESS),
    _alarm("E-217", "Main cylinder pressure loss", "critical", _PRESS,
           (("FM-SEAL-WEAR", 0.73), ("FM-RELIEF-DRIFT", 0.2))),
    _alarm("E-221", "Hydraulic oil temperature high", "warning", _PRESS,
           (("FM-OIL-CONTAM", 0.6), ("FM-RELIEF-DRIFT", 0.3))),
    _alarm("E-230", "Pump pressure unstable", "warning", _PRESS,
           (("FM-OIL-CONTAM", 0.7),)),
    _alarm("E-245", "Oil level low", "warning", _PRESS,
           (("FM-SEAL-WEAR", 0.6),)),
    _alarm("E-252", "Relief valve pressure out of range", "critical", _PRESS,
           (("FM-RELIEF-DRIFT", 0.85),)),
    _alarm("RB-551", "Gripper part-present signal lost", "warning", _ROBOT,
           (("FM-GRIPPER-WEAR", 0.8),)),
    _alarm("RB-560", "Axis 2 overcurrent", "critical", _ROBOT,
           (("FM-SERVO-OVERLOAD", 0.85),)),
    _alarm("RB-563", "Axis 2 following error", "warning", _ROBOT,
           (("FM-SERVO-OVERLOAD", 0.7),)),
    _alarm("RB-570", "Light curtain muting fault", "critical", _ROBOT,
           (("FM-LIGHT-CURTAIN", 0.8),)),
    _alarm("VT-012", "Reference pressure out of tolerance", "warning", _TESTER,
           (("FM-SENSOR-DRIFT", 0.75),)),
    _alarm("VT-018", "Leak rate above limit on master part", "critical", _TESTER,
           (("FM-FIXTURE-SEAL", 0.7), ("FM-SENSOR-DRIFT", 0.25))),
    _alarm("VT-021", "Fill time exceeded", "warning", _TESTER,
           (("FM-FIXTURE-SEAL", 0.6),)),
)
# fmt: on

# The time of day each daily info alarm fires, in Singapore time.
INFO_SCHEDULE: dict[str, dt.time] = {
    "SP-001": dt.time(6, 30),
    "E-200": dt.time(6, 45),
}

# --- Procedures -----------------------------------------------------------------


def _procedure(
    proc_id: str,
    title: str,
    resolves: str,
    *,
    loto: bool,
    minutes: int,
    parts: tuple[tuple[str, int], ...],
    steps: tuple[str, ...],
    main_fix: bool = True,
) -> ProcedureSpec:
    return ProcedureSpec(
        props=ProcedureProps(
            id=proc_id,
            title=title,
            steps=list(steps),
            requires_loto=loto,
            est_minutes=minutes,
        ),
        resolves=resolves,
        main_fix=main_fix,
        parts=parts,
    )


# fmt: off
PROCEDURES = (
    _procedure(
        "PROC-CNC-01", "Replace spindle bearing set", "FM-SPINDLE-BEARING",
        loto=True, minutes=240,
        parts=(("BRG-7014-ACD", 2), ("SEAL-SPDL-70", 1), ("GREASE-KLB-2", 1)),
        steps=(
            "Stop the machine, apply lockout and tagout on the main isolator, and verify zero energy.",
            "Remove the spindle cover and disconnect the drawbar and coolant lines.",
            "Extract the spindle cartridge with the puller and mount it on the bench.",
            "Press out both bearings and fit the new BRG-7014-ACD pair with the spacer.",
            "Fit a new SEAL-SPDL-70 front seal and pack the bearings with GREASE-KLB-2.",
            "Refit the cartridge, run the 20-minute warm-up cycle and check that vibration is below 2.5 mm/s.",
        ),
    ),
    # This one is a quick check, not the main fix.
    _procedure(
        "PROC-CNC-02", "Check spindle vibration from the HMI", "FM-SPINDLE-BEARING",
        loto=False, minutes=20, parts=(), main_fix=False,
        steps=(
            "Open Diagnostics, then Spindle, on the HMI and start the vibration test at 6000 rpm.",
            "Record the vibration reading and the bearing temperature.",
            "If vibration is above 2.5 mm/s or the temperature is above 65 C, plan PROC-CNC-01.",
            "Log the readings in the shift handover.",
        ),
    ),
    _procedure(
        "PROC-CNC-03", "Clean coolant filter and flush lines", "FM-COOLANT-BLOCK",
        loto=True, minutes=45, parts=(("FLT-CL-25", 1),),
        steps=(
            "Stop the coolant pump and apply lockout and tagout on the coolant pump breaker.",
            "Drain the filter housing into the waste tray.",
            "Replace the filter element with FLT-CL-25 and clean the housing.",
            "Flush the lines at low pressure for 5 minutes and check for leaks.",
            "Remove the lockout, restart the pump and confirm pressure above 18 bar.",
        ),
    ),
    _procedure(
        "PROC-CNC-04", "Replace tool clamp disc springs", "FM-TOOL-CLAMP",
        loto=True, minutes=120, parts=(("SPR-TC-40", 1), ("SENS-PROX-08", 1)),
        steps=(
            "Stop the machine, apply lockout and tagout, and release air pressure to the tool changer.",
            "Remove the drawbar assembly from the spindle.",
            "Replace the disc spring stack with SPR-TC-40.",
            "Replace the clamp confirmation sensor SENS-PROX-08 and set its gap to 0.8 mm.",
            "Refit the drawbar and confirm a clamping force above 18 kN with the test gauge.",
        ),
    ),
    _procedure(
        "PROC-HP-04", "Replace main cylinder seal kit", "FM-SEAL-WEAR",
        loto=True, minutes=180, parts=(("SEAL-KIT-50", 1), ("OIL-HLP46-20L", 1)),
        steps=(
            "Lower the ram onto blocks, stop the pump and apply lockout and tagout on the main isolator.",
            "Bleed stored pressure from the accumulator until the gauge reads 0 bar.",
            "Remove the cylinder gland and pull out the old seals.",
            "Fit the SEAL-KIT-50 seals in the order shown in figure 4.",
            "Refit the gland and top up the oil with OIL-HLP46-20L.",
            "Remove the lockout, cycle the press 10 times at low pressure and check for leaks.",
        ),
    ),
    _procedure(
        "PROC-HP-05", "Change hydraulic oil and filter", "FM-OIL-CONTAM",
        loto=True, minutes=120, parts=(("OIL-HLP46-20L", 3), ("FLT-HYD-10", 1)),
        steps=(
            "Stop the pump, apply lockout and tagout, and bleed the accumulator to 0 bar.",
            "Drain the tank and wipe out the sludge.",
            "Replace the return filter with FLT-HYD-10.",
            "Refill with OIL-HLP46-20L to the upper sight-glass mark.",
            "Run the pump unloaded for 10 minutes and take an oil sample.",
        ),
    ),
    _procedure(
        "PROC-HP-06", "Reset and test relief valve", "FM-RELIEF-DRIFT",
        loto=True, minutes=60, parts=(("SEAL-RV-25", 1),),
        steps=(
            "Stop the pump, apply lockout and tagout, and bleed the accumulator to 0 bar.",
            "Remove the relief valve cap and replace the seal with SEAL-RV-25.",
            "Remove the lockout and set the valve to 210 bar with the test gauge.",
            "Lock the adjuster and record the setting.",
        ),
    ),
    _procedure(
        "PROC-RW-01", "Replace gripper fingers", "FM-GRIPPER-WEAR",
        loto=True, minutes=40, parts=(("GRP-FNG-12", 2),),
        steps=(
            "Move the robot to the service position and apply lockout and tagout on the cell isolator.",
            "Release air from the gripper valve block.",
            "Unbolt the worn fingers and fit two GRP-FNG-12 fingers.",
            "Remove the lockout and teach the part-present sensor on a master part.",
        ),
    ),
    _procedure(
        "PROC-RW-02", "Inspect axis 2 and reset servo drive", "FM-SERVO-OVERLOAD",
        loto=True, minutes=90, parts=(("FUSE-SRV-10", 1), ("BRG-6205-2RS", 1)),
        steps=(
            "Apply lockout and tagout on the cell isolator and wait 5 minutes for the drive capacitors to discharge.",
            "Check the axis 2 gearbox for binding by moving the arm by hand.",
            "Replace the drive fuse with FUSE-SRV-10, and replace the idler bearing with BRG-6205-2RS if it feels rough.",
            "Remove the lockout, reset the drive and run axis 2 at 10 percent speed.",
        ),
    ),
    _procedure(
        "PROC-RW-03", "Realign light curtain", "FM-LIGHT-CURTAIN",
        loto=True, minutes=30, parts=(("BRKT-LC-01", 1),),
        steps=(
            "Apply lockout and tagout on the cell isolator.",
            "Check both light curtain brackets and replace a bent one with BRKT-LC-01.",
            "Align the emitter and receiver until all beam LEDs are green.",
            "Remove the lockout and run the muting test from the safety menu.",
        ),
    ),
    _procedure(
        "PROC-LT-01", "Recalibrate pressure sensor", "FM-SENSOR-DRIFT",
        loto=False, minutes=30, parts=(),
        steps=(
            "Load the master part and start Calibration, then Reference, from the tester menu.",
            "Compare the reading with the reference gauge.",
            "Apply the offset if the difference is under 2 percent, otherwise replace the sensor with SENS-PRS-10.",
            "Run three master parts and confirm they pass.",
        ),
    ),
    _procedure(
        "PROC-LT-02", "Replace fixture seals", "FM-FIXTURE-SEAL",
        loto=True, minutes=45, parts=(("SEAL-FIX-30", 2),),
        steps=(
            "Close the air supply and apply lockout and tagout on the tester pneumatic valve.",
            "Remove the fixture plate and both test seals.",
            "Fit two SEAL-FIX-30 seals and refit the plate.",
            "Remove the lockout and run three master parts to confirm the leak rate is within limits.",
        ),
    ),
)
# fmt: on

# --- Spare parts ----------------------------------------------------------------

# Nicknames written by hand, on top of the two spellings every part gets.
_PART_HAND_ALIASES: dict[str, tuple[str, ...]] = {
    "BRG-7014-ACD": ("7014ACD", "spdl brg"),
    "SEAL-SPDL-70": ("seal 70",),
    "GREASE-KLB-2": ("KLB2 grease",),
    "FLT-CL-25": ("CL filter 25",),
    "SPR-TC-40": ("disc springs TC",),
    "SENS-PROX-08": ("prox sensor M8",),
    "SEAL-KIT-50": ("SK-50", "seal kit 50"),
    "OIL-HLP46-20L": ("HLP46", "hyd oil 20L"),
    "FLT-HYD-10": ("hyd filter 10u",),
    "SEAL-RV-25": ("RV seal",),
    "GRP-FNG-12": ("gripper finger", "fingers"),
    "FUSE-SRV-10": ("fuse 10A",),
    "BRG-6205-2RS": ("6205 2RS", "brg 6205"),
    "BRKT-LC-01": ("LC bracket",),
    "SEAL-FIX-30": ("fixture seal",),
    "SENS-PRS-10": ("pressure sensor 10bar",),
    "VALVE-FILL-08": ("fill valve",),
}


def part_aliases(part_no: str) -> list[str]:
    """The other ways to write a part number: with spaces, without dashes, and the
    hand-written nicknames."""
    aliases = [
        part_no.replace("-", " "),
        part_no.replace("-", ""),
        *_PART_HAND_ALIASES.get(part_no, ()),
    ]
    return list(dict.fromkeys(aliases))


def _part(
    part_no: str,
    description: str,
    fits: tuple[str, ...],
    qty: int,
    bin_: str,
    lead_days: int,
    cost_sgd: float,
    qty_old: int,
) -> PartSpec:
    return PartSpec(
        props=PartProps(
            part_no=part_no,
            description=description,
            aliases=part_aliases(part_no),
            qty_on_hand=qty,
            bin=bin_,
            lead_time_days=lead_days,
            stock_as_of=STOCK_AS_OF,
            unit_cost_sgd=cost_sgd,
        ),
        fits=fits,
        qty_old=qty_old,
    )


# Columns: part number, description, where it fits, stock, bin, lead time in days,
# unit cost in SGD, and stock in the old June count.
# fmt: off
PARTS = (
    _part("BRG-7014-ACD", "Spindle angular contact bearing 7014 ACD", _cnc("SPINDLE"), 2, "A-04", 21, 410.00, 4),
    _part("SEAL-SPDL-70", "Spindle front seal 70 mm", _cnc("SPINDLE"), 3, "A-05", 10, 38.50, 3),
    _part("GREASE-KLB-2", "Spindle bearing grease 400 g", _cnc("SPINDLE"), 6, "C-01", 5, 24.00, 8),
    _part("FLT-CL-25", "Coolant filter element 25 micron", _cnc("COOLANT"), 1, "B-03", 7, 65.00, 5),
    _part("SPR-TC-40", "Tool clamp disc spring set", _cnc("ATC"), 2, "A-09", 28, 180.00, 2),
    _part("SENS-PROX-08", "Inductive proximity sensor M8", _cnc("ATC"), 4, "D-02", 7, 95.00, 4),
    _part("SEAL-KIT-50", "Main cylinder seal kit, 50 mm rod", ("HP-01-CYL",), 3, "B-12", 14, 520.00, 1),
    _part("OIL-HLP46-20L", "Hydraulic oil HLP 46, 20 litre pail", ("HP-01-PUMP", "HP-01-CYL"), 9, "C-07", 3, 145.00, 12),
    _part("FLT-HYD-10", "Hydraulic return filter 10 micron", ("HP-01-PUMP",), 2, "B-14", 10, 88.00, 2),
    _part("SEAL-RV-25", "Relief valve seal kit", ("HP-01-VALVE",), 1, "B-15", 21, 72.00, 0),
    _part("GRP-FNG-12", "Gripper finger, hardened", ("RW-01-GRIPPER",), 0, "D-08", 14, 160.00, 4),
    _part("FUSE-SRV-10", "Servo drive fuse 10 A", ("RW-01-J2",), 5, "D-11", 5, 12.50, 5),
    _part("BRG-6205-2RS", "Deep groove ball bearing 6205-2RS", ("RW-01-J2",), 6, "A-02", 4, 18.00, 6),
    _part("BRKT-LC-01", "Light curtain mounting bracket", ("RW-01-SAFETY",), 2, "D-12", 14, 45.00, 2),
    _part("SEAL-FIX-30", "Leak test fixture seal 30 mm", ("LT-01-FIXTURE",), 8, "E-01", 7, 22.00, 10),
    _part("SENS-PRS-10", "Pressure sensor 0 to 10 bar", ("LT-01-SENSOR",), 1, "E-03", 30, 640.00, 1),
    _part("VALVE-FILL-08", "Fill valve 8 mm", ("LT-01-VALVE",), 2, "E-05", 21, 210.00, 2),
    _part("BELT-HTD-8M", "Timing belt HTD 8M", ("RW-01-J2",), 3, "A-11", 10, 54.00, 3),
    _part("O-RING-NBR-12", "O-ring NBR 12 mm, pack of 10", ("HP-01-VALVE",), 15, "C-03", 3, 9.00, 15),
    _part("O-RING-NBR-30", "O-ring NBR 30 mm, pack of 10", ("LT-01-FIXTURE",), 12, "C-03", 3, 11.00, 14),
    _part("FLT-AIR-05", "Compressed air filter 5 micron", ("LT-01-VALVE",), 4, "B-05", 7, 33.00, 4),
    _part("LAMP-HMI-24", "HMI backlight 24 V", (), 2, "D-15", 30, 120.00, 2),
    _part("CABLE-ENC-05", "Encoder cable 5 m", ("RW-01-J2",), 2, "D-10", 14, 85.00, 2),
    _part("NOZZLE-CL-06", "Coolant nozzle 6 mm", _cnc("COOLANT"), 10, "B-04", 5, 14.00, 12),
    _part("PUMP-CL-04", "Coolant pump 0.4 kW", _cnc("COOLANT"), 1, "F-01", 35, 890.00, 1),
    _part("HOSE-HYD-12", "Hydraulic hose 12 mm, 1 m", ("HP-01-PUMP",), 4, "C-09", 7, 48.00, 4),
    _part("GAUGE-PRS-250", "Pressure gauge 0 to 250 bar", ("HP-01-VALVE",), 2, "C-10", 10, 75.00, 2),
    _part("SWITCH-LIM-01", "Limit switch", ("HP-01-CYL",), 3, "D-04", 10, 42.00, 3),
    _part("HOLDER-BT40", "Tool holder BT40", _cnc("ATC"), 6, "A-14", 21, 130.00, 6),
    _part("COLLET-ER32", "Collet set ER32", _cnc("SPINDLE"), 4, "A-15", 10, 95.00, 4),
    _part("BATT-ENC-36", "Encoder backup battery 3.6 V", ("RW-01-J2",), 6, "D-13", 7, 16.00, 6),
    _part("RELAY-SAF-24", "Safety relay 24 V", ("RW-01-SAFETY",), 2, "D-14", 14, 155.00, 2),
    _part("SENS-PT100-01", "Temperature sensor PT100", (*_cnc("SPINDLE"), "HP-01-PUMP"), 3, "D-03", 14, 68.00, 3),
    _part("COUPLING-PMP-28", "Pump coupling 28 mm", ("HP-01-PUMP",), 1, "C-11", 21, 115.00, 1),
    _part("WIPER-CYL-50", "Cylinder rod wiper 50 mm", ("HP-01-CYL",), 2, "B-13", 14, 36.00, 2),
    _part("CUP-VAC-40", "Vacuum suction cup 40 mm", ("RW-01-GRIPPER",), 8, "D-09", 7, 19.00, 8),
    _part("NUT-GLAND-50", "Cylinder gland nut 50 mm", ("HP-01-CYL",), 1, "B-16", 28, 98.00, 1),
    _part("FUSE-CTRL-02", "Control fuse 2 A", (), 20, "D-16", 3, 3.50, 20),
    _part("CLEANER-CL-5L", "Coolant system cleaner, 5 litre", _cnc("COOLANT"), 4, "C-12", 5, 42.00, 4),
    _part("TAPE-PTFE-12", "PTFE thread tape", (), 30, "C-13", 2, 2.00, 30),
)
# fmt: on

# --- Technicians ----------------------------------------------------------------

TECHNICIANS = (
    TechnicianSpec("Tan Wei Ming", "Wei Ming", ("Wei Ming", "WM"), "A"),
    TechnicianSpec("Lim Jia Hui", "Jia Hui", ("Jia Hui", "JH"), "A"),
    TechnicianSpec("Goh Siew Ling", "Siew Ling", ("Siew Ling", "SL"), "A"),
    TechnicianSpec("Rajesh Pillai", "Rajesh", ("Rajesh", "RP"), "B"),
    TechnicianSpec("Nur Aisyah", "Aisyah", ("Aisyah", "NA"), "B"),
    TechnicianSpec("Muhammad Hafiz", "Hafiz", ("Hafiz", "MH"), "C"),
    TechnicianSpec("Arun Prakash", "Arun", ("Arun", "AP"), "C"),
    TechnicianSpec("Chen Jun Hao", "Jun Hao", ("Jun Hao", "JHC"), "C"),
)

# --- Lookups --------------------------------------------------------------------

# Each lookup finds a table entry by its id. Other generator modules use these.
ASSET_BY_ID = {asset.id: asset for asset in ASSETS}
COMPONENT_BY_ID = {component.id: component for component in COMPONENTS}
ALARM_BY_CODE = {spec.props.code: spec for spec in ALARMS}
FAILURE_MODE_BY_ID = {spec.props.id: spec for spec in FAILURE_MODES}
PROCEDURE_BY_ID = {spec.props.id: spec for spec in PROCEDURES}
PART_BY_NO = {spec.props.part_no: spec for spec in PARTS}
TECHNICIAN_BY_NAME = {tech.name: tech for tech in TECHNICIANS}

# --- Incident weights -------------------------------------------------------------

# How likely each machine is to have an incident.
MACHINE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("HP-01", 0.30),
    ("CNC-02", 0.22),
    ("CNC-01", 0.18),
    ("RW-01", 0.18),
    ("LT-01", 0.12),
)

_CNC_FAILURE_WEIGHTS = (
    ("FM-SPINDLE-BEARING", 0.35),
    ("FM-COOLANT-BLOCK", 0.40),
    ("FM-TOOL-CLAMP", 0.25),
)

# How likely each failure mode is, for each machine.
FAILURE_MODE_WEIGHTS: dict[str, tuple[tuple[str, float], ...]] = {
    "CNC-01": _CNC_FAILURE_WEIGHTS,
    "CNC-02": _CNC_FAILURE_WEIGHTS,
    "HP-01": (
        ("FM-SEAL-WEAR", 0.50),
        ("FM-OIL-CONTAM", 0.30),
        ("FM-RELIEF-DRIFT", 0.20),
    ),
    "RW-01": (
        ("FM-GRIPPER-WEAR", 0.45),
        ("FM-SERVO-OVERLOAD", 0.35),
        ("FM-LIGHT-CURTAIN", 0.20),
    ),
    "LT-01": (
        ("FM-SENSOR-DRIFT", 0.50),
        ("FM-FIXTURE-SEAL", 0.50),
    ),
}
