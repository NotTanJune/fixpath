"""Gathers the readers that need no model: spares inventory, machine logs and the
maintenance export.

Each reader lives in its own module. This file lists them in one place for other
code to import.
"""

from app.ingest.inventory import build_alias_table, parse_inventory
from app.ingest.logs import is_log_header, parse_log
from app.ingest.maintenance import parse_maintenance_csv

__all__ = [
    "build_alias_table",
    "is_log_header",
    "parse_inventory",
    "parse_log",
    "parse_maintenance_csv",
]
