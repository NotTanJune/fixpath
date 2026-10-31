"""Helpers shared by the extractors that need no model.

quote builds the evidence for one fact. The confidence values say how far each kind
of source can be trusted. note_missing records a table column that could not be found.
"""

from app.ingest import headers
from app.ingest.headers import ColumnMap
from app.ingest.tables import Located
from app.ingest.types import MAX_QUOTE, EvidenceDraft, ExtractorName

INVENTORY_CONFIDENCE = 0.9  # hand-kept stock lists, read cell by cell
LOG_CONFIDENCE = 0.95  # a machine wrote the line
CUT_TEXT_CONFIDENCE = 0.6  # for the message of a vendor whose log cuts it short
CSV_CONFIDENCE = 0.9  # typed by hand into an export, read cell by cell


def quote(
    doc_path: str, text: str, extractor: ExtractorName, confidence: float
) -> list[EvidenceDraft]:
    """The evidence for one fact: one quote, cut to the length limit."""
    return [
        EvidenceDraft(
            doc_path=doc_path,
            quote=text[:MAX_QUOTE],
            extractor=extractor,
            confidence=confidence,
        )
    ]


def counted(count: int, one: str, many: str) -> str:
    """A note worded for one item or for several."""
    return (one if count == 1 else many).format(n=count)


def note_missing(located: Located, columns: ColumnMap, notes: list[str]) -> None:
    names = [c.name for c in headers.missing_required(located.layout, columns)]
    if names:
        notes.append(f"no column found for {', '.join(names)} in {located.name}")
