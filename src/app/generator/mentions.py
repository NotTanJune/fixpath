"""The record of what each raw file truly mentions.

The evals score ingestion against this record. Each renderer returns the mentions
for the files it wrote, and merge() joins them. FileMentions lists the machines,
alarms, parts and incidents one file mentions, always by their standard ids.
ManualMentions adds the failure modes and procedures a vendor manual describes.
write_mentions() saves the record as JSON.
"""

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import TRUTH_DIR


class Conflict(BaseModel):
    """A handover item that names the wrong machine for an incident."""

    model_config = ConfigDict(frozen=True)

    incident: str
    says_asset: str


class FileMentions(BaseModel):
    """The entities one file mentions. Every list is sorted and has no repeats, so
    the same file always gives the same record."""

    incidents: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    alarm_codes: list[str] = Field(default_factory=list)
    parts: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)

    @field_validator("incidents", "assets", "alarm_codes", "parts")
    @classmethod
    def _sorted_unique(cls, values: Iterable[str]) -> list[str]:
        return sorted(set(values))

    @field_validator("conflicts")
    @classmethod
    def _sorted_unique_conflicts(cls, values: Iterable[Conflict]) -> list[Conflict]:
        return sorted(set(values), key=lambda c: (c.incident, c.says_asset))


class ManualMentions(FileMentions):
    """The entities a vendor manual mentions. A manual adds the failure modes and the
    procedures it describes to the fields every file has. Only manuals carry the two
    extra lists, so the record of every other file keeps its five fields."""

    failure_modes: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)

    @field_validator("failure_modes", "procedures")
    @classmethod
    def _sorted_unique_ids(cls, values: Iterable[str]) -> list[str]:
        return sorted(set(values))


Mentions = dict[str, FileMentions]


def file_key(root: Path, path: Path) -> str:
    """The key for a file: its path from the output root, with forward slashes."""
    return path.relative_to(root).as_posix()


def merge(*parts: Mentions) -> Mentions:
    """Join the records of several renderers. Two renderers may not write one file."""
    merged: Mentions = {}
    for part in parts:
        for key, mention in part.items():
            if key in merged:
                raise ValueError(f"two renderers wrote the same file: {key}")
            merged[key] = mention
    return merged


def mentions_to_json(mentions: Mentions) -> str:
    """The record as stable, readable JSON text."""
    data = {key: mention.model_dump(mode="json") for key, mention in mentions.items()}
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_mentions(
    mentions: Mentions, path: Path = TRUTH_DIR / "mentions.json"
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mentions_to_json(mentions), encoding="utf-8", newline="\n")
    return path
