"""Turns an uploaded file into a form the extractors can read.

PDF, workbook, CSV, log and text files stay as they are. Markdown, web pages, JSON,
email and Word files become plain text, saved under the original name plus .txt. Any
other file that is text is treated the same way. Anything else is refused.

readable decides what to save for an upload. looks_like_text checks whether bytes are
text. html_text pulls the visible text out of a web page.
"""

import email
import email.policy
import io
import json
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

REFUSAL = "This file is not text, a PDF or a workbook, so it cannot be read."
NATIVE = (".pdf", ".xlsx", ".csv", ".log", ".txt")
CONVERTED = (".md", ".html", ".htm", ".json", ".eml", ".docx")
MAX_CONTROL_SHARE = 0.05


class Refused(Exception):
    """An upload that is not accepted, with the HTTP status and a message to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Readable:
    """What to save for an upload. converted_from is empty for a file kept as it was,
    else the original file ending in lower case."""

    name: str
    data: bytes
    converted_from: str | None


def looks_like_text(data: bytes) -> str | None:
    """The text of the bytes if they look like text, else None. Binary files fail this."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    else:
        if b"\x00" in data:
            return None
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
    if not text:
        return text
    control = sum(1 for c in text if ord(c) < 32 and c not in "\t\n\r")
    return text if control / len(text) <= MAX_CONTROL_SHARE else None


# --- HTML ---------------------------------------------------------------------------

_BLOCKS = frozenset(
    "address article aside blockquote body br dd details div dl dt fieldset figcaption"
    " figure footer form h1 h2 h3 h4 h5 h6 header hr html li main nav ol p pre section"
    " summary table tbody thead tfoot tr ul title".split()
)
_SKIPPED = frozenset({"script", "style", "template", "noscript"})
_CELLS = frozenset({"td", "th"})


class _VisibleText(HTMLParser):
    """Collects the visible text of a page. Table cells on one row share a line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = [""]
        self._skip = 0

    def _break(self) -> None:
        if self.lines[-1].strip():
            self.lines.append("")

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED:
            self._skip += 1
        elif tag in _BLOCKS:
            self._break()
        elif tag in _CELLS and self.lines[-1].strip():
            self.lines[-1] = self.lines[-1].rstrip() + " | "

    def handle_endtag(self, tag):
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self._break()

    def handle_data(self, data):
        if not self._skip:
            self.lines[-1] += data

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in self.lines)
        return "\n".join(line for line in lines if line)


def html_text(html: str) -> str:
    """The visible text of an HTML page, in reading order."""
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    return parser.text()


# --- the other formats --------------------------------------------------------------


def _json_text(text: str) -> str:
    """The JSON laid out for reading. Text that is not valid JSON stays as it is."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


def _eml_text(data: bytes) -> str:
    """The subject, sender, date and body of an email."""
    message = email.message_from_bytes(data, policy=email.policy.default)
    head = [
        f"{name}: {message[name]}"
        for name in ("Subject", "From", "Date")
        if message[name]
    ]
    part = message.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None:
        body = part.get_content()
        if part.get_content_type() == "text/html":
            body = html_text(body)
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    return "\n".join([*head, "", body]) if head else body


def _docx_text(data: bytes) -> str:
    """The paragraphs and tables of a Word file in order, one table row per line."""
    try:
        document = Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise Refused(415, REFUSAL) from None
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            lines.append(Paragraph(child, document).text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, document).rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(line for line in lines if line.strip())


def _text_of(data: bytes) -> str:
    text = looks_like_text(data)
    if text is None:
        raise Refused(415, REFUSAL)
    return text


def readable(name: str, data: bytes) -> Readable:
    """What to save for an upload. Refuses a file that is neither a known format nor text."""
    suffix = Path(name).suffix.lower()
    if suffix in NATIVE:
        return Readable(name, data, None)
    if suffix == ".docx":
        text = _docx_text(data)
    elif suffix == ".eml":
        text = _eml_text(data) if looks_like_text(data) is not None else None
        if text is None:
            raise Refused(415, REFUSAL)
    elif suffix in (".html", ".htm"):
        text = html_text(_text_of(data))
    elif suffix == ".json":
        text = _json_text(_text_of(data))
    else:  # Markdown or any other ending: keep it if it is text
        text = _text_of(data)
    return Readable(f"{name}.txt", text.encode("utf-8"), suffix)
