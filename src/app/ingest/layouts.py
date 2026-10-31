"""Describes the tables the extractors read: spares, machines and maintenance records.

Each layout lists the columns of a table and the names people give each column.
headers.py matches a file's column names against these lists.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    """One column of a table, with the names people use for it."""

    name: str
    meaning: str
    synonyms: tuple[str, ...]
    required: bool = False


@dataclass(frozen=True)
class Layout:
    """A table the extractors know how to read. The anchor column marks the header
    row."""

    name: str
    meaning: str
    columns: tuple[Column, ...]
    anchor: str

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


SPARES = Layout(
    name="spares",
    meaning="spare parts in stock, one row per part",
    anchor="part_no",
    columns=(
        Column(
            "part_no",
            "the part number or code",
            (
                "part no",
                "part number",
                "part num",
                "part",
                "part code",
                "partno",
                "pn",
                "p n",
                "item no",
                "item number",
                "item code",
                "item",
                "sku",
                "stock code",
                "stock no",
                "material",
                "material no",
                "article",
                "article no",
            ),
            required=True,
        ),
        Column(
            "description",
            "what the part is",
            (
                "description",
                "desc",
                "item description",
                "part description",
                "part name",
                "name",
                "details",
            ),
            required=True,
        ),
        Column(
            "qty",
            "how many are in stock",
            (
                "qty",
                "quantity",
                "on hand",
                "onhand",
                "qty on hand",
                "stock",
                "in stock",
                "stock qty",
                "count",
                "balance",
                "available",
            ),
            required=True,
        ),
        Column(
            "uom",
            "the unit of measure",
            ("uom", "unit", "units", "unit of measure"),
        ),
        Column(
            "bin",
            "the storage bin or shelf",
            (
                "bin",
                "loc",
                "location",
                "bin loc",
                "bin location",
                "shelf",
                "rack",
                "store location",
            ),
            required=True,
        ),
        Column(
            "lead_time",
            "how long a reorder takes",
            ("lead time", "leadtime", "lead time days", "lead time wks", "lt"),
        ),
        Column(
            "cost",
            "the unit cost",
            (
                "cost",
                "cost sgd",
                "unit cost",
                "unit cost sgd",
                "price",
                "unit price",
                "price sgd",
            ),
        ),
        Column(
            "remarks",
            "free text remarks",
            ("remarks", "remark", "notes", "note", "comments", "comment"),
        ),
    ),
)

MACHINES = Layout(
    name="machines",
    meaning="the machines on the line, one row per machine",
    anchor="tag",
    columns=(
        Column(
            "tag",
            "the asset tag",
            (
                "asset tag",
                "tag",
                "asset",
                "asset id",
                "asset no",
                "asset number",
                "equipment id",
                "equipment no",
                "machine id",
            ),
            required=True,
        ),
        Column(
            "description",
            "the machine name and kind",
            ("description", "desc", "name", "machine name", "machine"),
            required=True,
        ),
        Column(
            "make",
            "the manufacturer",
            ("make", "manufacturer", "vendor", "brand", "maker"),
            required=True,
        ),
        Column(
            "model",
            "the model",
            ("model", "model no", "model number", "type"),
            required=True,
        ),
        Column(
            "serial",
            "the serial number",
            ("serial no", "serial number", "serial", "sn", "s n"),
            required=True,
        ),
        Column(
            "location",
            "where the machine stands",
            ("location", "loc", "area", "bay", "site"),
            required=True,
        ),
        Column(
            "aliases",
            "the other names people use for the machine",
            (
                "also called",
                "aliases",
                "alias",
                "also known as",
                "aka",
                "other names",
                "known as",
                "nicknames",
            ),
            required=True,
        ),
    ),
)

MAINTENANCE = Layout(
    name="maintenance",
    meaning="breakdown records from the maintenance log, one row per work order",
    anchor="date",
    columns=(
        Column(
            "date",
            "the date of the breakdown",
            ("date", "date reported", "reported on", "reported", "when", "day"),
            required=True,
        ),
        Column(
            "machine",
            "the machine that broke down",
            (
                "mach",
                "machine",
                "machine id",
                "asset",
                "asset id",
                "equipment",
                "equip",
                "equipment id",
            ),
            required=True,
        ),
        Column(
            "problem",
            "the symptom or fault, sometimes starting with an alarm code",
            (
                "problem desc",
                "problem description",
                "problem",
                "description",
                "desc",
                "issue",
                "fault",
                "symptom",
                "complaint",
            ),
            required=True,
        ),
        Column(
            "action",
            "what was done to fix it",
            (
                "action taken",
                "action",
                "actions",
                "fix",
                "repair",
                "work done",
                "resolution",
                "remedy",
            ),
        ),
        Column(
            "parts",
            "the spare parts used, with quantities",
            ("parts", "parts used", "spares", "spares used", "materials"),
        ),
        Column(
            "tech",
            "the technician",
            (
                "tech",
                "technician",
                "tech name",
                "engineer",
                "attended by",
                "done by",
                "by",
                "fitter",
            ),
        ),
        Column(
            "downtime",
            "the downtime in minutes",
            (
                "downtime min",
                "downtime",
                "downtime minutes",
                "down time",
                "down time min",
                "dt min",
                "minutes down",
            ),
        ),
        Column(
            "ref",
            "the work order reference",
            (
                "ref",
                "reference",
                "wo",
                "wo no",
                "wo number",
                "work order",
                "work order no",
                "job no",
                "job number",
                "ticket",
                "ticket no",
            ),
            required=True,
        ),
    ),
)

LAYOUTS = (SPARES, MAINTENANCE, MACHINES)
