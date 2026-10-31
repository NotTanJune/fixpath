"""Decides what kind of file each raw file is, the order to read them in, and which reader to use.

classify looks at the file extension first, then the first lines of a text file, and
last the folder the file sits in. A handover saved in the wrong folder is still
recognised as a handover. ordered puts files in processing order, and extractor_for
picks the reader for a file.
"""

from collections.abc import Callable, Iterable
from pathlib import Path

from app.ingest import headers, layouts, parsers
from app.ingest.extract_document import extract_document, names_procedure
from app.ingest.extract_manual import extract_manual
from app.ingest.extract_text import MARKDOWN_NAME, extract_note, starts_handover
from app.ingest.normalize import AliasTable
from app.ingest.types import Extraction

# The document type for each folder of raw files.
FOLDER_TYPES = {
    "logs": "log",
    "inventory": "inventory",
    "manuals": "manual",
    "maintenance": "maintenance_note",
    "handovers": "handover",
    "procedures": "procedure",
}

# The order files are read in. The inventory comes first because it gives the machine
# names that later files need. Handovers come last.
PROCESSING_ORDER = (
    "inventory",
    "manual",
    "log",
    "maintenance_note",
    "procedure",
    "handover",
)

# Within one document type, workbooks come before CSVs, and CSVs before loose notes.
# Everything else goes by path.
_SUFFIX_RANK = {".xlsx": 0, ".xlsm": 0, ".csv": 1}
_DEFAULT_RANK = 2

_WORKBOOKS = (".xlsx", ".xlsm")
_LAYOUT_TYPES = {
    layouts.SPARES.name: "inventory",
    layouts.MACHINES.name: "inventory",
    layouts.MAINTENANCE.name: "maintenance_note",
}
_TEXT_SUFFIX = ".txt"
_SNIFF_BYTES = 8192
_SNIFF_LINES = 30

Extractor = Callable[..., Extraction]  # takes a path, the alias table and settings

# The reader for each document type and file extension. Handovers and loose notes go
# to one model-based reader for free text, and manuals go to another.
REGISTRY: dict[tuple[str, str], Extractor] = {
    ("log", ".log"): parsers.parse_log,
    ("inventory", ".xlsx"): parsers.parse_inventory,
    ("inventory", ".csv"): parsers.parse_inventory,
    ("maintenance_note", ".csv"): parsers.parse_maintenance_csv,
    ("handover", ".txt"): extract_note,
    ("maintenance_note", ".txt"): extract_note,
    ("manual", ".pdf"): extract_manual,
    ("procedure", ".txt"): extract_document,
}


def _first_lines(path: Path) -> list[str]:
    """Return the first lines of a text file, or none if it cannot be read."""
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read(_SNIFF_BYTES)
    except OSError:
        return []
    return text.splitlines()[:_SNIFF_LINES]


def _sniff(
    lines: list[str], *, text: bool = False, markdown: bool = False
) -> str | None:
    """Guess the document type from the first lines of a text file, if possible.

    Plain text is never treated as a stock list, and its title may mark it as a
    procedure. Markdown files may start their title with heading marks.
    """
    first = next((line for line in lines if line.strip()), "")
    if starts_handover(first, markdown):
        return "handover"
    if text and names_procedure(first, markdown):
        return "procedure"
    if parsers.is_log_header(first):
        return "log"
    layouts = [
        layout
        for layout in headers.LAYOUTS
        if not text or _LAYOUT_TYPES[layout.name] != "inventory"
    ]
    found = headers.sniff(lines, layouts)
    return _LAYOUT_TYPES[found[0].name] if found else None


class UnknownFile(ValueError):
    """No extension, content or folder says what kind of file this is."""


def classify(path: Path | str) -> str:
    """Return the document type of a raw file, or raise UnknownFile if it cannot be told."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "manual"
    if suffix in _WORKBOOKS:
        return "inventory"
    if suffix == ".log":
        return "log"
    name = path.name.lower()
    sniffed = _sniff(
        _first_lines(path),
        text=suffix == _TEXT_SUFFIX,
        markdown=name.endswith(MARKDOWN_NAME),
    )
    if sniffed:
        return sniffed
    folder = FOLDER_TYPES.get(path.parent.name.lower())
    if folder:
        return folder
    raise UnknownFile(f"cannot tell what kind of file {path} is")


def ordered(paths: Iterable[Path | str]) -> list[Path]:
    """Return the paths in processing order, leaving out hidden files.

    The result is the same whatever order the input is in.
    """

    def key(path: Path) -> tuple[int, int, str]:
        return (
            PROCESSING_ORDER.index(classify(path)),
            _SUFFIX_RANK.get(path.suffix.lower(), _DEFAULT_RANK),
            path.as_posix(),
        )

    visible = (Path(path) for path in paths if not Path(path).name.startswith("."))
    return sorted(visible, key=key)


def extractor_for(path: Path | str) -> Extractor | None:
    """Return the reader for a file, or None if its kind has no reader yet."""
    path = Path(path)
    return REGISTRY.get((classify(path), path.suffix.lower()))
