"""Checks that the text of the manuals and the values of the workbook have not changed.

Each is compared with a saved hash. The saved answers, the graph and the evals all
depend on those exact words, so the look of a document (colours, fonts, column widths)
must never change them. A failure here means a word, a line break or a cell moved.
Fix the style, not the hash, unless the words are meant to change. In that case the
saved model answers must be made again.
"""

import hashlib
import json
import random
from pathlib import Path

import pytest
from conftest import SEED
from openpyxl import load_workbook

from app.config import RAW_DIR
from app.generator.render_inventory import WORKBOOK_FILE, render_inventory
from app.generator.render_manuals import render_manuals
from app.generator.world import build_world
from app.ingest.manual_chunks import read_pages

PAGES = {
    "BH-250 service manual.pdf": (
        "349943797ff55cce1cbdfd713cb77c13dd6d5f1e438638e22627fe4be2fc4d2a"
    ),
    "Kessler_KX-500_Operator_Maintenance_Manual_rev3.pdf": (
        "6e5aa124808804be7d796410ffbfb23804149353da0c341eed2b3c65ad98ee13"
    ),
    "Orbis_OR6_cell_maintenance_guide.pdf": (
        "c9d3af441edc7605aa8de274581069ab152f8ff3484da14045e38dab09f5a6eb"
    ),
    "VT-LD20_manual_EN.pdf": (
        "79619d2e044306708fc69941247bc83d923c8f845d8bd30d10aa23d8daf45628"
    ),
}
WORKBOOK_VALUES = "717591b68d8ed5f997f5b8f3b846b0398f69da6be6b0e95c63c41604b5cf38a0"


def pages_hash(path: Path) -> str:
    """The sha256 of the page texts of a PDF as read_pages gives them, as JSON."""
    blob = json.dumps(read_pages(path), indent=1, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def values_hash(path: Path) -> str:
    """The sha256 of the sheet names, merged ranges and every cell value of a
    workbook, with the type of each value."""
    sheets = {
        sheet.title: {
            "dims": sheet.dimensions,
            "merged": sorted(str(r) for r in sheet.merged_cells.ranges),
            "cells": [
                [(repr(c.value), type(c.value).__name__) for c in row]
                for row in sheet.iter_rows()
            ],
        }
        for sheet in load_workbook(path, data_only=True).worksheets
    }
    return hashlib.sha256(json.dumps(sheets, indent=1).encode()).hexdigest()


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("pinned")
    world = build_world(SEED)
    render_manuals(world, random.Random(SEED + 4), out)
    render_inventory(world, random.Random(SEED + 3), out)
    return out / "raw"


@pytest.mark.parametrize("name", PAGES)
def test_a_fresh_manual_reads_as_the_pinned_text(rendered, name):
    assert pages_hash(rendered / "manuals" / name) == PAGES[name]


@pytest.mark.parametrize("name", PAGES)
def test_the_committed_manual_reads_as_the_pinned_text(name):
    assert pages_hash(RAW_DIR / "manuals" / name) == PAGES[name]


def test_a_fresh_workbook_holds_the_pinned_values(rendered):
    assert values_hash(rendered / "inventory" / WORKBOOK_FILE) == WORKBOOK_VALUES


def test_the_committed_workbook_holds_the_pinned_values():
    assert values_hash(RAW_DIR / "inventory" / WORKBOOK_FILE) == WORKBOOK_VALUES
