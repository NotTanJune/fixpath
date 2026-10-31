"""Tests for the hand-written plant catalogue: machines, components, failure modes,
alarm codes, procedures, parts, technicians and the dates. They check that the tables
agree with each other and that nothing the generator needs is empty."""

import datetime as dt

import pytest

from app.generator import catalogue

SGT = dt.timezone(dt.timedelta(hours=8))

# --- Expected catalogue, typed out by hand -------------------------------------

CNC = ["CNC-01", "CNC-02"]

# id: (name, machine_type, vendor, model, aliases)
MACHINES = {
    "CNC-01": (
        "Machining centre 1",
        "5-axis machining centre",
        "Kessler Werkzeugmaschinen",
        "KX-500",
        ["CNC1", "CNC 1", "MC-1", "Kessler 1"],
    ),
    "CNC-02": (
        "Machining centre 2",
        "5-axis machining centre",
        "Kessler Werkzeugmaschinen",
        "KX-500",
        ["CNC2", "CNC 2", "MC-2", "Kessler 2"],
    ),
    "HP-01": (
        "Hydraulic press",
        "250 t hydraulic press",
        "Bramwell Hydraulics",
        "BH-250",
        ["Press 2", "HP1", "hyd press", "the press", "PR-2"],
    ),
    "RW-01": (
        "Robot workcell",
        "Load, unload and deburr robot cell",
        "Orbis Robotics",
        "OR-6",
        ["robot cell", "RC-1", "robot", "deburr cell"],
    ),
    "LT-01": (
        "Leak tester",
        "Pressure-decay leak tester",
        "Veltra Test Systems",
        "VT-LD20",
        ["leak tester", "LT1", "leak test", "VT station"],
    ),
}

# id: name. The asset is the id without its last segment.
COMPONENTS = {
    "CNC-01-SPINDLE": "Spindle",
    "CNC-01-COOLANT": "Coolant system",
    "CNC-01-ATC": "Tool changer",
    "CNC-02-SPINDLE": "Spindle",
    "CNC-02-COOLANT": "Coolant system",
    "CNC-02-ATC": "Tool changer",
    "HP-01-PUMP": "Hydraulic pump",
    "HP-01-CYL": "Main cylinder and seals",
    "HP-01-VALVE": "Pressure relief valve",
    "RW-01-GRIPPER": "Gripper",
    "RW-01-J2": "Axis 2 servo",
    "RW-01-SAFETY": "Light curtain",
    "LT-01-SENSOR": "Pressure sensor",
    "LT-01-FIXTURE": "Test fixture and seals",
    "LT-01-VALVE": "Fill valve",
}

# fmt: off
# id: (name, mechanism, affected components)
FAILURE_MODES = {
    "FM-SPINDLE-BEARING": ("Spindle bearing wear", "Wear", ["CNC-01-SPINDLE", "CNC-02-SPINDLE"]),
    "FM-COOLANT-BLOCK": ("Coolant filter blocked", "Blockage", ["CNC-01-COOLANT", "CNC-02-COOLANT"]),
    "FM-TOOL-CLAMP": ("Tool clamp failure", "Mechanical failure", ["CNC-01-ATC", "CNC-02-ATC"]),
    "FM-SEAL-WEAR": ("Cylinder seal wear", "Wear", ["HP-01-CYL"]),
    "FM-OIL-CONTAM": ("Hydraulic oil contamination", "Contamination", ["HP-01-PUMP"]),
    "FM-RELIEF-DRIFT": ("Relief valve drift", "Out of adjustment", ["HP-01-VALVE"]),
    "FM-GRIPPER-WEAR": ("Gripper finger wear", "Wear", ["RW-01-GRIPPER"]),
    "FM-SERVO-OVERLOAD": ("Axis servo overload", "Overheating", ["RW-01-J2"]),
    "FM-LIGHT-CURTAIN": ("Light curtain misalignment", "Misalignment", ["RW-01-SAFETY"]),
    "FM-SENSOR-DRIFT": ("Pressure sensor drift", "Calibration drift", ["LT-01-SENSOR"]),
    "FM-FIXTURE-SEAL": ("Test fixture seal leak", "Leakage", ["LT-01-FIXTURE"]),
}

# code: (text, severity, machines, {failure mode: likelihood})
ALARMS = {
    "SP-001": ("Warm-up cycle complete", "info", CNC, {}),
    "SP-104": ("Spindle temperature high", "critical", CNC, {"FM-SPINDLE-BEARING": 0.8, "FM-COOLANT-BLOCK": 0.2}),
    "SP-117": ("Spindle vibration above limit", "warning", CNC, {"FM-SPINDLE-BEARING": 0.9}),
    "CL-210": ("Coolant pressure low", "warning", CNC, {"FM-COOLANT-BLOCK": 0.85}),
    "CL-214": ("Coolant flow interrupted", "critical", CNC, {"FM-COOLANT-BLOCK": 0.9}),
    "TC-305": ("Tool clamp not confirmed", "critical", CNC, {"FM-TOOL-CLAMP": 0.85}),
    "TC-311": ("Tool change timeout", "warning", CNC, {"FM-TOOL-CLAMP": 0.6}),
    "E-200": ("Pump start", "info", ["HP-01"], {}),
    "E-217": ("Main cylinder pressure loss", "critical", ["HP-01"], {"FM-SEAL-WEAR": 0.73, "FM-RELIEF-DRIFT": 0.2}),
    "E-221": ("Hydraulic oil temperature high", "warning", ["HP-01"], {"FM-OIL-CONTAM": 0.6, "FM-RELIEF-DRIFT": 0.3}),
    "E-230": ("Pump pressure unstable", "warning", ["HP-01"], {"FM-OIL-CONTAM": 0.7}),
    "E-245": ("Oil level low", "warning", ["HP-01"], {"FM-SEAL-WEAR": 0.6}),
    "E-252": ("Relief valve pressure out of range", "critical", ["HP-01"], {"FM-RELIEF-DRIFT": 0.85}),
    "RB-551": ("Gripper part-present signal lost", "warning", ["RW-01"], {"FM-GRIPPER-WEAR": 0.8}),
    "RB-560": ("Axis 2 overcurrent", "critical", ["RW-01"], {"FM-SERVO-OVERLOAD": 0.85}),
    "RB-563": ("Axis 2 following error", "warning", ["RW-01"], {"FM-SERVO-OVERLOAD": 0.7}),
    "RB-570": ("Light curtain muting fault", "critical", ["RW-01"], {"FM-LIGHT-CURTAIN": 0.8}),
    "VT-012": ("Reference pressure out of tolerance", "warning", ["LT-01"], {"FM-SENSOR-DRIFT": 0.75}),
    "VT-018": ("Leak rate above limit on master part", "critical", ["LT-01"], {"FM-FIXTURE-SEAL": 0.7, "FM-SENSOR-DRIFT": 0.25}),
    "VT-021": ("Fill time exceeded", "warning", ["LT-01"], {"FM-FIXTURE-SEAL": 0.6}),
}

# code: the alias set. One code per family shows each rule.
ALARM_ALIASES = {
    "E-217": {"E217", "ERR 217", "Err:217"},
    "E-200": {"E200", "ERR 200", "Err:200"},
    "SP-104": {"SP104", "ALM SP-104"},
    "SP-001": {"SP001", "ALM SP-001"},
    "CL-210": {"CL210", "ALM CL-210"},
    "TC-311": {"TC311", "ALM TC-311"},
    "RB-560": {"RB560"},
    "RB-551": {"RB551"},
    "VT-018": {"VT018", "VT18"},
    "VT-012": {"VT012", "VT12"},
    "VT-021": {"VT021", "VT21"},
}

# id: (title, resolves, main fix, needs LOTO, minutes, step count, {part: qty})
PROCEDURES = {
    "PROC-CNC-01": ("Replace spindle bearing set", "FM-SPINDLE-BEARING", True, True, 240, 6, {"BRG-7014-ACD": 2, "SEAL-SPDL-70": 1, "GREASE-KLB-2": 1}),
    "PROC-CNC-02": ("Check spindle vibration from the HMI", "FM-SPINDLE-BEARING", False, False, 20, 4, {}),
    "PROC-CNC-03": ("Clean coolant filter and flush lines", "FM-COOLANT-BLOCK", True, True, 45, 5, {"FLT-CL-25": 1}),
    "PROC-CNC-04": ("Replace tool clamp disc springs", "FM-TOOL-CLAMP", True, True, 120, 5, {"SPR-TC-40": 1, "SENS-PROX-08": 1}),
    "PROC-HP-04": ("Replace main cylinder seal kit", "FM-SEAL-WEAR", True, True, 180, 6, {"SEAL-KIT-50": 1, "OIL-HLP46-20L": 1}),
    "PROC-HP-05": ("Change hydraulic oil and filter", "FM-OIL-CONTAM", True, True, 120, 5, {"OIL-HLP46-20L": 3, "FLT-HYD-10": 1}),
    "PROC-HP-06": ("Reset and test relief valve", "FM-RELIEF-DRIFT", True, True, 60, 4, {"SEAL-RV-25": 1}),
    "PROC-RW-01": ("Replace gripper fingers", "FM-GRIPPER-WEAR", True, True, 40, 4, {"GRP-FNG-12": 2}),
    "PROC-RW-02": ("Inspect axis 2 and reset servo drive", "FM-SERVO-OVERLOAD", True, True, 90, 4, {"FUSE-SRV-10": 1, "BRG-6205-2RS": 1}),
    "PROC-RW-03": ("Realign light curtain", "FM-LIGHT-CURTAIN", True, True, 30, 4, {"BRKT-LC-01": 1}),
    "PROC-LT-01": ("Recalibrate pressure sensor", "FM-SENSOR-DRIFT", True, False, 30, 4, {}),
    "PROC-LT-02": ("Replace fixture seals", "FM-FIXTURE-SEAL", True, True, 45, 4, {"SEAL-FIX-30": 2}),
}
# fmt: on

# part_no: (current qty, bin, lead time days, unit cost SGD, old qty). A sample
# of rows that includes the stock-out (GRP-FNG-12) and several stock drops.
PART_SAMPLE = {
    "BRG-7014-ACD": (2, "A-04", 21, 410.00, 4),
    "FLT-CL-25": (1, "B-03", 7, 65.00, 5),
    "SEAL-KIT-50": (3, "B-12", 14, 520.00, 1),
    "OIL-HLP46-20L": (9, "C-07", 3, 145.00, 12),
    "SEAL-RV-25": (1, "B-15", 21, 72.00, 0),
    "GRP-FNG-12": (0, "D-08", 14, 160.00, 4),
    "SENS-PRS-10": (1, "E-03", 30, 640.00, 1),
    "TAPE-PTFE-12": (30, "C-13", 2, 2.00, 30),
}

# part_no: components it fits. A sample that covers "CNC both", two-component
# parts and the parts that fit nothing.
PART_FITS_SAMPLE = {
    "BRG-7014-ACD": ["CNC-01-SPINDLE", "CNC-02-SPINDLE"],
    "OIL-HLP46-20L": ["HP-01-CYL", "HP-01-PUMP"],
    "SENS-PT100-01": ["CNC-01-SPINDLE", "CNC-02-SPINDLE", "HP-01-PUMP"],
    "NOZZLE-CL-06": ["CNC-01-COOLANT", "CNC-02-COOLANT"],
    "LAMP-HMI-24": [],
    "FUSE-CTRL-02": [],
    "TAPE-PTFE-12": [],
}

# part_no: hand aliases. Every part also gets two derived variants.
PART_HAND_ALIASES = {
    "BRG-7014-ACD": ["7014ACD", "spdl brg"],
    "SEAL-SPDL-70": ["seal 70"],
    "GREASE-KLB-2": ["KLB2 grease"],
    "FLT-CL-25": ["CL filter 25"],
    "SPR-TC-40": ["disc springs TC"],
    "SENS-PROX-08": ["prox sensor M8"],
    "SEAL-KIT-50": ["SK-50", "seal kit 50"],
    "OIL-HLP46-20L": ["HLP46", "hyd oil 20L"],
    "FLT-HYD-10": ["hyd filter 10u"],
    "SEAL-RV-25": ["RV seal"],
    "GRP-FNG-12": ["gripper finger", "fingers"],
    "FUSE-SRV-10": ["fuse 10A"],
    "BRG-6205-2RS": ["6205 2RS", "brg 6205"],
    "BRKT-LC-01": ["LC bracket"],
    "SEAL-FIX-30": ["fixture seal"],
    "SENS-PRS-10": ["pressure sensor 10bar"],
    "VALVE-FILL-08": ["fill valve"],
}

# name: (first name, short forms, shift). The first name is the one people use for the
# person, and it is a field of its own: it does not depend on the order of the short
# forms.
TECHNICIANS = {
    "Tan Wei Ming": ("Wei Ming", ["Wei Ming", "WM"], "A"),
    "Lim Jia Hui": ("Jia Hui", ["Jia Hui", "JH"], "A"),
    "Goh Siew Ling": ("Siew Ling", ["Siew Ling", "SL"], "A"),
    "Rajesh Pillai": ("Rajesh", ["Rajesh", "RP"], "B"),
    "Nur Aisyah": ("Aisyah", ["Aisyah", "NA"], "B"),
    "Muhammad Hafiz": ("Hafiz", ["Hafiz", "MH"], "C"),
    "Arun Prakash": ("Arun", ["Arun", "AP"], "C"),
    "Chen Jun Hao": ("Jun Hao", ["Jun Hao", "JHC"], "C"),
}

# lookup name: (table name, key of one entry). Each lookup holds the entries of its
# table, keyed by id, so the generator modules never build their own.
LOOKUPS = {
    "ASSET_BY_ID": ("ASSETS", lambda asset: asset.id),
    "COMPONENT_BY_ID": ("COMPONENTS", lambda component: component.id),
    "ALARM_BY_CODE": ("ALARMS", lambda spec: spec.props.code),
    "FAILURE_MODE_BY_ID": ("FAILURE_MODES", lambda spec: spec.props.id),
    "PROCEDURE_BY_ID": ("PROCEDURES", lambda spec: spec.props.id),
    "PART_BY_NO": ("PARTS", lambda spec: spec.props.part_no),
    "TECHNICIAN_BY_NAME": ("TECHNICIANS", lambda tech: tech.name),
}


# --- Catalogue tests -----------------------------------------------------------


def test_catalogue_counts():
    assert len(catalogue.ASSETS) == 5
    assert len(catalogue.COMPONENTS) == 15
    assert len(catalogue.FAILURE_MODES) == 11
    assert len(catalogue.ALARMS) == 20
    assert len(catalogue.PROCEDURES) == 12
    assert len(catalogue.PARTS) == 40
    assert len(catalogue.TECHNICIANS) == 8


def test_the_line():
    assert catalogue.LINE.id == "LINE-03"
    assert catalogue.LINE.name == "Line 3, brake caliper housings"


def test_machines_match_the_catalogue_spec():
    rows = {
        asset.id: (
            asset.name,
            asset.machine_type,
            asset.vendor,
            asset.model,
            asset.aliases,
        )
        for asset in catalogue.ASSETS
    }
    assert rows == MACHINES


def test_the_press_keeps_its_old_line_number_as_an_alias():
    press = next(asset for asset in catalogue.ASSETS if asset.id == "HP-01")
    assert "Press 2" in press.aliases


def test_components_match_the_catalogue_spec():
    rows = {component.id: component.name for component in catalogue.COMPONENTS}
    assert rows == COMPONENTS
    for component in catalogue.COMPONENTS:
        assert component.asset_id == component.id.rsplit("-", 1)[0]


def test_failure_modes_match_the_catalogue_spec():
    rows = {
        spec.props.id: (spec.props.name, spec.props.mechanism, list(spec.affects))
        for spec in catalogue.FAILURE_MODES
    }
    assert rows == FAILURE_MODES


def test_alarm_codes_match_the_catalogue_spec():
    rows = {
        spec.props.code: (
            spec.props.text,
            spec.props.severity,
            list(spec.machines),
            dict(spec.indicates),
        )
        for spec in catalogue.ALARMS
    }
    assert rows == ALARMS


@pytest.mark.parametrize("code", sorted(ALARM_ALIASES))
def test_alarm_aliases_follow_the_rules(code):
    spec = next(spec for spec in catalogue.ALARMS if spec.props.code == code)
    assert set(spec.props.aliases) == ALARM_ALIASES[code]
    assert len(spec.props.aliases) == len(set(spec.props.aliases))


def test_every_alarm_code_gets_the_dashless_alias():
    for spec in catalogue.ALARMS:
        assert spec.props.code.replace("-", "") in spec.props.aliases


def test_procedures_match_the_catalogue_spec():
    rows = {
        spec.props.id: (
            spec.props.title,
            spec.resolves,
            spec.main_fix,
            spec.props.requires_loto,
            spec.props.est_minutes,
            len(spec.props.steps),
            dict(spec.parts),
        )
        for spec in catalogue.PROCEDURES
    }
    assert rows == PROCEDURES


def test_each_failure_mode_has_exactly_one_main_fix():
    for spec in catalogue.FAILURE_MODES:
        mains = [
            proc.props.id
            for proc in catalogue.PROCEDURES
            if proc.resolves == spec.props.id and proc.main_fix
        ]
        assert len(mains) == 1, spec.props.id


def test_procedure_steps_name_every_part_the_procedure_requires():
    for spec in catalogue.PROCEDURES:
        text = " ".join(spec.props.steps)
        for part_no, _ in spec.parts:
            assert part_no in text, (spec.props.id, part_no)


def test_procedure_steps_keep_their_figure_reference():
    steps = next(
        proc.props.steps
        for proc in catalogue.PROCEDURES
        if proc.props.id == "PROC-HP-04"
    )
    assert steps[3] == "Fit the SEAL-KIT-50 seals in the order shown in figure 4."


def test_parts_match_the_catalogue_spec_sample():
    by_no = {spec.props.part_no: spec for spec in catalogue.PARTS}
    assert len(by_no) == 40
    for part_no, (qty, bin_, lead, cost, old) in PART_SAMPLE.items():
        spec = by_no[part_no]
        assert (
            spec.props.qty_on_hand,
            spec.props.bin,
            spec.props.lead_time_days,
            spec.props.unit_cost_sgd,
            spec.qty_old,
        ) == (qty, bin_, lead, cost, old)
        assert spec.props.stock_as_of == dt.date(2026, 10, 15)


def test_part_fits_sample():
    by_no = {spec.props.part_no: spec for spec in catalogue.PARTS}
    for part_no, fits in PART_FITS_SAMPLE.items():
        assert sorted(by_no[part_no].fits) == sorted(fits), part_no


def test_every_part_gets_the_spaced_and_dashless_variants():
    for spec in catalogue.PARTS:
        part_no = spec.props.part_no
        assert part_no.replace("-", " ") in spec.props.aliases
        assert part_no.replace("-", "") in spec.props.aliases
        assert len(spec.props.aliases) == len(set(spec.props.aliases))
    sample = next(s for s in catalogue.PARTS if s.props.part_no == "SEAL-KIT-50")
    assert "SEAL KIT 50" in sample.props.aliases
    assert "SEALKIT50" in sample.props.aliases


def test_hand_aliases_are_added_on_top_of_the_variants():
    by_no = {spec.props.part_no: spec for spec in catalogue.PARTS}
    for part_no, hand in PART_HAND_ALIASES.items():
        assert len(by_no[part_no].props.aliases) == 2 + len(hand), part_no
        for alias in hand:
            assert alias in by_no[part_no].props.aliases, (part_no, alias)
    unlisted = [no for no in by_no if no not in PART_HAND_ALIASES]
    assert len(unlisted) == 40 - len(PART_HAND_ALIASES)
    for part_no in unlisted:
        assert len(by_no[part_no].props.aliases) == 2, part_no


def test_technicians_match_the_catalogue_spec():
    rows = {
        tech.name: (tech.first_name, list(tech.short_forms), tech.shift)
        for tech in catalogue.TECHNICIANS
    }
    assert rows == TECHNICIANS


@pytest.mark.parametrize("lookup", LOOKUPS)
def test_each_lookup_holds_the_entries_of_its_table_by_key(lookup):
    table, key = LOOKUPS[lookup]
    entries = getattr(catalogue, table)
    found = getattr(catalogue, lookup)
    assert list(found) == [key(entry) for entry in entries]
    for entry in entries:
        assert found[key(entry)] is entry


def test_each_failure_mode_has_two_symptom_templates():
    assert sorted(catalogue.SYMPTOMS) == sorted(FAILURE_MODES)
    for failure_mode, templates in catalogue.SYMPTOMS.items():
        assert len(templates) == 2, failure_mode
        assert templates[0] != templates[1]
    assert catalogue.SYMPTOMS["FM-SEAL-WEAR"] == (
        "oil leaking under the ram, pressure dropping",
        "press losing pressure mid-stroke, oil on the floor",
    )


def test_catalogue_references_resolve():
    machines = {asset.id for asset in catalogue.ASSETS}
    components = {component.id for component in catalogue.COMPONENTS}
    failure_modes = {spec.props.id for spec in catalogue.FAILURE_MODES}
    parts = {spec.props.part_no for spec in catalogue.PARTS}
    for component in catalogue.COMPONENTS:
        assert component.asset_id in machines
    for spec in catalogue.FAILURE_MODES:
        assert set(spec.affects) <= components
    for spec in catalogue.ALARMS:
        assert set(spec.machines) <= machines
        assert {fm for fm, _ in spec.indicates} <= failure_modes
    for spec in catalogue.PROCEDURES:
        assert spec.resolves in failure_modes
        assert {part_no for part_no, _ in spec.parts} <= parts
        assert all(qty >= 1 for _, qty in spec.parts)
    for spec in catalogue.PARTS:
        assert set(spec.fits) <= components


def test_every_failure_mode_has_an_alarm_on_each_machine_it_can_hit():
    """The generator picks an incident's alarm from the machine's codes for that
    failure mode, so none of those sets may be empty."""
    for fm in catalogue.FAILURE_MODES:
        for component_id in fm.affects:
            machine = component_id.rsplit("-", 1)[0]
            codes = [
                spec.props.code
                for spec in catalogue.ALARMS
                if machine in spec.machines
                and any(name == fm.props.id for name, _ in spec.indicates)
            ]
            assert codes, (fm.props.id, machine)


def test_every_damaged_component_has_a_spare_part_beyond_the_main_fix():
    """An incident can add one more part that fits the damaged component and is not in
    the main procedure, so that set may not be empty."""
    for fm in catalogue.FAILURE_MODES:
        main = next(
            proc
            for proc in catalogue.PROCEDURES
            if proc.resolves == fm.props.id and proc.main_fix
        )
        used = {part_no for part_no, _ in main.parts}
        for component in fm.affects:
            spare = [
                part.props.part_no
                for part in catalogue.PARTS
                if component in part.fits and part.props.part_no not in used
            ]
            assert spare, (fm.props.id, component)


def test_shifts_cover_the_day_in_sgt():
    assert "".join(catalogue.shift_for_hour(hour) for hour in range(24)) == (
        "CCCCCCC" + "AAAAAAAA" + "BBBBBBBB" + "C"
    )


def test_stock_dates_and_the_observation_window():
    assert catalogue.STOCK_AS_OF == dt.date(2026, 10, 15)
    assert catalogue.STOCK_OLD_AS_OF == dt.date(2026, 6, 30)
    assert catalogue.SGT.utcoffset(None) == dt.timedelta(hours=8)
    assert catalogue.PERIOD_START == dt.datetime(2026, 4, 1, 0, 0, tzinfo=SGT)
    assert catalogue.PERIOD_END == dt.datetime(2026, 9, 30, 23, 59, tzinfo=SGT)


def test_incident_weights_match_the_catalogue_spec():
    assert dict(catalogue.MACHINE_WEIGHTS) == {
        "HP-01": 0.30,
        "CNC-02": 0.22,
        "CNC-01": 0.18,
        "RW-01": 0.18,
        "LT-01": 0.12,
    }
    cnc = {"FM-SPINDLE-BEARING": 0.35, "FM-COOLANT-BLOCK": 0.40, "FM-TOOL-CLAMP": 0.25}
    observed = {
        asset: dict(weights)
        for asset, weights in catalogue.FAILURE_MODE_WEIGHTS.items()
    }
    assert observed == {
        "CNC-01": cnc,
        "CNC-02": cnc,
        "HP-01": {"FM-SEAL-WEAR": 0.50, "FM-OIL-CONTAM": 0.30, "FM-RELIEF-DRIFT": 0.20},
        "RW-01": {
            "FM-GRIPPER-WEAR": 0.45,
            "FM-SERVO-OVERLOAD": 0.35,
            "FM-LIGHT-CURTAIN": 0.20,
        },
        "LT-01": {"FM-SENSOR-DRIFT": 0.50, "FM-FIXTURE-SEAL": 0.50},
    }


def test_info_alarms_have_a_daily_time_in_sgt():
    assert dict(catalogue.INFO_SCHEDULE) == {
        "SP-001": dt.time(6, 30),
        "E-200": dt.time(6, 45),
    }


def test_machine_serials_match_the_log_headers():
    """Each machine has a serial number, in the order of the machine table. The log
    headers and the stores machine list print it."""
    assert catalogue.SERIALS == {
        "CNC-01": "KX5-10442",
        "CNC-02": "KX5-10457",
        "HP-01": "BH250-0091",
        "RW-01": "OR6-2231",
        "LT-01": "VT20-5512",
    }
    assert list(catalogue.SERIALS) == [asset.id for asset in catalogue.ASSETS]
