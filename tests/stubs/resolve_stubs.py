"""Support code for the tests of the resolver, the incident step and the linker.

ScriptedDecider stands in for the real decider: a test gives it a script of answers, and
it records every question it is asked. The builders make hand-built extractions. The
alias table, the spares rows and the manual text are copied from the real files, so the
fixtures match what the extractors produce.
"""

from collections.abc import Callable, Iterable
from typing import Any

from app.decide import Answer, Noul
from app.ingest.normalize import AliasTable, MachineEntry
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    EvidenceDraft,
    Extraction,
    NodeRef,
)

ALIASES = AliasTable(
    [
        MachineEntry(
            "CNC-01", "KX5-10442", "KX-500", ("CNC1", "CNC 1", "MC-1", "Kessler 1")
        ),
        MachineEntry(
            "CNC-02", "KX5-10457", "KX-500", ("CNC2", "CNC 2", "MC-2", "Kessler 2")
        ),
        MachineEntry(
            "HP-01",
            "BH250-0091",
            "BH-250",
            ("Press 2", "HP1", "hyd press", "the press", "PR-2"),
        ),
        MachineEntry(
            "RW-01",
            "OR6-2231",
            "OR-6",
            ("robot cell", "RC-1", "robot", "deburr cell"),
        ),
        MachineEntry(
            "LT-01",
            "VT20-5512",
            "VT-LD20",
            ("leak tester", "LT1", "leak test", "VT station"),
        ),
    ]
)

# Rows of the Spares sheet the tests use: part number, description, quantity, bin.
SPARES_ROWS = [
    ("SEAL-KIT-50", "Main cylinder seal kit, 50 mm rod", 3, "B-12"),
    ("OIL-HLP46-20L", "Hydraulic oil HLP 46, 20 litre pail", 9, "C-07"),
    ("SEAL-RV-25", "Relief valve seal kit", 1, "B-15"),
    ("BRG-6205-2RS", "Deep groove ball bearing 6205-2RS", 6, "A-02"),
]
# Part rows of the BH-250 manual.
MANUAL_PARTS = {
    "SEAL-KIT-50": "SK-50 seal kit (order no. SEAL-KIT-50)",
    "OIL-HLP46-20L": "Hydraulic oil HLP 46, 20 litre pail",
    "SEAL-RV-25": "Relief valve seal kit",
    "FLT-HYD-10": "Hydraulic return filter 10 micron",
}

INVENTORY = "inventory/spares_2026-10.xlsx"
OLD_STOCK = "inventory/spares_old.csv"
BH250 = "manuals/BH-250 service manual.pdf"
KX500 = "manuals/Kessler_KX-500_Operator_Maintenance_Manual_rev3.pdf"
EXPORT = "maintenance/maint_log_export_2026.csv"
NOTE = "maintenance/note_from_arun_CNC2_spindle.txt"
HANDOVER = "handovers/Handover_20260822_B.txt"
LOG = "logs/HP01.log"


def evidence(
    doc_path: str,
    quote: str = "q",
    extractor: str = "csv-parser",
    confidence: float = 0.9,
):
    return [
        EvidenceDraft(
            doc_path=doc_path, quote=quote, extractor=extractor, confidence=confidence
        )
    ]


def node(
    type: str,
    key: str,
    *,
    doc: str,
    label: str | None = None,
    props: dict[str, Any] | None = None,
    canonical: bool = True,
    quote: str | None = None,
    extractor: str = "csv-parser",
) -> CandidateNode:
    return CandidateNode(
        ref=NodeRef(type=type, key=key),
        label=label if label is not None else key,
        props=props or {},
        evidence=evidence(doc, quote or f"{type} {key}", extractor),
        canonical=canonical,
    )


def edge(
    src: tuple[str, str],
    rel: str,
    dst: tuple[str, str],
    *,
    doc: str,
    props: dict[str, Any] | None = None,
    quote: str | None = None,
    extractor: str = "csv-parser",
) -> CandidateEdge:
    return CandidateEdge(
        src=NodeRef(type=src[0], key=src[1]),
        rel=rel,
        dst=NodeRef(type=dst[0], key=dst[1]),
        props=props or {},
        evidence=evidence(doc, quote or f"{src[1]} {rel} {dst[1]}", extractor),
    )


EXTRACTORS = {
    "manual": "sol-manual",
    "inventory": "inventory-normalizer",
    "log": "log-parser",
    "handover": "sol-text",
}


def extraction(
    doc_path: str,
    doc_type: str,
    nodes: Iterable[CandidateNode] = (),
    edges: Iterable[CandidateEdge] = (),
    extractor: str | None = None,
) -> Extraction:
    if extractor is None:
        extractor = (
            "csv-parser"
            if doc_path.endswith(".csv") and doc_type == "maintenance_note"
            else (
                "sol-text" if doc_type == "maintenance_note" else EXTRACTORS[doc_type]
            )
        )
    return Extraction(
        doc_path=doc_path,
        doc_type=doc_type,
        extractor=extractor,
        nodes=list(nodes),
        edges=list(edges),
    )


def parts_fixture() -> list[Extraction]:
    """The inventory and the BH-250 manual as the extractors give them, with parts from both."""
    inventory = extraction(
        INVENTORY,
        "inventory",
        [
            node(
                "Part",
                part_no,
                doc=INVENTORY,
                label=description,
                props={
                    "part_no": part_no,
                    "description": description,
                    "qty_on_hand": qty,
                    "bin": bin_,
                    "stock_as_of": "2026-10-15",
                },
                extractor="inventory-normalizer",
            )
            for part_no, description, qty, bin_ in SPARES_ROWS
        ],
    )
    manual = extraction(
        BH250,
        "manual",
        [
            node(
                "Part",
                part_no,
                doc=BH250,
                label=description,
                props={"part_no": part_no, "description": description},
                extractor="sol-manual",
            )
            for part_no, description in MANUAL_PARTS.items()
        ],
    )
    return [inventory, manual]


# --- The scripted decider ---


def noul_answer(p: float, backend: str = "fake") -> Answer:
    return Answer("noul", p=p, probabilities={"yes": p, "no": 1 - p}, backend=backend)


def choice_answer(
    choice: str, probabilities: dict[str, float], backend: str = "fake"
) -> Answer:
    return Answer("choice", choice=choice, probabilities=probabilities, backend=backend)


Script = Callable[[str, Any], Any]


class ScriptedDecider:
    """Answers each question from a script and records what was asked.
    The script can return an answer, a refusal, or an error, which is raised."""

    name = "fake"
    model = "fake"

    def __init__(self, script: Script | float = 0.0):
        self.script = script if callable(script) else (lambda purpose, state: script)
        self.calls: list[tuple[str, Any, dict]] = []

    def ask(self, state, questions, purpose):
        self.calls.append((purpose, state, questions))
        result = self.script(purpose, state)
        if isinstance(result, BaseException):
            raise result
        answers = {}
        for qid, question in questions.items():
            if result is None:
                answers[qid] = Answer("refusal", backend=self.name)
            elif isinstance(result, Answer):
                answers[qid] = result
            elif isinstance(question, Noul):
                answers[qid] = noul_answer(float(result))
            else:
                raise TypeError(f"the script gave {result!r} for a {type(question)}")
        return answers

    def purposes(self) -> list[str]:
        return [purpose for purpose, _, _ in self.calls]

    def states(self, purpose: str) -> list[Any]:
        return [state for p, state, _ in self.calls if p == purpose]
