"""Check an uploaded file and save it in the right place.

`UploadGuard` refuses an upload early: in replay mode, or when the file is too large.
`save_upload` accepts any readable file up to 1 MB, makes its name safe, finds its
document type and saves it in the matching folder without replacing another file.
`safe_name` cleans the name. A refusal is a `Refused` with an HTTP status and a message.
"""

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.ingest import convert, router
from app.ingest.convert import Refused

MAX_BYTES = 1024 * 1024
BODY_LIMIT = MAX_BYTES + 64 * 1024  # the file plus a little extra for the upload wrapper
TOO_LARGE = "the upload is larger than 1 MB"
STOCK_LISTS = "Stock lists are loaded by a full ingest"
MAX_NAME_CHARS = 150
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]")


__all__ = ["Refused", "SavedUpload", "UploadGuard", "safe_name", "save_upload"]


@dataclass(frozen=True)
class SavedUpload:
    """A saved upload: where it is, and what it was converted from, if anything."""

    path: Path
    converted_from: str | None = None
    chars: int | None = None


def safe_name(raw: str) -> str:
    """Make an uploaded file name safe to save.

    Unusual characters become underscores, and leading dots are removed because hidden
    files are skipped later. Raise Refused if the name is empty or too long."""
    name = _UNSAFE.sub("_", raw.replace("\\", "/").rsplit("/", 1)[-1]).lstrip(" .")
    if not name:
        raise Refused(422, "the file name is empty")
    if len(name) > MAX_NAME_CHARS:
        raise Refused(422, f"the file name is longer than {MAX_NAME_CHARS} characters")
    return name


def save_upload(name: str, data: bytes, raw_dir: Path) -> SavedUpload:
    """Save an upload in the folder for its document type and return where it went.

    Raise Refused for a file that is too large, not readable, not recognized, a stock
    list, or has a name that is already taken. A stock list is refused because a full
    ingest loads those."""
    safe = safe_name(name)
    if len(data) > MAX_BYTES:
        raise Refused(413, "the file is larger than 1 MB")
    found = convert.readable(safe, data)
    with tempfile.TemporaryDirectory() as folder:
        staged = Path(folder) / found.name
        staged.write_bytes(found.data)
        try:
            doc_type = router.classify(staged)
        except router.UnknownFile:
            if found.converted_from is None:
                raise Refused(422, "cannot tell what kind of file this is") from None
            doc_type = "maintenance_note"
        if doc_type == "inventory":
            raise Refused(422, STOCK_LISTS)
        if (doc_type, staged.suffix.lower()) not in router.REGISTRY:
            raise Refused(422, "no extractor reads this kind of file yet")
    kind_folder = next(f for f, t in router.FOLDER_TYPES.items() if t == doc_type)
    target = Path(raw_dir) / kind_folder / found.name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:
            handle.write(found.data)
    except FileExistsError:
        raise Refused(409, f"{kind_folder}/{found.name} already exists") from None
    chars = (
        len(found.data.decode("utf-8")) if found.converted_from is not None else None
    )
    return SavedUpload(target, found.converted_from, chars)


class UploadGuard:
    """Refuse bad uploads before the file is read.

    In replay mode every upload is refused. Otherwise an upload that is too large is
    refused, whether its size is declared up front or only shows as it arrives."""

    def __init__(self, app, path: str, refusal: str | None = None):
        self.app = app
        self.path = path
        self.refusal = refusal

    async def __call__(self, scope, receive, send):
        mine = scope["type"] == "http" and scope["method"] == "POST"
        if not mine or scope["path"] != self.path:
            await self.app(scope, receive, send)
            return
        if self.refusal:
            await self._answer(403, self.refusal, scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > BODY_LIMIT:
            await self._answer(413, TOO_LARGE, scope, receive, send)
            return
        seen, started = 0, False

        async def counted():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > BODY_LIMIT:
                    raise HTTPException(413, TOO_LARGE)
            return message

        async def watched(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except HTTPException as refused:  # raised out of an app that does not handle it
            if started:
                raise
            detail = str(refused.detail)
            await self._answer(refused.status_code, detail, scope, receive, send)

    @staticmethod
    async def _answer(status: int, detail: str, scope, receive, send):
        reply = JSONResponse({"detail": detail}, status)
        await reply(scope, receive, send)
