"""Shared stubs and sample data for the ingestion parser tests.

FakeModel stands in for the real model, and the no_model fixture makes any test that
reaches the model fail unless it installs a fake first. The sample header rows come
from the real files. The unknown rows are layouts the parsers cannot read alone, paired
with the answers a model would give. The file also holds the paths of the real files and
a few small helpers.
"""

from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from app import llm
from app.config import RAW_DIR
from app.ingest import parsers
from app.ingest.headers import HeaderChoice, HeaderMap
from app.ingest.types import Extraction

WORKBOOK = RAW_DIR / "inventory" / "spares_2026-10.xlsx"
OLD_COUNT = RAW_DIR / "inventory" / "spares_old.csv"
EXPORT = RAW_DIR / "maintenance" / "maint_log_export_2026.csv"
ALIASES = parsers.build_alias_table(WORKBOOK)

SPARES_HEADERS = [
    "Part No.",
    "Description",
    "Qty",
    "UoM",
    "Bin",
    "Lead time",
    "Cost (SGD)",
    "Remarks",
]
MACHINE_HEADERS = [
    "Asset tag",
    "Description",
    "Make",
    "Model",
    "Serial no.",
    "Location",
    "Also called",
]
MAINTENANCE_HEADERS = [
    "Date",
    "Mach.",
    "Problem desc",
    "Action taken",
    "Parts",
    "Tech",
    "Downtime (min)",
    "Ref",
]

UNKNOWN_SPARES = ["Item Ref", "What", "How many", "Where"]
UNKNOWN_MACHINES = [
    "Unit ref",
    "Kind",
    "Builder",
    "Variant",
    "Ser. ref",
    "Whereabouts",
    "Pet names",
]
UNKNOWN_MAINTENANCE = [
    "Raised",
    "Item",
    "Story",
    "Rectification",
    "Consumables",
    "Attended",
    "Lost mins",
    "Docket",
]


def spares_answer() -> HeaderMap:
    """What a model would answer for the unknown spares layout."""
    return HeaderMap(
        choices=[
            HeaderChoice(table="spares", field="part_no", header="Item Ref"),
            HeaderChoice(table="spares", field="description", header="What"),
            HeaderChoice(table="spares", field="qty", header="How many"),
            HeaderChoice(table="spares", field="bin", header="Where"),
            HeaderChoice(table="spares", field="cost", header=None),
        ]
    )


def machines_answer() -> HeaderMap:
    """What a model would answer for the unknown machines layout."""
    fields = ["tag", "description", "make", "model", "serial", "location", "aliases"]
    return HeaderMap(
        choices=[
            HeaderChoice(table="machines", field=name, header=header)
            for name, header in zip(fields, UNKNOWN_MACHINES, strict=True)
        ]
    )


def maintenance_answer() -> HeaderMap:
    """What a model would answer for the unknown maintenance layout."""
    fields = [
        "date",
        "machine",
        "problem",
        "action",
        "parts",
        "tech",
        "downtime",
        "ref",
    ]
    return HeaderMap(
        choices=[
            HeaderChoice(table="maintenance", field=name, header=header)
            for name, header in zip(fields, UNKNOWN_MAINTENANCE, strict=True)
        ]
    )


class FakeModel:
    """Stands in for the real model. It records each call and returns the answer it was given."""

    def __init__(self, answer: HeaderMap | None = None):
        self.answer = answer or HeaderMap(choices=[])
        self.calls: list[dict] = []

    def __call__(
        self,
        role,
        schema,
        system,
        user,
        purpose,
        reasoning_effort="medium",
        *,
        settings=None,
    ):
        self.calls.append(
            {
                "role": role,
                "schema": schema,
                "system": system,
                "user": user,
                "purpose": purpose,
                "reasoning_effort": reasoning_effort,
                "settings": settings,
            }
        )
        return self.answer


def refuse(*args, **kwargs):
    raise AssertionError("the deterministic extractors must not call a model")


@pytest.fixture(autouse=True, scope="module")
def no_model():
    """Fail any test that reaches the model without installing a fake first."""
    patch = pytest.MonkeyPatch()
    patch.setattr(llm, "structured", refuse)
    yield
    patch.undo()


@pytest.fixture
def fake_model(monkeypatch) -> FakeModel:
    """A fake model for one test."""
    fake = FakeModel()
    monkeypatch.setattr(llm, "structured", fake)
    return fake


# --- Helpers ---


def count_by_type(extraction: Extraction) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in extraction.nodes:
        counts[node.ref.type] = counts.get(node.ref.type, 0) + 1
    return counts


def count_by_relation(extraction: Extraction) -> dict[str, int]:
    counts: dict[str, int] = {}
    for edge in extraction.edges:
        counts[edge.rel] = counts.get(edge.rel, 0) + 1
    return counts


def nodes_of(extraction: Extraction, node_type: str):
    return [node for node in extraction.nodes if node.ref.type == node_type]


def sheet_rows(sheet_name: str) -> list[tuple]:
    """The rows of one sheet of the real workbook."""
    workbook = load_workbook(WORKBOOK, read_only=True, data_only=True)
    try:
        return list(workbook[sheet_name].iter_rows(values_only=True))
    finally:
        workbook.close()


def machine_rows() -> list[tuple]:
    return [row for row in sheet_rows("Machines")[1:] if row[0]]


def spares_rows() -> list[tuple]:
    return [row for row in sheet_rows("Spares")[3:] if row[0]]


def write_workbook(
    path: Path,
    *,
    title: str | None,
    headers_row: list[str],
    rows: list[list],
    machines: list[list] | None = None,
    machine_headers: list[str] = MACHINE_HEADERS,
) -> Path:
    """A workbook shaped like the stores file, with an optional Machines sheet."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Spares"
    if title:
        sheet["A1"] = title
    for column, header in enumerate(headers_row, start=1):
        sheet.cell(row=3, column=column, value=header)
    for number, row in enumerate(rows, start=4):
        for column, value in enumerate(row, start=1):
            sheet.cell(row=number, column=column, value=value)
    if machines is not None:
        sheet = workbook.create_sheet("Machines")
        sheet.append(machine_headers)
        for row in machines:
            sheet.append(row)
    workbook.save(path)
    return path
