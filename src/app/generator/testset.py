"""The test set and the validation set for the live ingestion.

It writes ten test files, five validation files and an expected.json answer key.
The files cover plant documents in many formats, such as handovers, an email, a log,
a PDF bulletin, a Word procedure and one binary file that must be refused. The
validation files are for a person to try by hand.

Each file names machines, alarms and parts by the nicknames a technician would use,
so finding them is the ingestion's job. expected.json records the document type and
the items each ingest file should yield. The files are the same on every run.

build_samples() makes every file, write_testset() saves them, and each other
function here builds one file.
"""

import argparse
import io
import json
import random
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from app.config import ROOT
from app.generator import DEFAULT_SEED
from app.generator.render_manuals import _invariant_output
from app.generator.world import World, build_world

TESTSET_DIR = ROOT / "data" / "testset"
INGEST_FOLDER, VALIDATION_FOLDER, EXPECTED_FILE = (
    "ingest",
    "validation",
    "expected.json",
)
FIXED_TIME = datetime(2026, 10, 1, 9, 0, 0)
ZIP_TIME = (2026, 10, 1, 9, 0, 0)
BINARY_SEED = 29


@dataclass(frozen=True)
class Sample:
    """One file: its name, its content, the document type the ingestion should give it
    (none if it must be refused) and the ids it should find."""

    name: str
    data: bytes
    doc_type: str | None
    expect: tuple[str, ...]


class Plant:
    """The names of the plant, looked up in the world. A file can only name things the
    world has."""

    def __init__(self, world: World):
        self.nodes = {node.id: node for node in world.nodes}
        self.technicians = {t.name: t for t in world.technicians}

    def form(self, node_id: str, index: int = 0) -> str:
        """The id, or one of the nicknames, of a node, as a person might write it."""
        node = self.nodes[node_id]
        if index == 0:
            return node_id
        aliases = node.props["aliases"]
        return aliases[(index - 1) % len(aliases)]

    def text(self, node_id: str) -> str:
        """The plain description of a node, such as an alarm text or a part name."""
        props = self.nodes[node_id].props
        return props.get("text") or props.get("description") or props["name"]

    def person(self, name: str, short: int | None = None) -> str:
        if short is None:
            return self.technicians[name].name
        return self.technicians[name].short_forms[short]


# --- the text files ------------------------------------------------------------------


def handover_txt(p: Plant) -> Sample:
    text = (
        "Shift handover 19 Oct Shift A\n"
        "From: NA   To: RP\n"
        "\n"
        f"- {p.form('HP-01', 2)} stopped twice with {p.form('E-217', 2)}, oil on the floor\n"
        f"- swapped the {p.form('SEAL-KIT-50', 3)} and topped up with {p.form('OIL-HLP46-20L', 3)}\n"
        f"- {p.form('CNC-01', 1)} warm-up normal\n"
        "- pls f/u on stores count for seals\n"
        "\n"
        f"Signed {p.person('Nur Aisyah')}\n"
    )
    return Sample(
        "01_handover_2026-10-19_A.txt",
        text.encode(),
        "handover",
        ("HP-01", "E-217", "SEAL-KIT-50", "OIL-HLP46-20L", "CNC-01"),
    )


def handover_md(p: Plant) -> Sample:
    text = (
        "# Shift handover 20 Oct Shift B\n"
        "\n"
        "**From:** JH   **To:** MH\n"
        "\n"
        "## Press and robot cell\n"
        "\n"
        f"- **{p.form('HP-01', 5)}** pressure hunting again, {p.form('E-221', 3)} on the screen. "
        f"Changed the `{p.form('FLT-HYD-10', 1)}` return filter.\n"
        f"- Robot cell: {p.form('RB-551', 1)} gripper part-present lost twice. "
        f"Swapped the *{p.form('GRP-FNG-12', 3)}* on the {p.form('RW-01', 1)}.\n"
        "\n"
        "| Item | Status |\n"
        "| --- | --- |\n"
        "| 5S | done |\n"
        "| Stores count | pending |\n"
        "\n"
        f"Signed {p.person('Lim Jia Hui')}\n"
    )
    return Sample(
        "02_handover_2026-10-20_B.md",
        text.encode(),
        "handover",
        ("HP-01", "E-221", "FLT-HYD-10", "RW-01", "RB-551", "GRP-FNG-12"),
    )


def note_eml(p: Plant) -> Sample:
    text = (
        "From: Arun Prakash <arun.prakash@plant.example>\r\n"
        "To: shift-leads@plant.example\r\n"
        f"Subject: {p.form('CNC-02', 1)} spindle, {p.form('SP-104', 1)} again\r\n"
        "Date: Thu, 22 Oct 2026 08:15:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"On 22 Oct {p.form('CNC-02', 1)} stopped at about 0215 with {p.form('SP-104', 1)} "
        "on the screen. Grinding noise from the spindle, vibration alarm. "
        f"When I opened it up I found the bearings worn. I replaced {p.form('BRG-7014-ACD', 1)} x2 "
        f"and {p.form('GREASE-KLB-2', 3)} and {p.form('SEAL-SPDL-70', 1)}. "
        "Next time check the spindle first. Please tell the next shift if it comes back.\r\n"
        "\r\n"
        "Arun\r\n"
    )
    return Sample(
        "03_note_from_arun_CNC2_spindle.eml",
        text.encode(),
        "maintenance_note",
        ("CNC-02", "SP-104", "BRG-7014-ACD", "GREASE-KLB-2", "SEAL-SPDL-70"),
    )


def maintenance_csv(p: Plant) -> Sample:
    rows = [
        (
            "2026-10-13",
            p.form("CNC-01", 4),
            f"{p.form('CL-210', 2)}: coolant pressure dropping, parts coming out hot",
            "Clean coolant filter & flush lines",
            f"{p.form('FLT-CL-25', 1)} x1",
            p.person("Chen Jun Hao"),
            "120",
            "WO-5101",
        ),
        (
            "15/10/2026",
            p.form("HP-01", 4),
            f"{p.form('E-252', 3)} relief valve pressure out of range",
            "Reset and test relief valve",
            f"{p.form('SEAL-RV-25', 1)} x1",
            p.person("Arun Prakash"),
            "95",
            "WO-5102",
        ),
        (
            "2026-10-16",
            p.form("LT-01", 1),
            f"{p.form('VT-018', 1)} leak rate above limit on master part",
            "Replace fixture seals",
            f"{p.form('SEAL-FIX-30', 1)} x2",
            "NA",
            "60",
            "WO-5103",
        ),
        (
            "17/10/2026",
            p.form("RW-01", 1),
            f"{p.form('RB-560', 1)}: axis 2 overcurrent, cell stopped",
            "Inspect axis 2 and reset servo drive",
            f"{p.form('FUSE-SRV-10', 2)} x1",
            p.person("Rajesh Pillai"),
            "150",
            "WO-5104",
        ),
    ]
    header = "Date,Mach.,Problem desc,Action taken,Parts,Tech,Downtime (min),Ref"
    body = "\n".join(",".join(_csv_cell(c) for c in row) for row in rows)
    return Sample(
        "04_maintenance_export_oct.csv",
        f"{header}\n{body}\n".encode(),
        "maintenance_note",
        (
            "CNC-01",
            "CL-210",
            "FLT-CL-25",
            "HP-01",
            "E-252",
            "SEAL-RV-25",
            "LT-01",
            "VT-018",
            "SEAL-FIX-30",
            "RW-01",
            "RB-560",
            "FUSE-SRV-10",
        ),
    )


def _csv_cell(value: str) -> str:
    return f'"{value}"' if "," in value else value


def machine_log(p: Plant) -> Sample:
    """The press event log, in its controller's format and in time order."""
    beats = ["12/10/2026 00:00:00", "12/10/2026 04:00:00", "22/10/2026 00:00:00"]
    alarms = [
        ("12/10/2026 06:45:00", "E-200"),
        ("14/10/2026 02:31:00", "E-245"),
        ("14/10/2026 02:48:00", "E-217"),
        ("14/10/2026 02:48:00", "E-217"),
        ("21/10/2026 15:20:00", "E-221"),
        ("21/10/2026 15:20:00", "E-221"),
    ]
    events = [(stamp, "--- | HEARTBEAT") for stamp in beats]
    for stamp, code in alarms:
        number = code.split("-")[1]
        events.append((stamp, f"ERR {number} | {p.text(code).upper()[:24]}"))
    events.sort(key=lambda e: datetime.strptime(e[0], "%d/%m/%Y %H:%M:%S"))
    lines = ["BRAMWELL BH-250 EVENT LOG  S/N BH250-0091"]
    lines += [f"{stamp} | {rest}" for stamp, rest in events]
    return Sample(
        "05_hp01_event_log_oct.log",
        ("\n".join(lines) + "\n").encode(),
        "log",
        ("HP-01", "E-200", "E-245", "E-217", "E-221"),
    )


def pump_html(p: Plant) -> Sample:
    html = (
        '<!doctype html>\n<html><head><meta charset="utf-8">'
        "<title>Work instruction WI-27</title>"
        "<style>body { font-family: sans-serif; }</style>"
        "<script>var tracking = 'not text';</script></head><body>\n"
        f"<h1>Work instruction WI-27: replace the gripper fingers on {p.form('RW-01', 1)}</h1>\n"
        f"<p>Use when {p.form('RB-551', 1)} keeps coming back or a part drops at pick-up.</p>\n"
        "<ol>\n"
        "<li>Stop the cell and open the light curtain muting key switch.</li>\n"
        f"<li>Remove the two worn <b>{p.form('GRP-FNG-12', 3)}</b> with the 4 mm key.</li>\n"
        f"<li>Fit new {p.form('GRP-FNG-12', 1)} and torque to 6 Nm.</li>\n"
        f"<li>Run ten pick cycles and check that {p.form('RB-551', 1)} does not return.</li>\n"
        "</ol>\n"
        "<table><tr><th>Part</th><th>Qty</th></tr>"
        f"<tr><td>{p.form('GRP-FNG-12', 2)}</td><td>2</td></tr></table>\n"
        "</body></html>\n"
    )
    return Sample(
        "08_work_instruction_gripper.html",
        html.encode(),
        "procedure",
        ("RW-01", "RB-551", "GRP-FNG-12"),
    )


def alarm_json(p: Plant) -> Sample:
    alarms = [
        ("2026-10-23T03:12:00+08:00", "CNC-02", 2, "SP-117", "JHC"),
        ("2026-10-23T03:12:40+08:00", "CNC-02", 2, "SP-104", "JHC"),
        ("2026-10-24T10:05:00+08:00", "LT-01", 1, "VT-021", "WM"),
        ("2026-10-25T21:44:00+08:00", "RW-01", 1, "RB-570", "SL"),
    ]
    export = {
        "export": {
            "site": "Line 3",
            "generated": "2026-10-26T07:00:00+08:00",
            "alarms": [
                {
                    "time": time,
                    "machine": p.form(machine, alias),
                    "code": p.form(code, 1 if code != "SP-117" else 0),
                    "text": p.text(code),
                    "acknowledged_by": who,
                }
                for time, machine, alias, code, who in alarms
            ],
        }
    }
    return Sample(
        "09_alarm_export_week43.json",
        json.dumps(export, sort_keys=True).encode(),
        "maintenance_note",
        ("CNC-02", "SP-117", "SP-104", "LT-01", "VT-021", "RW-01", "RB-570"),
    )


def binary_bin() -> Sample:
    rng = random.Random(BINARY_SEED)
    body = bytes(rng.randrange(256) for _ in range(2048))
    return Sample(
        "10_unknown_firmware.bin",
        b"\x7fFWBIN\x00\x01\x00\x00" + body + b"\x00\x00\x00\x00",
        None,
        (),
    )


# --- the PDF and the Word files ------------------------------------------------------

PDF_MARGIN, PDF_TOP, PDF_LEAD = 56, 780, 16


def _pdf(title: str, vendor: str, pages: list[list[str]]) -> bytes:
    """A small PDF with a footer and page numbers. Lines that start with a digit are
    drawn as headings."""
    buffer = io.BytesIO()
    with _invariant_output():
        sheet = canvas.Canvas(buffer, pagesize=A4, pageCompression=0, invariant=1)
        sheet.setTitle(title)
        sheet.setAuthor(vendor)
        sheet.setCreator(vendor)
        for number, lines in enumerate(pages, 1):
            y = PDF_TOP
            for line in lines:
                heading = line[:1].isdigit()
                sheet.setFont("Helvetica-Bold" if heading else "Helvetica", 12)
                for piece in simpleSplit(
                    line, sheet._fontname, 12, A4[0] - 2 * PDF_MARGIN
                ):
                    sheet.drawString(PDF_MARGIN, y, piece)
                    y -= PDF_LEAD
                y -= 4 if not heading else 0
            sheet.setFont("Helvetica", 9)
            sheet.drawString(PDF_MARGIN, 40, f"{title}, rev 1")
            sheet.drawString(PDF_MARGIN, 28, f"Page {number}")
            sheet.showPage()
        sheet.save()
    return buffer.getvalue()


def bulletin_bh250(p: Plant) -> Sample:
    pages = [
        [
            "Service bulletin SB-250-07",
            "Bramwell Hydraulics BH-250 hydraulic press",
            "1 Scope",
            "This bulletin applies to every BH-250 press in service, among them the "
            f"press {p.form('HP-01')} on line 3. It covers pressure loss in the main "
            "cylinder and the seal kit that cures it.",
            f"2 Alarm {p.form('E-217')}",
            f"The controller raises {p.form('E-217')}, {p.text('E-217').lower()}, when the "
            "main cylinder cannot hold pressure during the stroke. The usual cause is "
            "wear of the cylinder rod seals. Check for oil under the press before "
            "anything else.",
        ],
        [
            "3 Replacing the seals",
            "Lock out the main isolator and release the stored pressure first. Drain "
            "the cylinder, withdraw the rod and fit the main cylinder seal kit "
            f"{p.form('SEAL-KIT-50')}. Replace the rod wiper {p.form('WIPER-CYL-50')} at "
            "the same time, since a worn wiper brings dirt back to the new seals.",
            "4 Parts",
            f"{p.form('SEAL-KIT-50')}  {p.text('SEAL-KIT-50')}, one kit",
            f"{p.form('WIPER-CYL-50')}  {p.text('WIPER-CYL-50')}, one",
            f"{p.form('OIL-HLP46-20L')}  {p.text('OIL-HLP46-20L')}, to top up",
        ],
    ]
    return Sample(
        "06_service_bulletin_BH-250.pdf",
        _pdf(
            "Bramwell Hydraulics BH-250 Service bulletin SB-250-07",
            "Bramwell Hydraulics",
            pages,
        ),
        "manual",
        ("HP-01", "E-217", "SEAL-KIT-50", "WIPER-CYL-50", "OIL-HLP46-20L"),
    )


def bulletin_kx500(p: Plant) -> Sample:
    pages = [
        [
            "Service bulletin SB-500-12",
            "Kessler Werkzeugmaschinen KX-500 machining centre",
            "1 Scope",
            "This bulletin applies to KX-500 machining centres, among them "
            f"{p.form('CNC-01')} and {p.form('CNC-02')} on line 3. It covers a rise in "
            "spindle vibration and temperature.",
            f"2 Alarm {p.form('SP-117')}",
            f"The control raises {p.form('SP-117')}, {p.text('SP-117').lower()}, when the "
            f"spindle vibration passes the limit. {p.form('SP-104')}, "
            f"{p.text('SP-104').lower()}, may follow within the hour. Both point to "
            "worn spindle bearings.",
        ],
        [
            "3 Replacing the bearings",
            f"Fit the angular contact pair {p.form('BRG-7014-ACD')} with the spacer, "
            f"a new front seal {p.form('SEAL-SPDL-70')} and fresh grease "
            f"{p.form('GREASE-KLB-2')}. Run the warm-up cycle and check vibration.",
            "4 Parts",
            f"{p.form('BRG-7014-ACD')}  {p.text('BRG-7014-ACD')}, one pair",
            f"{p.form('SEAL-SPDL-70')}  {p.text('SEAL-SPDL-70')}, one",
            f"{p.form('GREASE-KLB-2')}  {p.text('GREASE-KLB-2')}, one tube",
        ],
    ]
    return Sample(
        "V3_service_bulletin_KX-500.pdf",
        _pdf(
            "Kessler KX-500 Service bulletin SB-500-12",
            "Kessler Werkzeugmaschinen",
            pages,
        ),
        "manual",
        (
            "CNC-01",
            "CNC-02",
            "SP-117",
            "SP-104",
            "BRG-7014-ACD",
            "SEAL-SPDL-70",
            "GREASE-KLB-2",
        ),
    )


def _docx(title: str, build) -> bytes:
    """A Word file with fixed properties and times, so it is identical on every run."""
    document = Document()
    properties = document.core_properties
    properties.author = "Plant maintenance"
    properties.last_modified_by = "Plant maintenance"
    properties.title = title
    properties.created = properties.modified = FIXED_TIME
    properties.last_printed = FIXED_TIME
    properties.revision = 1
    build(document)
    raw = io.BytesIO()
    document.save(raw)
    stable = io.BytesIO()
    with (
        zipfile.ZipFile(raw) as source,
        zipfile.ZipFile(stable, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            entry = zipfile.ZipInfo(info.filename, ZIP_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            target.writestr(entry, source.read(info.filename))
    return stable.getvalue()


def pump_docx(p: Plant) -> Sample:
    def build(document) -> None:
        document.add_heading(
            f"Procedure: change the hydraulic oil and filter on {p.form('HP-01', 5)}", 1
        )
        document.add_paragraph(
            f"Do this when {p.form('E-221', 1)} ({p.text('E-221').lower()}) returns "
            f"or when {p.form('E-230', 1)} shows after a start. Allow 90 minutes."
        )
        document.add_paragraph(
            "Lock out the main isolator and release the stored pressure."
        )
        document.add_paragraph(
            "Drain the tank into the oil trolley and clean the strainer."
        )
        table = document.add_table(rows=3, cols=2)
        rows = [
            ("Part", "Qty"),
            (p.form("OIL-HLP46-20L", 3), "3"),
            (p.form("FLT-HYD-10", 1), "1"),
        ]
        for row, (left, right) in zip(table.rows, rows, strict=True):
            row.cells[0].text, row.cells[1].text = left, right
        document.add_paragraph(
            "Fill to the sight glass mark, run the pump for ten minutes and look for leaks."
        )

    return Sample(
        "07_pump_procedure.docx",
        _docx("Hydraulic oil and filter change", build),
        "procedure",
        ("HP-01", "E-221", "E-230", "OIL-HLP46-20L", "FLT-HYD-10"),
    )


# --- the validation set --------------------------------------------------------------


def robot_docx(p: Plant) -> Sample:
    def build(document) -> None:
        document.add_heading(
            f"Checklist: restart {p.form('RW-01', 2)} after {p.form('RB-563', 1)}", 1
        )
        document.add_paragraph(
            f"{p.form('RB-563', 1)} ({p.text('RB-563').lower()}) usually clears after "
            "the drive is reset."
        )
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Check", "Result"
        table.rows[1].cells[0].text = f"Fuse {p.form('FUSE-SRV-10', 1)} intact"
        table.rows[1].cells[1].text = "ok"
        document.add_paragraph("Record the restart time on the shift sheet.")

    return Sample(
        "V1_robot_restart_checklist.docx",
        _docx("Robot cell restart checklist", build),
        "maintenance_note",
        ("RW-01", "RB-563", "FUSE-SRV-10"),
    )


def leak_md(p: Plant) -> Sample:
    text = (
        f"# Leak tester note, {p.form('LT-01', 1)}\n"
        "\n"
        f"The station failed the master part on 27 Oct with **{p.form('VT-018', 1)}**. "
        f"I changed the `{p.form('SEAL-FIX-30', 3)}` and ran the master part three times. "
        "It passed all three.\n"
        "\n"
        f"- {p.form('VT-012', 1)} showed once at the start, then cleared\n"
        "- calibration due next month\n"
    )
    return Sample(
        "V2_leak_tester_note.md",
        text.encode(),
        "maintenance_note",
        ("LT-01", "VT-018", "SEAL-FIX-30", "VT-012"),
    )


def export_csv(p: Plant) -> Sample:
    rows = [
        (
            "2026-10-27",
            p.form("CNC-02", 2),
            f"{p.form('TC-305', 1)} tool clamp not confirmed",
            "Replace tool clamp disc springs",
            f"{p.form('SPR-TC-40', 1)} x1",
            p.person("Goh Siew Ling"),
            "140",
            "WO-6001",
        ),
        (
            "28/10/2026",
            p.form("HP-01", 1),
            f"{p.form('E-230', 2)} pump pressure unstable",
            "Change hydraulic oil & filter",
            f"{p.form('FLT-HYD-10', 2)} x1; {p.form('OIL-HLP46-20L', 2)} x3",
            p.person("Tan Wei Ming"),
            "205",
            "WO-6002",
        ),
    ]
    header = "Date,Mach.,Problem desc,Action taken,Parts,Tech,Downtime (min),Ref"
    body = "\n".join(",".join(_csv_cell(c) for c in row) for row in rows)
    return Sample(
        "V4_maintenance_export_late_oct.csv",
        f"{header}\n{body}\n".encode(),
        "maintenance_note",
        (
            "CNC-02",
            "TC-305",
            "SPR-TC-40",
            "HP-01",
            "E-230",
            "FLT-HYD-10",
            "OIL-HLP46-20L",
        ),
    )


def shift_note(p: Plant) -> Sample:
    text = (
        f"Wei Ming here. {p.form('CNC-01', 3)} threw {p.form('TC-311', 1)} twice on the "
        "night run, tool change timeout. The tool clamp looked fine. "
        f"Will swap the {p.form('SPR-TC-40', 2)} on Thursday if it repeats.\n"
    )
    return Sample(
        "V5_shift_note.note",
        text.encode(),
        "maintenance_note",
        ("CNC-01", "TC-311", "SPR-TC-40"),
    )


# --- the whole set -------------------------------------------------------------------


def build_samples(world: World) -> tuple[list[Sample], list[Sample]]:
    """The ten ingest files and the five validation files."""
    p = Plant(world)
    ingest = [
        handover_txt(p),
        handover_md(p),
        note_eml(p),
        maintenance_csv(p),
        machine_log(p),
        bulletin_bh250(p),
        pump_docx(p),
        pump_html(p),
        alarm_json(p),
        binary_bin(),
    ]
    validation = [
        robot_docx(p),
        leak_md(p),
        bulletin_kx500(p),
        export_csv(p),
        shift_note(p),
    ]
    return ingest, validation


def expected(samples: list[Sample]) -> dict[str, dict]:
    """The answer key: for each file, its document type and the ids it should yield."""
    return {
        s.name: {"doc_type": s.doc_type, "expect": sorted(s.expect)} for s in samples
    }


def write_testset(out: Path = TESTSET_DIR, world: World | None = None) -> list[Path]:
    """Write both sets and the answer key, replacing the old ones. Returns the paths
    written."""
    out = Path(out)
    ingest, validation = build_samples(world or build_world(DEFAULT_SEED))
    written = []
    for folder, samples in ((INGEST_FOLDER, ingest), (VALIDATION_FOLDER, validation)):
        target = out / folder
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for sample in samples:
            path = target / sample.name
            path.write_bytes(sample.data)
            written.append(path)
    path = out / EXPECTED_FILE
    path.write_text(
        json.dumps(expected(ingest), indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    return [*written, path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.generator.testset",
        description="Write the live ingestion test set and validation set.",
    )
    parser.add_argument("--out", type=Path, default=TESTSET_DIR)
    args = parser.parse_args(argv)
    written = write_testset(args.out)
    print(f"wrote {len(written)} files under {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
