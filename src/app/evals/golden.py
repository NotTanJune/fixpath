"""Builds the golden question sets used to test the system, from the hidden truth.

Run it with python -m app.evals.golden. It writes two files of test questions: one for
triage and one for full answers. The questions are made from fixed templates and a seeded
random generator, never by a model, so they cost nothing and come out the same every
time. They read the way a technician types, with short forms and aliases.

build_triage() makes the triage set, which has one question per kind of incident plus a
few that test edge cases such as a missing alarm or an off-topic request. build_answers()
makes the answer set, which has 20 real incidents plus 5 trap questions. write() saves
both sets and load() reads them back.
"""

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.evals.truth import Truth, load_truth

GOLDEN_DIR = ROOT / "data" / "evals" / "golden"
SEED = 7
NAMES = ("triage", "answers")
NO_ALARM = "none"

# Words that name a hazard in a symptom. A question with one counts as safety-critical
# even when the fix needs no lockout. Edge-case questions are not scored on safety
# unless they hold one of these.
HAZARD_WORDS = ("hissing", "oil on the floor", "leaking", "light curtain", "guard")

TRIAGE_WITH_ALARM = (
    "{machine} down w/ {alarm}, {symptom} pls help",
    "{alarm} on {machine} again. {symptom}",
    "{symptom} on {machine}, throwing {alarm} pls check",
    "{machine} {alarm} - {symptom}",
    "{alarm} {machine}: {symptom}, what to do?",
)
TRIAGE_NO_ALARM = (
    "{machine} {symptom}, no alarm on the screen pls help",
    "{symptom} on {machine} w/ no alarm",
    "{machine}: {symptom}. whats wrong?",
)
ANSWER_WITH_ALARM = (
    "{machine} is showing {alarm}. {symptom}. "
    "whats the fix and what parts do i need pls",
    "{alarm} w/ {symptom} on {machine}, how do we fix it",
    "{symptom} - {machine} - {alarm}. cause? fix? parts?",
    "need help on {machine}, {alarm} came up and {symptom}",
)
ANSWER_NO_ALARM = (
    "{machine}: {symptom}. no alarm code. whats the fix and parts pls",
    "{symptom} on {machine} w/ nothing on the alarm list, what do i do",
)

ANSWERS_PER_SET = 20
MIN_PER_MACHINE = 3

IncidentType = tuple[str, str | None, str]


def _incident_type(truth: Truth, incident_id: str) -> IncidentType:
    alarms = truth.targets(incident_id, "TRIGGERED_BY")
    return (
        str(truth.machine_of(incident_id)),
        alarms[0] if alarms else None,
        str(truth.root_cause_of(incident_id)),
    )


def _incidents_by_type(truth: Truth) -> dict[IncidentType, list[str]]:
    """The incident ids of each kind of incident (machine, alarm, cause), in id order."""
    found: dict[IncidentType, list[str]] = defaultdict(list)
    for incident_id in sorted(truth.of_type("Incident")):
        found[_incident_type(truth, incident_id)].append(incident_id)
    return dict(
        sorted(
            found.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2])
        )
    )


# --- the words of a question ----------------------------------------------------------


def _unique_models(truth: Truth) -> dict[str, list[str]]:
    """The machines that have each model name."""
    found: dict[str, list[str]] = defaultdict(list)
    for asset_id, asset in sorted(truth.of_type("Asset").items()):
        found[str(asset.props.get("model"))].append(asset_id)
    return dict(found)


def machine_ref(rng: random.Random, truth: Truth, asset_id: str) -> str:
    """A machine as a technician names it: its id, an alias, or its model when no other
    machine shares it."""
    asset = truth.nodes[asset_id]
    options = [asset_id, *asset.props.get("aliases", [])]
    model = str(asset.props.get("model"))
    if len(_unique_models(truth)[model]) == 1:
        options.append(f"the {model}")
    return rng.choice(options)


def alarm_ref(rng: random.Random, truth: Truth, code: str) -> str:
    """An alarm code in one of the spellings the logs use."""
    return rng.choice([code, *truth.nodes[code].props.get("aliases", [])])


def _hazard(question: str) -> bool:
    text = question.lower()
    return any(word in text for word in HAZARD_WORDS)


def _symptom(truth: Truth, incident_id: str) -> str:
    return str(truth.nodes[incident_id].props["symptom_text"])


def _write_question(
    rng: random.Random,
    truth: Truth,
    incident_id: str,
    with_alarm: tuple[str, ...],
    no_alarm: tuple[str, ...],
) -> str:
    machine, alarm, _mode = _incident_type(truth, incident_id)
    templates = with_alarm if alarm else no_alarm
    return rng.choice(templates).format(
        machine=machine_ref(rng, truth, machine),
        alarm=alarm_ref(rng, truth, alarm) if alarm else "",
        symptom=_symptom(truth, incident_id),
    )


# --- the facts of a fix ---------------------------------------------------------------


def _requires_lockout(truth: Truth, procedure_id: str) -> bool:
    return bool(truth.nodes[procedure_id].props.get("requires_loto"))


def parts_of(truth: Truth, procedure_id: str) -> list[dict[str, Any]]:
    """The parts a procedure needs, with quantity and bin, sorted by part number."""
    parts = [
        {
            "part_no": edge.dst,
            "qty": edge.props["qty"],
            "bin": truth.nodes[edge.dst].props.get("bin"),
        }
        for edge in truth.edges
        if edge.src == procedure_id and edge.rel == "REQUIRES"
    ]
    return sorted(parts, key=lambda part: part["part_no"])


def _machines_of_alarm(truth: Truth, code: str) -> set[str]:
    """The machines that have the failure modes the alarm points to."""
    machines = set()
    for mode in truth.targets(code, "INDICATES"):
        for edge in truth.edges:
            if edge.src == mode and edge.rel == "AFFECTS":
                machines.add(str(truth.nodes[edge.dst].props.get("asset_id")))
    return machines


# --- triage ---------------------------------------------------------------------------


def _labels(
    machine: str | None,
    alarm: str | None,
    root_cause: str | None,
    intent: str,
    safety: bool | None,
) -> dict[str, Any]:
    return {
        "machine": machine,
        "alarm": alarm or NO_ALARM,
        "root_cause": root_cause,
        "intent": intent,
        "safety": safety,
    }


def _gate_items(rng: random.Random, truth: Truth) -> list[dict[str, Any]]:
    assets = sorted(truth.of_type("Asset"))
    plain_aliases = [
        (asset_id, alias)
        for asset_id in assets
        for alias in truth.nodes[asset_id].props.get("aliases", [])
        if not any(char.isdigit() for char in alias)
    ]
    alias_machine, alias = rng.choice(plain_aliases)
    alarm_machine = next(
        (code, next(iter(machines)))
        for code in sorted(truth.of_type("AlarmCode"))
        if len(machines := _machines_of_alarm(truth, code)) == 1
    )
    code, code_machine = alarm_machine
    plain_machine = rng.choice(assets)
    models = _unique_models(truth)
    shared = next(model for model, ids in sorted(models.items()) if len(ids) > 1)
    parts = sorted(truth.of_type("Part"))
    two_machine_part = next(
        part
        for part in parts
        if len(
            {
                str(truth.nodes[c].props.get("asset_id"))
                for e in truth.edges
                if e.src == part and e.rel == "FITS"
                for c in [e.dst]
            }
        )
        == 2
        and truth.nodes[part].props.get("aliases")
    )
    part_alias = str(truth.nodes[two_machine_part].props["aliases"][-1])

    def gate(
        kind: str,
        question: str,
        labels: dict[str, Any],
        expect: list[str] | None = None,
    ):
        item: dict[str, Any] = {
            "source": "gate",
            "kind": kind,
            "question": question,
            "truth_incident": None,
            "hazard": _hazard(question),
            "labels": {**labels, "safety": True if _hazard(question) else None},
        }
        if expect:
            item["expect_status"] = expect
        return item

    return [
        gate(
            "alias_only",
            f"{alias} keeps tripping out, no idea why",
            _labels(alias_machine, None, None, "troubleshoot", None),
        ),
        gate(
            "alarm_only",
            f"{alarm_ref(rng, truth, code)} again, what does that mean pls",
            _labels(code_machine, code, None, "troubleshoot", None),
        ),
        gate(
            "no_alarm",
            f"{plain_machine} stopped mid cycle, nothing on the alarm list",
            _labels(plain_machine, None, None, "troubleshoot", None),
        ),
        gate(
            "ambiguous_model",
            f"the {shared} on the left is throwing a clamp fault pls help",
            _labels(None, None, None, "troubleshoot", None),
            ["clarify"],
        ),
        gate(
            "part_question",
            f"got any {part_alias} left? need one pls",
            _labels(None, None, None, "find_part", None),
            ["clarify"],
        ),
        gate(
            "out_of_scope",
            "who is on the C shift roster this week",
            _labels(None, None, None, "out_of_scope", None),
            ["out_of_scope"],
        ),
    ]


def build_triage(truth: Truth) -> list[dict[str, Any]]:
    """The triage set: the first incident of each kind, then the edge-case questions."""
    rng = random.Random(SEED)
    items = []
    for kind, incident_ids in _incidents_by_type(truth).items():
        incident_id = incident_ids[0]
        machine, alarm, mode = kind
        question = _write_question(
            rng, truth, incident_id, TRIAGE_WITH_ALARM, TRIAGE_NO_ALARM
        )
        procedure_id = truth.incident_details[incident_id].procedure_id
        hazard = _hazard(question)
        items.append(
            {
                "source": "incident",
                "kind": "incident",
                "question": question,
                "truth_incident": incident_id,
                "hazard": hazard,
                "labels": _labels(
                    machine,
                    alarm,
                    mode,
                    "troubleshoot",
                    _requires_lockout(truth, procedure_id) or hazard,
                ),
            }
        )
    items += _gate_items(rng, truth)
    for number, item in enumerate(items, start=1):
        item["id"] = f"tri-{number:02d}"
    return [_ordered(item) for item in items]


# --- answers --------------------------------------------------------------------------


def _chosen_types(rng: random.Random, types: list[IncidentType]) -> list[IncidentType]:
    """Pick 20 incident kinds: one for every failure mode, then enough per machine to
    reach three, then fill up at random."""
    shuffled = types[:]
    rng.shuffle(shuffled)
    chosen: list[IncidentType] = []
    for mode in sorted({t[2] for t in types}):
        chosen.append(next(t for t in shuffled if t[2] == mode))
    for machine in sorted({t[0] for t in types}):
        for found in (t for t in shuffled if t[0] == machine and t not in chosen):
            if sum(1 for c in chosen if c[0] == machine) >= MIN_PER_MACHINE:
                break
            chosen.append(found)
    for found in shuffled:
        if len(chosen) >= ANSWERS_PER_SET:
            break
        if found not in chosen:
            chosen.append(found)
    return sorted(chosen[:ANSWERS_PER_SET], key=lambda t: (t[0], t[1] or "", t[2]))


def _answer_item(
    rng: random.Random, truth: Truth, kind: IncidentType, incident_id: str
) -> dict[str, Any]:
    machine, alarm, mode = kind
    procedure_id = truth.incident_details[incident_id].procedure_id
    question = _write_question(
        rng, truth, incident_id, ANSWER_WITH_ALARM, ANSWER_NO_ALARM
    )
    return {
        "source": "incident",
        "trap": None,
        "question": question,
        "truth_incident": incident_id,
        "hazard": _hazard(question),
        "expect": {
            "machine": machine,
            "alarm": alarm,
            "root_cause": mode,
            "fix": procedure_id,
            "parts": parts_of(truth, procedure_id),
            "lockout": _requires_lockout(truth, procedure_id),
            "truth_answer": _truth_answer(truth, mode, procedure_id),
        },
    }


def _truth_answer(truth: Truth, mode: str, procedure_id: str) -> str:
    return (
        f"Root cause: {truth.nodes[mode].props['name']}. Fix: "
        f"{truth.nodes[procedure_id].props['title']}."
    )


def _traps(truth: Truth) -> list[dict[str, Any]]:
    def trap(name: str, question: str, expect: dict[str, Any]) -> dict[str, Any]:
        return {
            "source": "trap",
            "trap": name,
            "question": question,
            "truth_incident": None,
            "expect": expect,
        }

    return [
        trap(
            "unknown_machine",
            "Line 4 robot is down",
            {
                "status": ["clarify"],
                "reason_contains": ["RW-01", "Line 3"],
                "no_causes": True,
            },
        ),
        trap(
            "vague_symptom",
            "the CNC is making a weird noise",
            {"status": ["confirm", "clarify"], "chips": True},
        ),
        trap(
            "out_of_scope",
            "can you approve my leave for Friday",
            {"status": ["out_of_scope"], "referral": True, "no_retrieval": True},
        ),
        trap(
            "conflicting_notes",
            "LT-01 VT-018 leak rate high on every part, whats the fix pls",
            {
                "note_contains": ["INC-2026-010"],
                "truth_answer": _truth_answer(truth, "FM-FIXTURE-SEAL", "PROC-LT-02"),
            },
        ),
        trap(
            "zero_stock",
            "RW-01 RB-551 gripper not seeing parts, what do I need to fix it",
            {
                "fix": "PROC-RW-01",
                "part": {"part_no": "GRP-FNG-12", "enough": False},
                "note_contains": ["GRP-FNG-12", "not enough in stock"],
                "truth_answer": _truth_answer(truth, "FM-GRIPPER-WEAR", "PROC-RW-01"),
            },
        ),
    ]


def build_answers(truth: Truth) -> list[dict[str, Any]]:
    """The answer set: 20 distinct incidents, then the 5 traps. It uses the last incident
    of each kind, so the question differs from the triage set's."""
    rng = random.Random(SEED + 1)
    by_type = _incidents_by_type(truth)
    items = [
        _answer_item(rng, truth, kind, by_type[kind][-1])
        for kind in _chosen_types(rng, list(by_type))
    ]
    items += _traps(truth)
    for number, item in enumerate(items, start=1):
        item["id"] = f"ans-{number:02d}"
    return [_ordered(item) for item in items]


def _ordered(item: dict[str, Any]) -> dict[str, Any]:
    return {"id": item["id"], **{k: v for k, v in item.items() if k != "id"}}


# --- files ----------------------------------------------------------------------------


def write(folder: Path = GOLDEN_DIR, truth: Truth | None = None) -> dict[str, int]:
    """Write both sets as JSON lines and return the number of items in each."""
    truth = truth or load_truth()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    built = {"triage": build_triage(truth), "answers": build_answers(truth)}
    for name, items in built.items():
        text = "".join(
            json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n"
            for item in items
        )
        (folder / f"{name}.jsonl").write_text(text, encoding="utf-8", newline="\n")
    return {name: len(items) for name, items in built.items()}


def load(name: str, folder: Path = GOLDEN_DIR) -> list[dict[str, Any]]:
    """The items of a saved set, "triage" or "answers"."""
    if name not in NAMES:
        raise ValueError(f"the golden sets are {', '.join(NAMES)}; got {name!r}")
    path = Path(folder) / f"{name}.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def main() -> int:
    argparse.ArgumentParser(prog="python -m app.evals.golden").parse_args()
    counts = write()
    for name, count in counts.items():
        print(f"wrote {GOLDEN_DIR.relative_to(ROOT)}/{name}.jsonl: {count} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
