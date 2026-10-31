"""Keep a list of the files that were uploaded through the API.

`record` adds one entry after a successful upload. `added_times` reads the list back,
so the sources page can show when each file arrived. A missing or unreadable list counts
as empty.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def uploads_path(kg_path: Path) -> Path:
    return Path(kg_path).parent / "pipeline" / "uploads.json"


def _read(kg_path: Path) -> list[dict[str, Any]]:
    try:
        found = json.loads(uploads_path(kg_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def record(kg_path: Path, doc_id: str, path: str, doc_type: str | None) -> dict:
    """Add an entry for a new upload, stamped with the current time, and return it."""
    entry = {
        "doc_id": doc_id,
        "path": path,
        "doc_type": doc_type,
        "added_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    target = uploads_path(kg_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.writing")
    text = json.dumps([*_read(kg_path), entry], indent=2, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    return entry


def added_times(kg_path: Path) -> dict[str, str]:
    """Return when each uploaded document was added. The latest entry wins."""
    return {
        e["doc_id"]: e["added_at"]
        for e in _read(kg_path)
        if isinstance(e.get("doc_id"), str) and isinstance(e.get("added_at"), str)
    }
