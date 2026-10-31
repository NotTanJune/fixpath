"""Tests for the generated plant: it comes out the same for the same seed, and its
nodes, edges, incidents and alarm events follow the rules. They also cover the JSON
file, the committed world and the command line. The catalogue has its own tests in
test_catalogue.py."""

import ast
import datetime as dt
import hashlib
import inspect
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

from app import ontology
from app.config import DATA_DIR, ROOT, TRUTH_DIR
from app.generator import DEFAULT_SEED
from app.generator import __main__ as cli
from app.generator import catalogue
from app.generator import world as world_module
from app.generator.world import (
    IncidentDetail,
    World,
    build_world,
    world_to_json,
    write_world,
)

SGT = dt.timezone(dt.timedelta(hours=8))
SEEDS = (42, 43, 2026)

# --- The built world -------------------------------------------------------------


@pytest.fixture(scope="module")
def world() -> World:
    return build_world(42)


def nodes_of(world: World, node_type: str):
    return [node for node in world.nodes if node.type == node_type]


def edges_of(world: World, rel: str):
    return [edge for edge in world.edges if edge.rel == rel]


def node_types_by_id(world: World) -> dict[str, str]:
    return {node.id: node.type for node in world.nodes}


# --- Determinism ---------------------------------------------------------------


def test_two_builds_with_the_same_seed_serialize_to_identical_bytes():
    first = world_to_json(build_world(42)).encode("utf-8")
    second = world_to_json(build_world(42)).encode("utf-8")
    assert first == second


def test_build_ignores_and_leaves_alone_the_global_random_state():
    random.seed(1)
    state = random.getstate()
    first = world_to_json(build_world(42))
    assert random.getstate() == state
    random.seed(2)
    assert world_to_json(build_world(42)) == first


def test_build_gives_the_same_bytes_under_any_hash_seed(world, tmp_path):
    """Set iteration order depends on PYTHONHASHSEED, so a build that leaned on
    it would differ between these processes."""
    code = (
        "import hashlib\n"
        "from app.generator.world import build_world, world_to_json\n"
        "text = world_to_json(build_world(42))\n"
        "print(hashlib.sha256(text.encode('utf-8')).hexdigest())\n"
    )
    expected = hashlib.sha256(world_to_json(world).encode("utf-8")).hexdigest()
    for hash_seed in ("0", "1", "4242"):
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected, hash_seed


def test_generator_code_uses_no_clock_and_no_global_random():
    """The seed alone decides the world, so generator code may not use the global
    random module, time.time() or datetime.now()."""
    offenders = []
    for path in sorted(Path(catalogue.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom) and node.module == "random":
                offenders.extend(
                    f"{where} from random import {alias.name}"
                    for alias in node.names
                    if alias.name != "Random"
                )
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value.id if isinstance(node.value, ast.Name) else None
            if base == "random" and node.attr != "Random":
                offenders.append(f"{where} random.{node.attr}")
            if base == "time" and node.attr in {"time", "time_ns", "monotonic"}:
                offenders.append(f"{where} time.{node.attr}")
            if node.attr in {"now", "utcnow", "today"}:
                offenders.append(f"{where} .{node.attr}")
    assert offenders == []


# --- Nodes ---------------------------------------------------------------------


def test_world_has_the_documented_top_level_keys(world):
    assert sorted(json.loads(world_to_json(world))) == [
        "alarm_events",
        "edges",
        "incidents_detail",
        "nodes",
        "seed",
        "stock_old",
        "technicians",
    ]
    assert world.seed == 42


def test_every_node_id_is_valid_for_its_type(world):
    for node in world.nodes:
        assert node.type in ontology.NODE_TYPES, node.type
        assert ontology.validate_id(node.type, node.id), (node.type, node.id)


def test_node_ids_are_unique_across_types(world):
    ids = [node.id for node in world.nodes]
    assert len(ids) == len(set(ids))


def test_node_props_are_the_ontology_models_and_repeat_the_node_id(world):
    id_field = {"AlarmCode": "code", "Part": "part_no"}
    for node in world.nodes:
        parsed = ontology.props_model(node.type).model_validate(node.props)
        assert parsed.model_dump(mode="json") == node.props
        assert node.props[id_field.get(node.type, "id")] == node.id


def test_the_catalogue_becomes_nodes(world):
    counts = Counter(node.type for node in world.nodes)
    assert counts["Line"] == 1
    assert counts["Asset"] == 5
    assert counts["Component"] == 15
    assert counts["AlarmCode"] == 20
    assert counts["FailureMode"] == 11
    assert counts["Procedure"] == 12
    assert counts["Part"] == 40
    assert set(counts) <= set(ontology.NODE_TYPES)


def test_part_nodes_carry_the_current_stock(world):
    parts = {node.id: node.props for node in nodes_of(world, "Part")}
    assert parts["GRP-FNG-12"]["qty_on_hand"] == 0
    assert parts["SEAL-KIT-50"]["qty_on_hand"] == 3
    assert parts["SEAL-KIT-50"]["bin"] == "B-12"
    assert parts["SEAL-KIT-50"]["unit_cost_sgd"] == 520.0
    assert parts["SEAL-KIT-50"]["aliases"][:2] == ["SEAL KIT 50", "SEALKIT50"]
    assert {props["stock_as_of"] for props in parts.values()} == {"2026-10-15"}


def test_stock_old_holds_the_june_quantities(world):
    parts = [spec.props.part_no for spec in catalogue.PARTS]
    assert sorted(world.stock_old) == sorted(parts)
    assert world.stock_old["GRP-FNG-12"] == 4
    assert world.stock_old["SEAL-RV-25"] == 0
    assert world.stock_old["SEAL-KIT-50"] == 1
    assert world.stock_old["OIL-HLP46-20L"] == 12


def test_technicians_are_listed_with_short_forms_and_shift(world):
    rows = {tech.name: (tech.short_forms, tech.shift) for tech in world.technicians}
    assert rows == {
        spec.name: (list(spec.short_forms), spec.shift)
        for spec in catalogue.TECHNICIANS
    }
    assert len(world.technicians) == 8


# --- Edges ---------------------------------------------------------------------


def test_every_edge_endpoint_exists_as_a_node(world):
    ids = set(node_types_by_id(world))
    for edge in world.edges:
        assert edge.src in ids, edge
        assert edge.dst in ids, edge


def test_every_edge_is_allowed_by_the_ontology(world):
    types = node_types_by_id(world)
    for edge in world.edges:
        assert ontology.is_allowed(types[edge.src], edge.rel, types[edge.dst]), edge


def test_edge_props_match_the_relation_models(world):
    for edge in world.edges:
        model = ontology.RELATIONS[edge.rel].props
        if model is None:
            assert edge.props == {}, edge
        else:
            parsed = model.model_validate(edge.props)
            assert parsed.model_dump(mode="json") == edge.props


def test_edges_are_unique(world):
    keys = [(edge.src, edge.rel, edge.dst) for edge in world.edges]
    assert len(keys) == len(set(keys))


def test_the_catalogue_becomes_edges(world):
    counts = Counter(edge.rel for edge in world.edges)
    assert counts["HAS_MACHINE"] == 5
    assert counts["HAS_COMPONENT"] == 15
    assert counts["AFFECTS"] == 14
    assert counts["RESOLVED_BY"] == 12
    assert counts["REQUIRES"] == 16
    assert counts["FITS"] == 51
    assert counts["INDICATES"] == 22


def test_the_line_has_every_machine_and_each_machine_its_components(world):
    machines = {edge.dst for edge in edges_of(world, "HAS_MACHINE")}
    assert {edge.src for edge in edges_of(world, "HAS_MACHINE")} == {"LINE-03"}
    assert machines == {"CNC-01", "CNC-02", "HP-01", "RW-01", "LT-01"}
    for edge in edges_of(world, "HAS_COMPONENT"):
        assert edge.dst.startswith(edge.src + "-")


def test_every_failure_mode_has_a_fix_and_the_check_is_a_second_fix(world):
    resolved = Counter(edge.src for edge in edges_of(world, "RESOLVED_BY"))
    for node in nodes_of(world, "FailureMode"):
        assert resolved[node.id] >= 1, node.id
    assert resolved["FM-SPINDLE-BEARING"] == 2
    targets = {
        (edge.src, edge.dst)
        for edge in edges_of(world, "RESOLVED_BY")
        if edge.src == "FM-SPINDLE-BEARING"
    }
    assert targets == {
        ("FM-SPINDLE-BEARING", "PROC-CNC-01"),
        ("FM-SPINDLE-BEARING", "PROC-CNC-02"),
    }


def test_every_required_part_exists_with_its_quantity(world):
    parts = {node.id for node in nodes_of(world, "Part")}
    requires = {
        (edge.src, edge.dst): edge.props["qty"] for edge in edges_of(world, "REQUIRES")
    }
    assert all(part in parts for _, part in requires)
    assert requires[("PROC-CNC-01", "BRG-7014-ACD")] == 2
    assert requires[("PROC-HP-05", "OIL-HLP46-20L")] == 3
    assert requires[("PROC-RW-01", "GRP-FNG-12")] == 2
    assert not [pair for pair in requires if pair[0] in {"PROC-CNC-02", "PROC-LT-01"}]


def test_parts_that_fit_nothing_have_no_fits_edge(world):
    fitted = {edge.src for edge in edges_of(world, "FITS")}
    for part_no in ("LAMP-HMI-24", "FUSE-CTRL-02", "TAPE-PTFE-12"):
        assert part_no not in fitted
    fits = {(edge.src, edge.dst) for edge in edges_of(world, "FITS")}
    assert ("SENS-PT100-01", "HP-01-PUMP") in fits
    assert ("SENS-PT100-01", "CNC-02-SPINDLE") in fits


def test_indicates_edges_carry_the_catalogue_likelihoods(world):
    likelihood = {
        (edge.src, edge.dst): edge.props["likelihood"]
        for edge in edges_of(world, "INDICATES")
    }
    assert likelihood[("E-217", "FM-SEAL-WEAR")] == 0.73
    assert likelihood[("E-217", "FM-RELIEF-DRIFT")] == 0.2
    assert likelihood[("VT-018", "FM-SENSOR-DRIFT")] == 0.25
    assert not [pair for pair in likelihood if pair[0] in {"SP-001", "E-200"}]


# --- The JSON file -------------------------------------------------------------


def test_world_json_has_sorted_keys_two_space_indent_and_one_trailing_newline(world):
    text = world_to_json(world)
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")
    assert "\r" not in text
    data = json.loads(text)
    assert json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n" == text


def test_lists_are_sorted_by_a_stable_key(world):
    data = json.loads(world_to_json(world))
    nodes = [(node["type"], node["id"]) for node in data["nodes"]]
    assert nodes == sorted(nodes)
    edges = [(edge["src"], edge["rel"], edge["dst"]) for edge in data["edges"]]
    assert edges == sorted(edges)
    names = [tech["name"] for tech in data["technicians"]]
    assert names == sorted(names)
    details = [record["id"] for record in data["incidents_detail"]]
    assert details == sorted(details)
    events = [
        (event["timestamp"], event["asset"], event["code"], event["incident_id"] or "")
        for event in data["alarm_events"]
    ]
    assert events and events == sorted(events)


def test_write_world_defaults_to_the_truth_directory():
    default = inspect.signature(write_world).parameters["path"].default
    assert default == TRUTH_DIR / "world.json"


def test_write_world_writes_the_serialized_world(world, tmp_path):
    path = tmp_path / "deeper" / "world.json"
    assert write_world(world, path) == path
    assert path.read_bytes() == world_to_json(world).encode("utf-8")


def test_world_json_loads_back_into_the_model(world):
    assert World.model_validate_json(world_to_json(world)) == world


# --- Incidents ---------------------------------------------------------------------

FIRST_MINUTE = dt.datetime(2026, 4, 1, 0, 0, tzinfo=SGT)
LAST_MINUTE = dt.datetime(2026, 9, 30, 23, 59, tzinfo=SGT)


@pytest.fixture(scope="module", params=SEEDS, ids=[f"seed{seed}" for seed in SEEDS])
def seeded_world(request) -> World:
    """The rules below hold for every seed, so each test runs on a few."""
    return build_world(request.param)


@dataclass
class IncidentFacts:
    """What the world says about one incident, read back from its node, its edges
    and its detail record the way a renderer or an eval would."""

    id: str
    props: dict
    detail: IncidentDetail
    assets: list[str]
    failure_modes: list[str]
    alarms: list[str]
    parts: dict[str, int]

    @property
    def start(self) -> dt.datetime:
        return self.detail.start.astimezone(SGT)

    @property
    def asset(self) -> str:
        (asset,) = self.assets
        return asset

    @property
    def failure_mode(self) -> str:
        (failure_mode,) = self.failure_modes
        return failure_mode

    @property
    def alarm(self) -> str | None:
        return self.alarms[0] if self.alarms else None


def incident_facts(world: World) -> list[IncidentFacts]:
    detail = {record.id: record for record in world.incidents_detail}
    facts = {
        node.id: IncidentFacts(node.id, node.props, detail[node.id], [], [], [], {})
        for node in nodes_of(world, "Incident")
    }
    for edge in world.edges:
        if edge.src not in facts:
            continue
        found = facts[edge.src]
        if edge.rel == "OCCURRED_ON":
            found.assets.append(edge.dst)
        elif edge.rel == "ROOT_CAUSE":
            found.failure_modes.append(edge.dst)
        elif edge.rel == "TRIGGERED_BY":
            found.alarms.append(edge.dst)
        elif edge.rel == "USED":
            found.parts[edge.dst] = edge.props["qty"]
    assert facts, "the world has no incidents, so no rule below would be checked"
    return [facts[incident_id] for incident_id in sorted(facts)]


def expected_shift(hour: int) -> str:
    """Shift A is 07:00 to 15:00, B is 15:00 to 23:00 and C is 23:00 to 07:00."""
    if 7 <= hour < 15:
        return "A"
    return "B" if 15 <= hour < 23 else "C"


MAIN_FIX = {spec.resolves: spec for spec in catalogue.PROCEDURES if spec.main_fix}
FAILURE_MODE_SPEC = {spec.props.id: spec for spec in catalogue.FAILURE_MODES}
PART_SPEC = {spec.props.part_no: spec for spec in catalogue.PARTS}


def affected_component(failure_mode: str, asset: str) -> str:
    (component,) = [
        comp
        for comp in FAILURE_MODE_SPEC[failure_mode].affects
        if comp.rsplit("-", 1)[0] == asset
    ]
    return component


def test_different_seeds_give_different_worlds():
    first = json.loads(world_to_json(build_world(42)))
    second = json.loads(world_to_json(build_world(43)))
    assert (first.pop("seed"), second.pop("seed")) == (42, 43)
    assert first != second


def test_there_are_sixty_incidents_numbered_in_start_order(seeded_world):
    facts = incident_facts(seeded_world)
    assert [fact.id for fact in facts] == [f"INC-2026-{n:03d}" for n in range(1, 61)]
    starts = [fact.start for fact in facts]
    assert starts == sorted(starts)
    assert len(nodes_of(seeded_world, "Incident")) == 60
    assert len(seeded_world.incidents_detail) == 60


def test_incident_starts_are_whole_minutes_inside_the_window(seeded_world):
    for fact in incident_facts(seeded_world):
        assert FIRST_MINUTE <= fact.start <= LAST_MINUTE, fact.id
        assert (fact.start.second, fact.start.microsecond) == (0, 0), fact.id
        assert fact.start.utcoffset() == dt.timedelta(hours=8)
        assert fact.detail.start.utcoffset() == dt.timedelta(hours=8)


def test_incident_shift_matches_its_sgt_start_hour(seeded_world):
    for fact in incident_facts(seeded_world):
        assert fact.props["shift"] == expected_shift(fact.start.hour), fact.id


def test_incident_date_is_the_sgt_start_date(seeded_world):
    for fact in incident_facts(seeded_world):
        assert fact.props["date"] == fact.start.date().isoformat(), fact.id
        assert "2026-04-01" <= fact.props["date"] <= "2026-09-30"


def test_each_incident_has_one_machine_and_one_root_cause_and_at_most_one_alarm(
    seeded_world,
):
    for fact in incident_facts(seeded_world):
        assert len(fact.assets) == 1, fact.id
        assert len(fact.failure_modes) == 1, fact.id
        assert len(fact.alarms) <= 1, fact.id


def test_the_root_cause_can_hit_the_machine(seeded_world):
    for fact in incident_facts(seeded_world):
        assert affected_component(fact.failure_mode, fact.asset), fact.id


def test_the_affected_component_is_public_and_finds_each_damaged_component():
    for spec in catalogue.FAILURE_MODES:
        for component in spec.affects:
            asset = component.rsplit("-", 1)[0]
            found = world_module.affected_component(spec.props.id, asset)
            assert found == component, (spec.props.id, asset)


def test_the_affected_component_names_the_machine_when_the_failure_mode_misses_it():
    with pytest.raises(ValueError) as error:
        world_module.affected_component("FM-SEAL-WEAR", "CNC-01")
    message = str(error.value)
    assert "FM-SEAL-WEAR" in message and "CNC-01" in message
    assert "exactly one component" in message and "none" in message


def test_the_affected_component_lists_them_when_the_failure_mode_hits_two(monkeypatch):
    two = catalogue.FailureModeSpec(
        props=ontology.FailureModeProps(id="FM-TEST", name="Test", mechanism="Wear"),
        affects=("CNC-01-SPINDLE", "CNC-01-COOLANT"),
    )
    monkeypatch.setitem(catalogue.FAILURE_MODE_BY_ID, "FM-TEST", two)
    with pytest.raises(ValueError) as error:
        world_module.affected_component("FM-TEST", "CNC-01")
    message = str(error.value)
    assert "FM-TEST" in message and "CNC-01" in message
    assert "exactly one component" in message
    assert "CNC-01-SPINDLE" in message and "CNC-01-COOLANT" in message


def test_the_alarm_is_raised_by_the_machine_and_indicates_the_root_cause(
    seeded_world,
):
    alarm_spec = {spec.props.code: spec for spec in catalogue.ALARMS}
    for fact in incident_facts(seeded_world):
        if fact.alarm is None:
            continue
        spec = alarm_spec[fact.alarm]
        assert fact.asset in spec.machines, fact.id
        assert fact.failure_mode in dict(spec.indicates), fact.id


def test_the_fix_is_the_main_procedure_and_the_parts_follow_it(seeded_world):
    for fact in incident_facts(seeded_world):
        procedure = MAIN_FIX[fact.failure_mode]
        assert fact.detail.procedure_id == procedure.props.id, fact.id
        extra = {
            part_no: qty
            for part_no, qty in fact.parts.items()
            if part_no not in dict(procedure.parts)
        }
        for part_no, qty in procedure.parts:
            assert fact.parts[part_no] == qty, (fact.id, part_no)
        assert len(extra) <= 1, fact.id
        for part_no, qty in extra.items():
            assert qty == 1, fact.id
            component = affected_component(fact.failure_mode, fact.asset)
            assert component in PART_SPEC[part_no].fits, (fact.id, part_no)


def test_downtime_follows_the_estimate_formula(seeded_world):
    for fact in incident_facts(seeded_world):
        minutes = MAIN_FIX[fact.failure_mode].props.est_minutes
        downtime = fact.props["downtime_min"]
        assert downtime % 5 == 0, fact.id
        # est * U(0.8, 2.5) + U(20, 120), rounded to the nearest 5.
        assert minutes * 0.8 + 20 - 2.5 <= downtime <= minutes * 2.5 + 120 + 2.5


def test_the_technician_is_a_member_of_the_incident_shift(seeded_world):
    shift_of = {tech.name: tech.shift for tech in seeded_world.technicians}
    for fact in incident_facts(seeded_world):
        assert fact.props["technician"] == fact.detail.technician, fact.id
        assert shift_of[fact.props["technician"]] == fact.props["shift"], fact.id


def test_symptom_text_is_one_of_the_two_templates(seeded_world):
    for fact in incident_facts(seeded_world):
        assert fact.props["symptom_text"] in catalogue.SYMPTOMS[fact.failure_mode]


def test_resolution_text_names_the_procedure_and_the_parts_used(seeded_world):
    titles = {spec.props.id: spec.props.title for spec in catalogue.PROCEDURES}
    for fact in incident_facts(seeded_world):
        text = fact.props["resolution_text"]
        procedure_id = fact.detail.procedure_id
        prefix = f"Done {titles[procedure_id]} ({procedure_id}), used "
        assert text.startswith(prefix), text
        used = text[len(prefix) :]
        if fact.parts:
            listed = re.findall(r"([A-Z0-9]+(?:-[A-Z0-9]+)+) x(\d+)", used)
            assert {part: int(qty) for part, qty in listed} == fact.parts
            assert len(listed) == len(fact.parts)
        else:
            assert used == "no parts"


def test_incident_detail_holds_start_procedure_parts_and_technician(seeded_world):
    for fact in incident_facts(seeded_world):
        detail = fact.detail
        assert detail.id == fact.id
        assert {use.part_no: use.qty for use in detail.parts} == fact.parts
        assert [use.part_no for use in detail.parts] == sorted(fact.parts)
        assert detail.technician in {tech.name for tech in seeded_world.technicians}


def test_indicates_support_counts_the_incidents_of_that_alarm_and_root_cause(
    seeded_world,
):
    started = Counter(
        (fact.alarm, fact.failure_mode)
        for fact in incident_facts(seeded_world)
        if fact.alarm is not None
    )
    edges = edges_of(seeded_world, "INDICATES")
    for edge in edges:
        assert edge.props["support"] == started[(edge.src, edge.dst)], edge
    assert sum(edge.props["support"] for edge in edges) == sum(started.values())


def test_used_edges_carry_the_quantity_and_link_the_incident_to_the_part(seeded_world):
    parts = {node.id for node in nodes_of(seeded_world, "Part")}
    used = edges_of(seeded_world, "USED")
    assert used
    for edge in used:
        assert edge.dst in parts
        assert edge.props["qty"] >= 1


def test_incidents_detail_is_sorted_by_incident_id(seeded_world):
    ids = [record.id for record in seeded_world.incidents_detail]
    assert ids == sorted(ids)


# --- How the choices are weighted ----------------------------------------------------


@pytest.fixture(scope="module")
def many_worlds() -> list[World]:
    """Forty seeds, so 2400 incidents and about 10000 background warnings, enough
    to see the weights."""
    return [build_world(seed) for seed in range(1000, 1040)]


@pytest.fixture(scope="module")
def many_incidents(many_worlds) -> list[IncidentFacts]:
    return [fact for world in many_worlds for fact in incident_facts(world)]


def share(items, wanted) -> float:
    items = list(items)
    return sum(1 for item in items if wanted(item)) / len(items)


def test_machines_are_picked_with_the_catalogue_weights(many_incidents):
    for asset, weight in catalogue.MACHINE_WEIGHTS:
        observed = share(many_incidents, lambda fact: fact.asset == asset)
        assert abs(observed - weight) < 0.04, (asset, observed)


def test_failure_modes_are_picked_with_the_catalogue_weights_per_machine(
    many_incidents,
):
    for asset, weights in catalogue.FAILURE_MODE_WEIGHTS.items():
        on_machine = [fact for fact in many_incidents if fact.asset == asset]
        assert len(on_machine) > 200
        for failure_mode, weight in weights:
            observed = share(on_machine, lambda fact: fact.failure_mode == failure_mode)
            assert abs(observed - weight) < 0.10, (asset, failure_mode, observed)


def test_nine_incidents_in_ten_start_with_an_alarm(many_incidents):
    observed = share(many_incidents, lambda fact: fact.alarm is not None)
    assert abs(observed - 0.9) < 0.04


def test_the_starting_alarm_is_weighted_by_likelihood(many_incidents):
    # FM-SPINDLE-BEARING: SP-104 at 0.8 against SP-117 at 0.9.
    spindle = [
        fact.alarm
        for fact in many_incidents
        if fact.failure_mode == "FM-SPINDLE-BEARING" and fact.alarm
    ]
    assert len(spindle) > 150
    assert abs(share(spindle, lambda code: code == "SP-104") - 0.8 / 1.7) < 0.10
    # FM-SEAL-WEAR on the press: E-217 at 0.73 against E-245 at 0.6.
    seal = [
        fact.alarm
        for fact in many_incidents
        if fact.failure_mode == "FM-SEAL-WEAR" and fact.alarm
    ]
    assert len(seal) > 150
    assert abs(share(seal, lambda code: code == "E-217") - 0.73 / 1.33) < 0.10


def test_about_one_incident_in_seven_uses_an_extra_part(many_incidents):
    def extra_parts(fact: IncidentFacts) -> int:
        return len(fact.parts) - len(MAIN_FIX[fact.failure_mode].parts)

    assert all(extra_parts(fact) in (0, 1) for fact in many_incidents)
    observed = share(many_incidents, lambda fact: extra_parts(fact) == 1)
    assert abs(observed - 0.15) < 0.04


def test_incident_starts_are_spread_evenly_over_the_six_months(many_incidents):
    span = (LAST_MINUTE - FIRST_MINUTE).total_seconds()
    positions = [
        (fact.start - FIRST_MINUTE).total_seconds() / span for fact in many_incidents
    ]
    assert abs(sum(positions) / len(positions) - 0.5) < 0.03
    for sixth in range(6):
        observed = share(positions, lambda p: sixth / 6 <= p < (sixth + 1) / 6)
        assert abs(observed - 1 / 6) < 0.04, sixth
    # The three shifts are eight hours each, so a uniform start gives each a third.
    for shift in "ABC":
        observed = share(many_incidents, lambda fact: fact.props["shift"] == shift)
        assert abs(observed - 1 / 3) < 0.04, shift


def test_technicians_share_their_shift_evenly(many_incidents):
    crew = {
        shift: [t.name for t in catalogue.TECHNICIANS if t.shift == shift]
        for shift in "ABC"
    }
    for shift, names in crew.items():
        on_shift = [fact for fact in many_incidents if fact.props["shift"] == shift]
        for name in names:
            observed = share(on_shift, lambda fact: fact.props["technician"] == name)
            assert abs(observed - 1 / len(names)) < 0.10, (shift, name)


def test_both_symptom_templates_get_used(many_incidents):
    for failure_mode, templates in catalogue.SYMPTOMS.items():
        texts = [
            fact.props["symptom_text"]
            for fact in many_incidents
            if fact.failure_mode == failure_mode
        ]
        if len(texts) < 40:
            continue
        for template in templates:
            assert abs(share(texts, lambda text: text == template) - 0.5) < 0.2


def test_the_extra_part_is_drawn_from_everything_that_fits(many_incidents):
    seen = {
        part_no
        for fact in many_incidents
        if fact.failure_mode == "FM-SEAL-WEAR"
        for part_no in fact.parts
        if part_no not in dict(MAIN_FIX["FM-SEAL-WEAR"].parts)
    }
    assert seen == {"SWITCH-LIM-01", "WIPER-CYL-50", "NUT-GLAND-50"}


# --- Alarm events --------------------------------------------------------------------

WINDOW_END = dt.datetime(2026, 10, 1, 0, 0, tzinfo=SGT)
ALARM_SPEC = {spec.props.code: spec for spec in catalogue.ALARMS}
SEVERITY = {code: spec.props.severity for code, spec in ALARM_SPEC.items()}


def tagged_events(world: World, incident_id: str):
    return [event for event in world.alarm_events if event.incident_id == incident_id]


def test_alarm_events_are_utc_and_sorted(seeded_world):
    events = seeded_world.alarm_events
    assert events
    assert all(event.timestamp.utcoffset() == dt.timedelta(0) for event in events)
    keys = [
        (event.timestamp, event.asset, event.code, event.incident_id or "")
        for event in events
    ]
    assert keys == sorted(keys)
    first = json.loads(world_to_json(seeded_world))["alarm_events"][0]
    assert sorted(first) == ["asset", "code", "incident_id", "timestamp"]
    assert first["timestamp"].endswith("Z")


def test_alarm_events_use_codes_that_their_machine_can_raise(seeded_world):
    assert seeded_world.alarm_events
    for event in seeded_world.alarm_events:
        assert event.asset in ALARM_SPEC[event.code].machines, event


def test_alarm_events_fall_inside_the_six_months(seeded_world):
    assert seeded_world.alarm_events
    for event in seeded_world.alarm_events:
        assert FIRST_MINUTE <= event.timestamp < WINDOW_END, event


def test_events_stay_inside_the_window_for_incidents_at_either_edge(monkeypatch):
    """Half the incidents start an hour into the window, so any precursor would
    fall before it. The other half start in its last minute, so most of the burst
    would fall after it. Those lines are left out."""
    starts = [FIRST_MINUTE + dt.timedelta(hours=1)] * 30 + [LAST_MINUTE] * 30
    monkeypatch.setattr(world_module, "_incident_starts", lambda rng: starts)
    edge_world = build_world(7)
    for event in edge_world.alarm_events:
        assert FIRST_MINUTE <= event.timestamp < WINDOW_END, event
    with_alarm = [fact for fact in incident_facts(edge_world) if fact.alarm]
    assert {fact.start for fact in with_alarm} == {starts[0], starts[-1]}
    for fact in with_alarm:
        mine = tagged_events(edge_world, fact.id)
        assert any(event.timestamp == fact.start for event in mine), fact.id
        if fact.start == starts[0]:
            assert all(event.timestamp >= fact.start for event in mine), fact.id


def test_events_with_an_incident_id_belong_to_a_real_incident(seeded_world):
    ids = {fact.id for fact in incident_facts(seeded_world)}
    tagged = {event.incident_id for event in seeded_world.alarm_events} - {None}
    assert tagged
    assert tagged <= ids


def test_an_incident_with_an_alarm_has_one_to_four_lines_within_ten_minutes(
    seeded_world,
):
    for fact in incident_facts(seeded_world):
        if fact.alarm is None:
            continue
        burst = [
            event
            for event in tagged_events(seeded_world, fact.id)
            if event.timestamp >= fact.start
        ]
        assert 1 <= len(burst) <= 4, fact.id
        assert {(event.asset, event.code) for event in burst} == {
            (fact.asset, fact.alarm)
        }
        assert min(event.timestamp for event in burst) == fact.start, fact.id
        for event in burst:
            assert event.timestamp - fact.start <= dt.timedelta(minutes=10)
        assert len({event.timestamp for event in burst}) == len(burst), fact.id


def test_an_incident_without_an_alarm_has_no_alarm_events(seeded_world):
    silent = [fact for fact in incident_facts(seeded_world) if fact.alarm is None]
    assert silent
    for fact in silent:
        assert tagged_events(seeded_world, fact.id) == [], fact.id


def test_a_precursor_is_one_earlier_warning_of_the_same_root_cause(seeded_world):
    early_total = 0
    for fact in incident_facts(seeded_world):
        early = [
            event
            for event in tagged_events(seeded_world, fact.id)
            if event.timestamp < fact.start
        ]
        assert len(early) <= 1, fact.id
        early_total += len(early)
        for event in early:
            lead = fact.start - event.timestamp
            assert dt.timedelta(hours=2) <= lead <= dt.timedelta(hours=48), fact.id
            assert event.asset == fact.asset
            assert SEVERITY[event.code] == "warning"
            assert fact.failure_mode in dict(ALARM_SPEC[event.code].indicates)
    assert early_total > 0


def test_background_events_are_warnings_without_an_incident(seeded_world):
    background = [
        event
        for event in seeded_world.alarm_events
        if event.incident_id is None and SEVERITY[event.code] != "info"
    ]
    assert background
    assert all(SEVERITY[event.code] == "warning" for event in background)
    machines = {asset.id for asset in catalogue.ASSETS}
    assert {event.asset for event in background} == machines


def test_info_events_fire_once_a_day_at_the_scheduled_sgt_time(seeded_world):
    info = [
        event for event in seeded_world.alarm_events if SEVERITY[event.code] == "info"
    ]
    assert all(event.incident_id is None for event in info)
    days = [dt.date(2026, 4, 1) + dt.timedelta(days=n) for n in range(183)]
    assert days[-1] == dt.date(2026, 9, 30)

    def at(day: dt.date, hour: int, minute: int) -> dt.datetime:
        return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=SGT)

    expected = {("CNC-01", "SP-001", at(day, 6, 30)) for day in days}
    expected |= {("CNC-02", "SP-001", at(day, 6, 30)) for day in days}
    expected |= {("HP-01", "E-200", at(day, 6, 45)) for day in days}
    assert len(info) == 3 * 183
    assert {(event.asset, event.code, event.timestamp) for event in info} == expected


def test_raised_edges_match_the_alarm_events(seeded_world):
    times: dict[tuple[str, str], list[dt.datetime]] = {}
    for event in seeded_world.alarm_events:
        times.setdefault((event.asset, event.code), []).append(event.timestamp)
    raised = {
        (edge.src, edge.dst): edge.props for edge in edges_of(seeded_world, "RAISED")
    }
    assert times
    assert set(raised) == set(times)
    for key, stamps in times.items():
        props = raised[key]
        assert props["count"] == len(stamps), key
        assert dt.datetime.fromisoformat(props["first_seen"]) == min(stamps), key
        assert dt.datetime.fromisoformat(props["last_seen"]) == max(stamps), key


def test_raised_counts_include_the_info_events(seeded_world):
    raised = {
        (edge.src, edge.dst): edge.props["count"]
        for edge in edges_of(seeded_world, "RAISED")
    }
    assert raised[("CNC-01", "SP-001")] == 183
    assert raised[("CNC-02", "SP-001")] == 183
    assert raised[("HP-01", "E-200")] == 183
    assert ("RW-01", "E-200") not in raised


def test_raised_edge_for_an_incident_alarm_counts_its_lines(seeded_world):
    """A critical code only ever fires inside an incident, so its RAISED count is
    the number of lines those incidents logged."""
    lines = Counter(
        (event.asset, event.code)
        for event in seeded_world.alarm_events
        if SEVERITY[event.code] == "critical"
    )
    assert lines
    for (asset, code), count in lines.items():
        edge = next(
            e
            for e in edges_of(seeded_world, "RAISED")
            if (e.src, e.dst) == (asset, code)
        )
        assert edge.props["count"] == count


def test_background_warnings_average_two_a_week_per_machine(many_worlds):
    for asset in catalogue.ASSETS:
        counts = [
            sum(
                1
                for event in world.alarm_events
                if event.asset == asset.id
                and event.incident_id is None
                and SEVERITY[event.code] == "warning"
            )
            for world in many_worlds
        ]
        mean = sum(counts) / len(counts)
        assert abs(mean - 2 * 26) < 5, (asset.id, mean)
        assert len(set(counts)) > 10, "a Poisson count varies from seed to seed"


def test_background_warnings_use_every_warning_code_of_the_machine_evenly(many_worlds):
    for asset in catalogue.ASSETS:
        codes = [
            spec.props.code
            for spec in catalogue.ALARMS
            if asset.id in spec.machines and spec.props.severity == "warning"
        ]
        seen = [
            event.code
            for world in many_worlds
            for event in world.alarm_events
            if event.asset == asset.id
            and event.incident_id is None
            and event.code in codes
        ]
        assert set(seen) == set(codes)
        for code in codes:
            assert abs(share(seen, lambda c: c == code) - 1 / len(codes)) < 0.08, code


def test_about_half_the_alarm_incidents_get_a_precursor(many_worlds):
    def has_precursor(world: World, fact: IncidentFacts) -> bool:
        return any(
            event.timestamp < fact.start for event in tagged_events(world, fact.id)
        )

    # The light curtain has no warning code, so it cannot have a precursor.
    candidates = [
        (world, fact)
        for world in many_worlds
        for fact in incident_facts(world)
        if fact.alarm is not None and fact.failure_mode != "FM-LIGHT-CURTAIN"
    ]
    observed = share(candidates, lambda pair: has_precursor(*pair))
    assert abs(observed - 0.5) < 0.06
    light_curtain = [
        fact
        for world in many_worlds
        for fact in incident_facts(world)
        if fact.failure_mode == "FM-LIGHT-CURTAIN"
    ]
    assert light_curtain
    for world in many_worlds:
        for fact in incident_facts(world):
            if fact.failure_mode == "FM-LIGHT-CURTAIN":
                assert not has_precursor(world, fact)


def test_the_precursor_code_is_chosen_among_the_warning_codes(many_worlds):
    seen = set()
    for world in many_worlds:
        for fact in incident_facts(world):
            if fact.failure_mode == "FM-OIL-CONTAM":
                seen |= {
                    event.code
                    for event in tagged_events(world, fact.id)
                    if event.timestamp < fact.start
                }
    assert seen == {"E-221", "E-230"}


# --- The default seed and the committed world ----------------------------------------


def test_the_committed_world_json_is_the_default_seed_world():
    committed = (TRUTH_DIR / "world.json").read_bytes()
    expected = world_to_json(build_world(DEFAULT_SEED)).encode("utf-8")
    assert committed == expected, (
        "data/truth/world.json is out of date with the generator: run `make data`"
    )


def test_the_default_seed_gives_every_machine_and_failure_mode_incidents():
    """The demo world needs something to find for each machine and failure mode:
    at least 6 incidents on every machine and at least 2 for every failure mode."""
    facts = incident_facts(build_world(DEFAULT_SEED))
    per_machine = Counter(fact.asset for fact in facts)
    per_failure_mode = Counter(fact.failure_mode for fact in facts)
    for asset in catalogue.ASSETS:
        found = per_machine[asset.id]
        assert found >= 6, (asset.id, found)
    for spec in catalogue.FAILURE_MODES:
        found = per_failure_mode[spec.props.id]
        assert found >= 2, (spec.props.id, found)


# --- Command line and Makefile -------------------------------------------------------


def test_cli_writes_world_json_under_the_out_directory(tmp_path, capsys):
    assert cli.main(["--seed", "42", "--out", str(tmp_path)]) == 0
    path = tmp_path / "truth" / "world.json"
    assert path.read_bytes() == world_to_json(build_world(42)).encode("utf-8")
    assert capsys.readouterr().out.startswith("wrote ")


def test_cli_seed_picks_the_world(tmp_path):
    cli.main(["--seed", "43", "--out", str(tmp_path)])
    data = json.loads((tmp_path / "truth" / "world.json").read_text(encoding="utf-8"))
    assert data["seed"] == 43


def test_cli_out_defaults_to_the_data_directory(monkeypatch):
    written, rendered = [], []
    monkeypatch.setattr(
        cli, "write_world", lambda world, path: written.append(path) or path
    )
    # The raw files and mentions.json go to the same directory. They are stubbed so
    # that this test, which uses the default directory, never rewrites the repo data.
    monkeypatch.setattr(
        cli, "render_raw", lambda world, out: rendered.append(out) or {}
    )
    assert cli.main(["--seed", "42"]) == 0
    assert written == [TRUTH_DIR / "world.json"]
    assert rendered == [DATA_DIR]


def test_cli_seed_defaults_to_the_default_seed(tmp_path):
    assert cli.main(["--out", str(tmp_path)]) == 0
    data = json.loads((tmp_path / "truth" / "world.json").read_text(encoding="utf-8"))
    assert data["seed"] == DEFAULT_SEED


def test_module_entry_point_writes_the_world(world, tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "app.generator", "--seed", "42", "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("wrote ")
    written = (tmp_path / "truth" / "world.json").read_bytes()
    assert written == world_to_json(world).encode("utf-8")


def test_make_data_runs_the_generator_without_a_seed_flag():
    """The seed lives only in DEFAULT_SEED, so the target passes none."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^\.PHONY:.*\bdata\b", makefile, re.MULTILINE)
    assert re.search(
        r"^data:\n\tuv run python -m app\.generator$", makefile, re.MULTILINE
    )
