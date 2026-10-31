"""Defines the data shapes that the ingestion stages pass to each other.

A reader turns one raw file into an Extraction: candidate nodes and links, each with
the quote that supports it, plus text chunks and warnings. Later stages produce a
Resolution with merged items, review items and merge decisions. These shapes can be
saved as JSON and read back unchanged. They do not check the rules themselves,
because the validator does that.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.ontology import DocumentProps

# The document types are defined once, in the ontology.
DocType = DocumentProps.model_fields["doc_type"].annotation

# The longest evidence quote, in characters.
MAX_QUOTE = 300

ExtractorName = Literal[
    "log-parser",
    "inventory-normalizer",
    "csv-parser",
    "sol-text",
    "sol-manual",
    "sol-doc",
    "linker",
]

# Text that stands for "not known", as hand-kept files use for an empty cell.
_PLACEHOLDERS = frozenset({"", "na", "n/a", "-", "?"})


def is_missing(value: Any) -> bool:
    """Say whether a value says nothing: None, an empty list, blank text or a placeholder.

    Zero and False count as real values.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in _PLACEHOLDERS
    if isinstance(value, list):
        return not value
    return False


class EvidenceDraft(BaseModel):
    """Where a candidate came from: a quote from one raw file.

    doc_path is relative to data/raw. quote is a verbatim piece of the source of at
    most 300 characters. For a spreadsheet, which has no running text, it is the
    cell values of the row joined by " | ".
    """

    doc_path: str
    page: int | None = None
    quote: str = Field(max_length=MAX_QUOTE)
    extractor: ExtractorName
    confidence: float = Field(ge=0, le=1)


class NodeRef(BaseModel):
    """A reference to a node. key is the canonical id when the extractor knows it for
    certain, otherwise the raw mention text."""

    model_config = ConfigDict(frozen=True)

    type: str
    key: str


class CandidateNode(BaseModel):
    """A node an extractor found. canonical is true only when ref.key is a verified
    canonical id, such as a tag from the Machines sheet."""

    ref: NodeRef
    label: str
    props: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    canonical: bool = False


class CandidateEdge(BaseModel):
    """A relation an extractor found between two node references."""

    src: NodeRef
    rel: str
    dst: NodeRef
    props: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceDraft] = Field(default_factory=list)


class Chunk(BaseModel):
    """A piece of a document's text for search. id is the document id, the page (or
    0) and the position in reading order, joined by dashes."""

    id: str
    doc_path: str
    page: int | None = None
    ord: int
    text: str


class Extraction(BaseModel):
    """Everything one extractor found in one file. notes holds the warnings a person
    should see, such as "12 of 1385 lines could not be parsed"."""

    doc_path: str
    doc_type: DocType
    extractor: ExtractorName
    nodes: list[CandidateNode] = Field(default_factory=list)
    edges: list[CandidateEdge] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --- Resolution -----------------------------------------------------------------
# The resolver turns all extractions into one consistent set of nodes and links.
# Its output is plain data too, so the Pipeline view can show it.


class ResolvedNode(BaseModel):
    """A canonical node. sources lists the doc paths that contributed to it."""

    id: str
    type: str
    label: str
    props: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class ResolvedEdge(BaseModel):
    """A relation between two canonical ids."""

    src: str
    rel: str
    dst: str
    props: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceDraft] = Field(default_factory=list)


class MergeDecision(BaseModel):
    """How one mention, or one pair of incident records, was decided.

    decision is rule (a deterministic rule matched), merge (the decider said yes),
    queue (the decider was unsure, so a person decides), unresolved (no candidate
    was good enough, or the decider gave no answer) or new (a pair that is not the
    same incident). candidate, p and backend are None when no decider was asked.
    """

    doc_path: str
    type: str
    mention: str
    candidate: str | None = None
    p: float | None = None
    decision: Literal["rule", "merge", "queue", "unresolved", "new"]
    backend: str | None = None


class ReviewItem(BaseModel):
    """Something a person should look at: a merge that was not certain, a conflict
    between sources, a mention that stayed unresolved, or a root cause that was not
    clear."""

    kind: Literal["merge", "conflict", "unresolved", "link"]
    doc_paths: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None


class Resolution(BaseModel):
    """The result of resolving and linking. Nodes are sorted by id and edges by
    (src, rel, dst)."""

    nodes: list[ResolvedNode] = Field(default_factory=list)
    edges: list[ResolvedEdge] = Field(default_factory=list)
    merges: list[MergeDecision] = Field(default_factory=list)
    review: list[ReviewItem] = Field(default_factory=list)
