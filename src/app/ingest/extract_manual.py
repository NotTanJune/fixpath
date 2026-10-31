"""Reads vendor PDF manuals into searchable text and facts for the graph.

The manual text is cut into sections for search. The model reads the whole manual
once and lists components, failure modes, alarms with their causes, procedures and
spare parts. The code builds the facts from that list and backs each one with a real
line of the manual, such as the line holding an alarm code or a part number.

extract_manual is the entry point. lockout_in reads the lockout setting from a
procedure's own line, so the code does not rely only on the model.
"""

import itertools
import re
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ingest.manual_chunks import body_lines, chunk_text, read_pages
from app.ingest.manual_pages import Pages
from app.ingest.manual_schema import (
    CODE_LENGTH,
    EFFORT,
    LIKELIHOOD,
    MANUAL_SYSTEM,
    PURPOSE,
    ManualExtraction,
    ProcedureItem,
)
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

_SECTION_NUMBER = re.compile(r"(\d+\.\d+)\s")


# --- Reading the answer -----------------------------------------------------------


_LOCKOUT = re.compile(r"Lockout required:\s*(yes|no)\b", re.IGNORECASE)


def lockout_in(text: str) -> bool | None:
    """Whether the line says lockout is required, or None if it says nothing."""
    found = _LOCKOUT.search(text)
    return found.group(1).lower() == "yes" if found else None


def _norm(text: str) -> str:
    """A phrase in a plain form, so two spellings of it compare equal."""
    text = re.sub(
        r"\s*\((?:most likely|possible)\)\s*$", "", text.strip(), flags=re.IGNORECASE
    )
    return re.sub(r"\s+", " ", text).casefold()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", text.upper()).strip("-")


def _letters_and_digits(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _ask(pages: dict[int, str], settings: Settings | None) -> ManualExtraction:
    # Imported here so the extractors that need no model do not load the model client.
    from app import llm

    text = "\n\n".join(
        f"[page {number}]\n" + "\n".join(body_lines(page))
        for number, page in pages.items()
    )
    return llm.structured(
        "sol", ManualExtraction, MANUAL_SYSTEM, text, PURPOSE, EFFORT, settings=settings
    )


class _Builder:
    """Turns the model's answer into the facts of one extraction."""

    def __init__(
        self,
        answer: ManualExtraction,
        pages: Pages,
        aliases: AliasTable,
        doc_path: str,
    ) -> None:
        self.answer = answer
        self.pages = pages
        self.assets = aliases.assets_for_model(answer.model)
        self.doc_path = doc_path
        self.nodes: dict[tuple[str, str], CandidateNode] = {}
        self.edges: dict[tuple[str, str, str], CandidateEdge] = {}
        self.notes: list[str] = []
        self.component_codes: dict[str, str] = {}  # name key -> code
        self.mode_keys: dict[str, str] = {}  # phrase key -> failure mode id
        self.mode_lines: dict[str, int | None] = {}
        self.alarm_keys: dict[str, str] = {}
        self.alarm_evidence: dict[str, list[EvidenceDraft]] = {}
        self.procedure_lines: dict[str, int | None] = {}
        self.procedure_evidence: dict[str, list[EvidenceDraft]] = {}
        self.troubleshooting = self.pages.find("troubleshooting table")
        self.parts_list = self.pages.find("spare parts list")

    def _ev(self, index: int | None, count: int = 1) -> list[EvidenceDraft]:
        return self.pages.evidence(index, count, self.doc_path)

    def _missing(self, what: str) -> None:
        self.notes.append(f"no line of the manual holds {what}")

    def node(
        self,
        node_type: str,
        key: str,
        label: str,
        props: dict[str, Any],
        evidence: list[EvidenceDraft],
        canonical: bool,
    ) -> CandidateNode:
        existing = self.nodes.get((node_type, key))
        if existing is None:
            existing = CandidateNode(
                ref=NodeRef(type=node_type, key=key),
                label=label,
                props=props,
                evidence=[],
                canonical=canonical,
            )
            self.nodes[node_type, key] = existing
        for item in evidence:
            if item not in existing.evidence:
                existing.evidence.append(item)
        return existing

    def edge(
        self,
        src: tuple[str, str],
        rel: str,
        dst: tuple[str, str],
        evidence: list[EvidenceDraft],
        props: dict[str, Any] | None = None,
    ) -> None:
        key = (f"{src[0]}:{src[1]}", rel, f"{dst[0]}:{dst[1]}")
        if key not in self.edges:
            self.edges[key] = CandidateEdge(
                src=NodeRef(type=src[0], key=src[1]),
                rel=rel,
                dst=NodeRef(type=dst[0], key=dst[1]),
                props=props or {},
                evidence=evidence,
            )

    def assets_and_components(self) -> None:
        answer = self.answer
        if not self.assets:
            self.notes.append(
                f"no asset in the alias table has the model {answer.model!r}, "
                "so there are no components"
            )
            return
        model_line = self.pages.find(answer.model)
        for tag in self.assets:
            self.node(
                "Asset",
                tag,
                tag,
                {"id": tag, "vendor": answer.vendor, "model": answer.model},
                self._ev(model_line),
                True,
            )
        used: set[str] = set()
        for item in answer.components:
            code = (
                _letters_and_digits(item.code)[:CODE_LENGTH]
                or _letters_and_digits(item.name)[:CODE_LENGTH]
            )
            if code in used or not code:
                base = code
                for n in itertools.count(1):
                    code = f"{base[: CODE_LENGTH - len(str(n))]}{n}"
                    if code not in used:
                        break
            used.add(code)
            key = _norm(item.name)
            self.component_codes[key] = code
            line = self.pages.find_after(item.name, self.parts_list)
            evidence = self._ev(line)
            if not evidence:
                self._missing(f"the component {item.name!r}")
            for tag in self.assets:
                component = f"{tag}-{code}"
                self.node(
                    "Component",
                    component,
                    item.name.strip(),
                    {"id": component, "name": item.name.strip(), "asset_id": tag},
                    evidence,
                    validate_id("Component", component),
                )
                self.edge(
                    ("Asset", tag), "HAS_COMPONENT", ("Component", component), evidence
                )

    def component_ids(self, name: str, user: str) -> list[str]:
        """The component ids for a name on every machine, or none with a note."""
        code = self.component_codes.get(_norm(name))
        if code is None:
            if self.assets:
                self.notes.append(f"{user}: {name!r} matches no component")
            return []
        return [f"{tag}-{code}" for tag in self.assets]

    def failure_modes(self) -> None:
        for item in self.answer.failure_modes:
            slug = _slug(item.short_name) or _slug(item.phrase)
            key = f"FM-{slug}"
            line = self.pages.find_after(item.phrase, self.troubleshooting)
            evidence = self._ev(line)
            if not evidence:
                self._missing(f"the cause {item.phrase!r}")
            self.node(
                "FailureMode",
                key,
                item.short_name.strip() or item.phrase.strip(),
                {
                    "id": key,
                    "name": item.short_name.strip(),
                    "mechanism": item.phrase.strip(),
                },
                evidence,
                validate_id("FailureMode", key),
            )
            self.mode_keys[_norm(item.phrase)] = key
            self.mode_lines[key] = line
            for component in self.component_ids(
                item.component_name, f"failure mode {key}"
            ):
                self.edge(
                    ("FailureMode", key), "AFFECTS", ("Component", component), evidence
                )

    def alarms(self) -> None:
        for item in self.answer.alarms:
            code = item.code.strip()
            key = normalize_alarm_code(code) or code
            # In an alarm table the code sits alone on a line, then its message and
            # severity. A bulletin names it in a heading or sentence instead.
            line = self.pages.find(code, exact=True)
            if line is None:
                line = self.pages.find(key, exact=True)
            count = 3  # the code, its message and its severity
            if line is None:
                line = self.pages.find(code)
                if line is None:
                    line = self.pages.find(key)
                count = 2
            evidence = self._ev(line, count)
            if not evidence:
                self._missing(f"the alarm code {code!r}")
            self.node(
                "AlarmCode",
                key,
                item.message.strip() or key,
                {
                    "code": key,
                    "text": item.message.strip(),
                    "severity": item.severity,
                    "aliases": [],
                },
                evidence,
                normalize_alarm_code(code) is not None,
            )
            self.alarm_keys[code] = key
            self.alarm_evidence[key] = evidence
            end = self.pages.next_code_line(line) if line is not None else None
            for cause in item.causes:
                mode = self.mode_keys.get(_norm(cause.phrase))
                if mode is None:
                    self.notes.append(
                        f"alarm {key}: the cause {cause.phrase!r} matches no failure mode"
                    )
                    continue
                cause_line = (
                    self.pages.find(cause.phrase.strip(), start=line, stop=end)
                    if line is not None
                    else None
                )
                self.edge(
                    ("AlarmCode", key),
                    "INDICATES",
                    ("FailureMode", mode),
                    self._ev(cause_line) or evidence,
                    {"likelihood": LIKELIHOOD[cause.rank], "support": 0},
                )

    def parts_and_procedures(self) -> None:
        for item in self.answer.spare_parts:
            key, canonical = self._part_key(item.part_no)
            line = self.pages.find_after(item.part_no, self.parts_list)
            count = self.pages.part_row_length(line) if line is not None else 1
            evidence = self._ev(line, count)
            if not evidence:
                self._missing(f"the part number {item.part_no!r}")
            self.node(
                "Part",
                key,
                item.description.strip() or key,
                {"part_no": key, "description": item.description.strip()},
                evidence,
                canonical,
            )
            for name in item.fits:
                for component in self.component_ids(name, f"spare part {key}"):
                    self.edge(("Part", key), "FITS", ("Component", component), evidence)
        for item in self.answer.procedures:
            self._procedure(item)

    @staticmethod
    def _part_key(part_no: str) -> tuple[str, bool]:
        dashed = dashed_part(part_no)
        return (dashed, True) if dashed else (part_no.strip(), False)

    def _procedure(self, item: ProcedureItem) -> None:
        key = item.plant_ref.strip().upper()
        line = self.pages.find(f"Plant ref: {key}")
        if line is None:
            line = self.pages.find(key)
        evidence = self._ev(line)
        if not evidence:
            self._missing(f"the plant reference {key!r}")
        self.procedure_lines[key] = line
        self.procedure_evidence[key] = evidence
        requires_loto = self._lockout(key, item.requires_loto, line)
        self.node(
            "Procedure",
            key,
            item.title.strip(),
            {
                "id": key,
                "title": item.title.strip(),
                "steps": [step.strip() for step in item.steps],
                "requires_loto": requires_loto,
                "est_minutes": item.minutes,
                "manual_ref": self._section_of(line),
            },
            evidence,
            validate_id("Procedure", key),
        )
        for part in item.parts:
            part_key, canonical = self._part_key(part.part_no)
            row = self.pages.find_after(part.part_no, line)
            count = self.pages.part_row_length(row, 3) if row is not None else 1
            row_evidence = self._ev(row, count)
            if not row_evidence:
                self._missing(f"the part number {part.part_no!r}")
            self.node(
                "Part",
                part_key,
                part.description.strip() or part_key,
                {"part_no": part_key, "description": part.description.strip()},
                row_evidence,
                canonical,
            )
            self.edge(
                ("Procedure", key),
                "REQUIRES",
                ("Part", part_key),
                row_evidence or evidence,
                {"qty": part.qty},
            )

    def _lockout(self, key: str, model_said: bool, line: int | None) -> bool:
        """Whether the procedure needs lockout. A missed lockout is the costly mistake,
        so if the line and the model disagree, the answer is yes and a note says so."""
        stated = lockout_in(self.pages.lines[line].text) if line is not None else None
        if stated is None or stated == model_said:
            return model_said
        words = {True: "yes", False: "no"}
        self.notes.append(
            f"procedure {key}: the text says lockout required: {words[stated]}, the "
            f"model said {words[model_said]}; using lockout required"
        )
        return True

    def _section_of(self, line: int | None) -> str | None:
        """The section number of the heading just above the line."""
        if line is None:
            return None
        for index in range(line - 1, -1, -1):
            text = self.pages.lines[index].text.strip()
            if text:
                match = _SECTION_NUMBER.match(text)
                return match.group(1) if match else None
        return None

    def remedies(self) -> None:
        """Link each failure mode to the procedures that fix it. A mode with no fix
        listed takes the procedure of an alarm whose only cause it is."""
        procedures = set(self.procedure_lines)
        remedies: dict[str, list[str]] = {}
        for item in self.answer.failure_modes:
            key = self.mode_keys.get(_norm(item.phrase))
            if key is None:
                continue
            refs = remedies.setdefault(key, [])
            for ref in item.procedure_refs:
                ref = ref.strip().upper()
                if ref in procedures:
                    refs.append(ref)
                else:
                    self.notes.append(
                        f"failure mode {key}: {ref} is not a procedure of this manual"
                    )
        for key, refs in remedies.items():
            start = self.mode_lines.get(key)
            for ref in refs:
                found = self.pages.find(ref, start=start) if start is not None else None
                near = found is not None and found - start <= 10
                evidence = self._ev(found) if near else self.procedure_evidence[ref]
                self.edge(
                    ("FailureMode", key), "RESOLVED_BY", ("Procedure", ref), evidence
                )
        for alarm in self.answer.alarms:
            ref = (alarm.procedure_ref or "").strip().upper()
            if len(alarm.causes) != 1 or ref not in procedures:
                continue
            key = self.mode_keys.get(_norm(alarm.causes[0].phrase))
            if key is not None and not remedies.get(key):
                alarm_key = self.alarm_keys[alarm.code.strip()]
                self.edge(
                    ("FailureMode", key),
                    "RESOLVED_BY",
                    ("Procedure", ref),
                    self.alarm_evidence[alarm_key] or self.procedure_evidence[ref],
                )

    def build(self) -> tuple[list[CandidateNode], list[CandidateEdge], list[str]]:
        self.assets_and_components()
        self.failure_modes()
        self.alarms()
        self.parts_and_procedures()
        self.remedies()
        return list(self.nodes.values()), list(self.edges.values()), self.notes


def extract_manual(
    path: Path, aliases: AliasTable, settings: Settings | None = None
) -> Extraction:
    """Read a vendor manual into text sections and facts.

    The manual prints no component or failure mode ids, so this code makes them from
    the model's short names. They hold steady only while the model answer is cached,
    so later steps match them by name, not by id.
    """
    path = Path(path)
    doc_path = doc_path_for(path)
    pages = read_pages(path)
    extraction = Extraction(
        doc_path=doc_path, doc_type="manual", extractor="sol-manual"
    )

    document = document_id(doc_path)
    for order, (page, text) in enumerate(chunk_text(pages)):
        extraction.chunks.append(
            Chunk(
                id=chunk_id(document, page, order),
                doc_path=doc_path,
                page=page,
                ord=order,
                text=text,
            )
        )

    builder = _Builder(_ask(pages, settings), Pages(pages), aliases, doc_path)
    extraction.nodes, extraction.edges, extraction.notes = builder.build()
    return extraction
