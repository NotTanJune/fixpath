"""Reads procedures, work instructions and checklists into facts for the graph.

The model reads the document once and lists the machines, parts, alarm codes and
problems it mentions, copying each exactly as written. The code then checks every
mention against a real line of the document and drops any it cannot find. Each fact
is backed by the line it came from.

extract_document is the entry point. names_procedure tells the file router whether a
first line looks like a procedure title.
"""

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import Settings
from app.ingest.evidence import quote
from app.ingest.normalize import (
    AliasTable,
    chunk_id,
    dashed_part,
    doc_path_for,
    document_id,
    normalize_alarm_code,
)
from app.ingest.types import (
    CandidateEdge,
    CandidateNode,
    Chunk,
    EvidenceDraft,
    Extraction,
    NodeRef,
)
from app.ontology import validate_id

EXTRACTOR = "sol-doc"
CONFIDENCE = 0.8  # a model read free text, and the quotes were checked
PURPOSE = "extract document"
EFFORT = "low"
TITLE_WORDS = 4  # a title names a procedure within its first words
NO_PREFIX = "DOC"  # the id prefix of a procedure for no known machine
_TITLE_NAMES = re.compile(
    r"\b(?:procedure|work instruction|checklist|sop|instructions?)\b"
)
_LOCKOUT = re.compile(r"\block(?:ed)?[\s-]?out\b|\bloto\b", re.IGNORECASE)
_PREFIX = re.compile(r"[A-Z]{2,3}")

# --- The schema -------------------------------------------------------------------
# Every field is required, so a value that may be missing is typed "or None" with no
# default.


class PartNeed(BaseModel):
    """A part or consumable that the document says is needed, and how many."""

    mention: str
    qty: int | None


class FailureModeMention(BaseModel):
    """A problem that the document says the procedure fixes: the words of the document
    and a short noun phrase for it."""

    phrase: str
    short_name: str


class DocExtraction(BaseModel):
    """What the model reads from one procedure or instruction."""

    title: str | None
    machine_mentions: list[str]
    steps: list[str]
    requires_lockout: bool
    minutes: int | None
    parts: list[PartNeed]
    alarm_mentions: list[str]
    failure_modes: list[FailureModeMention]


DOC_SYSTEM = """\
You read one document from a precision machining plant that describes how to do
something: a procedure, a work instruction, a checklist, an SOP or a service note. It
is plain text. A table is written one row to a line, with " | " between the cells.

Copy every piece of text exactly as it is written. Never correct, expand, translate or
normalize a mention, and never make up a value that the document does not state.

title is the title or first heading as written, without the words "Procedure:" or
"Work instruction" removed. machine_mentions holds each machine the document is for,
as written, such as "PR-2", "CNC 1" or "robot cell"; leave out machines that are only
named in passing. steps are the things to do, in order, as written, each as one
string, without their numbers. Include a checklist item as a step.

requires_lockout is true when the document says to lock out, isolate or release stored
energy before the work, and false when it says nothing about that. minutes is the time
the document gives for the work in whole minutes ("Allow 90 minutes" is 90, "2 hours" is
120); null when it gives none.

parts holds each spare part, consumable or tool that the document says is needed, with
the mention exactly as written (in a table, the cell of the part column) and qty as
the number when the document gives one; null when it gives none. alarm_mentions holds
every alarm code the document names, as written, such as "E221" or "Err:230".

failure_modes holds each problem or cause that the document says this procedure
fixes: phrase is the words of the document, such as "hydraulic oil temperature high",
and short_name is a short noun phrase for it such as "oil overheating". A document that
only lists alarm codes names no failure mode. Use an empty list when there is none.\
"""


# --- Routing ----------------------------------------------------------------------


def names_procedure(first_line: str, markdown: bool = False) -> bool:
    """Whether the first line names the document as a procedure, work instruction,
    checklist or similar. The router uses this too, so both agree on the type."""
    if not markdown and first_line.lstrip().startswith("#"):
        return False
    head = first_line.lstrip("# \t")
    words = re.findall(r"[a-z0-9]+", head.lower())[:TITLE_WORDS]
    return _TITLE_NAMES.search(" ".join(words)) is not None


# --- Reading the model's answer ---------------------------------------------------


def _ask(text: str, settings: Settings | None) -> DocExtraction:
    # Imported here so the extractors that need no model do not load the model client.
    from app import llm

    return llm.structured(
        "sol", DocExtraction, DOC_SYSTEM, text, PURPOSE, EFFORT, settings=settings
    )


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


class _Lines:
    """The lines of the document, used to find the line that holds a mention."""

    def __init__(self, doc_path: str, text: str) -> None:
        self.doc_path = doc_path
        self.lines = [line.strip() for line in text.splitlines() if line.strip()]
        self.squashed = [_squash(line) for line in self.lines]

    def find(self, mention: str | None) -> str | None:
        """The first line that contains the text, ignoring case and spacing."""
        wanted = _squash(mention or "")
        if not wanted:
            return None
        for line, squashed in zip(self.lines, self.squashed, strict=True):
            if wanted in squashed:
                return line
        return None

    def evidence(self, line: str) -> list[EvidenceDraft]:
        return quote(self.doc_path, line, EXTRACTOR, CONFIDENCE)


class _Builder:
    """Turns the model's answer into the facts of one extraction."""

    def __init__(self, answer: DocExtraction, lines: _Lines, aliases: AliasTable):
        self.answer = answer
        self.lines = lines
        self.aliases = aliases
        self.nodes: dict[tuple[str, str], CandidateNode] = {}
        self.edges: dict[tuple[str, str, str], CandidateEdge] = {}
        self.notes: list[str] = []

    def node(
        self,
        node_type: str,
        key: str,
        label: str,
        props: dict[str, Any],
        line: str,
        canonical: bool,
    ) -> CandidateNode:
        found = self.nodes.get((node_type, key))
        if found is None:
            found = CandidateNode(
                ref=NodeRef(type=node_type, key=key),
                label=label,
                props=props,
                evidence=self.lines.evidence(line),
                canonical=canonical,
            )
            self.nodes[node_type, key] = found
        else:
            for item in self.lines.evidence(line):
                if item not in found.evidence:
                    found.evidence.append(item)
        return found

    def edge(
        self,
        src: NodeRef,
        rel: str,
        dst: NodeRef,
        line: str,
        props: dict[str, Any] | None = None,
    ) -> None:
        key = (f"{src.type}:{src.key}", rel, f"{dst.type}:{dst.key}")
        if key not in self.edges:
            self.edges[key] = CandidateEdge(
                src=src,
                rel=rel,
                dst=dst,
                props=props or {},
                evidence=self.lines.evidence(line),
            )

    def _missing(self, what: str) -> None:
        self.notes.append(f"no line of the document holds {what}")

    def machines(self) -> str:
        """Add the machines and return the id prefix, taken from the first machine."""
        prefix = None
        for mention in self.answer.machine_mentions:
            mention = mention.strip()
            if not mention:
                continue
            tag = self.aliases.asset_for(mention)
            if tag is None:
                self.notes.append(f"machine {mention!r} is not in the alias table")
                continue
            line = self.lines.find(mention)
            if line is None:
                self._missing(f"the machine {mention!r}")
                continue
            self.node("Asset", tag, tag, {}, line, False)
            letters = tag.split("-")[0].upper()
            if prefix is None and _PREFIX.fullmatch(letters):
                prefix = letters
        if prefix is None:
            self.notes.append(
                f"no machine gives the procedure an id prefix, so {NO_PREFIX} is used"
            )
        return prefix or NO_PREFIX

    def procedure(self, key: str, prefix: str) -> CandidateNode:
        answer = self.answer
        title = (answer.title or "").strip()
        line = self.lines.find(title) if title else None
        if line is None:
            line = self.lines.lines[0]
            if title:
                self.notes.append(
                    "the title is not a line of the document, so the first line is its evidence"
                )
            title = title or line.lstrip("# \t")
        text = "\n".join(self.lines.lines)
        requires_loto = answer.requires_lockout
        if not requires_loto and _LOCKOUT.search(text):
            requires_loto = True
            self.notes.append(
                "the text mentions lockout, the model said it is not required; "
                "using lockout required"
            )
        minutes = answer.minutes if answer.minutes and answer.minutes > 0 else 0
        if not minutes:
            self.notes.append("no time was stated, so est_minutes is 0")
        props = {
            "id_prefix": prefix,
            "title": title,
            "steps": [step.strip() for step in answer.steps if step.strip()],
            "requires_loto": requires_loto,
            "est_minutes": minutes,
            "manual_ref": None,
        }
        return self.node("Procedure", key, title, props, line, False)

    def parts(self, procedure: NodeRef) -> None:
        for item in self.answer.parts:
            name = item.mention.strip()
            if not name:
                continue
            line = self.lines.find(name)
            if line is None:
                self._missing(f"the part {name!r}")
                continue
            part_key = dashed_part(name) or name
            self.node("Part", part_key, part_key, {}, line, False)
            if item.qty is None:
                self.notes.append(f"part {name!r} has no quantity, so 1 is assumed")
            self.edge(
                procedure,
                "REQUIRES",
                NodeRef(type="Part", key=part_key),
                line,
                {"qty": item.qty if item.qty is not None else 1},
            )

    def alarms(self) -> None:
        for mention in self.answer.alarm_mentions:
            mention = mention.strip()
            if not mention:
                continue
            line = self.lines.find(mention)
            if line is None:
                self._missing(f"the alarm code {mention!r}")
                continue
            key = normalize_alarm_code(mention) or mention
            self.node("AlarmCode", key, key, {}, line, False)

    def failure_modes(self, procedure: NodeRef) -> None:
        for item in self.answer.failure_modes:
            phrase, name = item.phrase.strip(), item.short_name.strip()
            slug = re.sub(r"[^A-Z0-9]+", "-", (name or phrase).upper()).strip("-")
            if not slug:
                continue
            line = self.lines.find(phrase)
            if line is None:
                self._missing(f"the cause {phrase!r}")
                continue
            key = f"FM-{slug}"
            self.node(
                "FailureMode",
                key,
                name or phrase,
                {"id": key, "name": name or phrase, "mechanism": phrase},
                line,
                validate_id("FailureMode", key),
            )
            self.edge(
                NodeRef(type="FailureMode", key=key), "RESOLVED_BY", procedure, line
            )

    def build(self, key: str) -> None:
        if not [step for step in self.answer.steps if step.strip()]:
            self.notes.append("no steps were found, so there is no procedure")
            return
        prefix = self.machines()
        procedure = self.procedure(key, prefix).ref
        self.parts(procedure)
        self.alarms()
        self.failure_modes(procedure)


def extract_document(
    path: Path, aliases: AliasTable, settings: Settings | None = None
) -> Extraction:
    """Read one procedure document into a procedure and the things it names.

    The procedure gets its final id later, once the graph shows which numbers are free.
    Machines, parts and alarm codes are added only so the document can be found from
    them. Settings are loaded when not given.
    """
    path = Path(path)
    doc_path = doc_path_for(path)
    text = path.read_text(encoding="utf-8-sig")
    extraction = Extraction(
        doc_path=doc_path, doc_type="procedure", extractor=EXTRACTOR
    )
    extraction.chunks.append(
        Chunk(
            id=chunk_id(document_id(doc_path), None, 0),
            doc_path=doc_path,
            page=None,
            ord=0,
            text=text,
        )
    )
    lines = _Lines(doc_path, text)
    if not lines.lines:
        extraction.notes.append("the document is empty")
        return extraction
    builder = _Builder(_ask(text, settings), lines, aliases)
    builder.build(f"{doc_path}#procedure")
    extraction.nodes = list(builder.nodes.values())
    extraction.edges = list(builder.edges.values())
    extraction.notes = builder.notes
    return extraction
