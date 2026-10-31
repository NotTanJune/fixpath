"""The product name and its text, read from config/brand.json.

Code gets the brand from `load_brand` and never repeats these values.
"""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

BRAND_FILE = Path(__file__).resolve().parents[2] / "config/brand.json"


@dataclass(frozen=True)
class Brand:
    name: str
    slug: str
    tagline: str
    description: str


@cache
def load_brand() -> Brand:
    data = json.loads(BRAND_FILE.read_text(encoding="utf-8"))
    return Brand(**data)
