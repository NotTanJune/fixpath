"""Write the vendor manuals as PDF files, one for each machine model.

The manuals are the only source of repair procedures and alarm cause tables. Each
manual has a cover and five chapters: safety, alarms, troubleshooting, procedures and
spare parts. The words come from manual_content.py, and the result is the same on
every run.

Each vendor has its own colour and cover design. The design changes only how the
page looks, not the text a reader or the ingestion pulls from it.

render_manuals() writes all the manuals and returns what each mentions. _build()
makes one PDF, _story() lays out its pages, and look_of() picks a vendor's styles.
"""

import math
import os
import random
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab import rl_config
from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.generator import catalogue as cat
from app.generator.manual_content import (
    AUDIENCE,
    FIGURE_CAPTION,
    LOCKOUT_WARNING,
    MANUALS,
    NO_ISOLATION,
    NO_PARTS,
    REVISION,
    SEAL_ORDER,
    SEAL_ORDER_PROCEDURE,
    Cell,
    ModelContent,
    alarm_row,
    collect_model,
    spare_part_row,
    troubleshooting_row,
    used_part_row,
)
from app.generator.mentions import ManualMentions, Mentions, file_key
from app.generator.world import World

# --- Page and styles ------------------------------------------------------------------

# The alarm and troubleshooting tables sit on wide landscape pages so that cell lines
# do not wrap. Every other page is portrait.
PAGE_SIZE = A4
WIDE_SIZE = landscape(A4)
MARGIN = 18 * mm
BOTTOM_MARGIN = 20 * mm
WIDTH = PAGE_SIZE[0] - 2 * MARGIN
WIDE_WIDTH = WIDE_SIZE[0] - 2 * MARGIN
# The colour of each vendor, dark enough for white text on it. It is used for the cover
# band, headings, table headers and footer rule.
VENDOR_COLOURS = {
    "Kessler Werkzeugmaschinen": "#1b3a6b",
    "Bramwell Hydraulics": "#9a3412",
    "Orbis Robotics": "#0b6b5d",
    "Veltra Test Systems": "#5b2d8f",
}
INK = colors.HexColor("#1f2933")
MUTED = colors.HexColor("#566170")
RULE = colors.HexColor("#c9d0d9")
ALTERNATE = colors.HexColor("#f3f5f8")
HAZARD = colors.HexColor("#ffd21f")
NOTICE_FILL = colors.HexColor("#fff4c2")
COVER_BAND_HEIGHT = 96 * mm
TOP_STRIP_HEIGHT = 4 * mm
NOTICE_ICON = 12 * mm


def tint(colour: colors.Color, share: float) -> colors.Color:
    """The colour mixed with white. A share of 0 gives the colour, 1 gives white."""
    return colors.Color(
        *(value + (1 - value) * share for value in colour.rgb()),
    )


def _style(name: str, size: float, **options: object) -> ParagraphStyle:
    options.setdefault("fontName", "Helvetica")
    options.setdefault("leading", round(size * 1.3, 1))
    options.setdefault("textColor", INK)
    return ParagraphStyle(name, fontSize=size, **options)


@dataclass(frozen=True)
class Look:
    """The look of one manual: the vendor colour and the text styles that use it."""

    colour: colors.Color
    body: ParagraphStyle
    bold: ParagraphStyle
    notice: ParagraphStyle
    chapter: ParagraphStyle
    section: ParagraphStyle
    step: ParagraphStyle
    cell: ParagraphStyle
    cell_head: ParagraphStyle
    caption: ParagraphStyle
    cover_vendor: ParagraphStyle
    cover_model: ParagraphStyle
    cover_type: ParagraphStyle
    cover_title: ParagraphStyle
    cover_revision: ParagraphStyle
    cover_audience: ParagraphStyle


def look_of(vendor: str) -> Look:
    colour = colors.HexColor(VENDOR_COLOURS[vendor])
    bold = "Helvetica-Bold"
    return Look(
        colour=colour,
        body=_style("body", 9.5, leading=13, spaceAfter=6),
        bold=_style("bold", 9.5, leading=13, fontName=bold),
        notice=_style("notice", 9.5, leading=13, fontName=bold),
        chapter=_style("chapter", 16, fontName=bold, spaceAfter=3, textColor=colour),
        section=_style(
            "section",
            11.5,
            fontName=bold,
            spaceBefore=8,
            spaceAfter=1,
            textColor=colour,
        ),
        step=_style(
            "step", 9.5, leading=13, leftIndent=16, firstLineIndent=-16, spaceAfter=3
        ),
        cell=_style("cell", 8.5, leading=10.5),
        cell_head=_style(
            "cell_head", 8.5, leading=10.5, fontName=bold, textColor=colors.white
        ),
        caption=_style(
            "caption",
            9,
            fontName="Helvetica-Oblique",
            alignment=1,
            spaceAfter=6,
            textColor=MUTED,
        ),
        cover_vendor=_style(
            "cover_vendor",
            22,
            fontName=bold,
            leading=28,
            textColor=colors.white,
            rightIndent=52 * mm,
        ),
        cover_model=_style(
            "cover_model", 54, fontName=bold, leading=60, textColor=colour
        ),
        cover_type=_style("cover_type", 14, leading=18, textColor=MUTED),
        cover_title=_style("cover_title", 24, fontName=bold, leading=30),
        cover_revision=_style("cover_revision", 12, leading=16, textColor=MUTED),
        cover_audience=_style(
            "cover_audience", 11, fontName=bold, leading=14, textColor=colour
        ),
    )


# The width of each table column, as a share of the page width.
ALARM_COLUMNS = (0.06, 0.20, 0.08, 0.34, 0.32)
TROUBLESHOOTING_COLUMNS = (0.34, 0.33, 0.33)
USED_PARTS_COLUMNS = (0.24, 0.66, 0.10)
SPARE_PARTS_COLUMNS = (0.22, 0.40, 0.38)

# --- Building blocks ------------------------------------------------------------------


def _paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def _cell(value: Cell, style: ParagraphStyle) -> Paragraph:
    lines = [value] if isinstance(value, str) else value
    return Paragraph("<br/>".join(escape(line) for line in lines), style)


def _table(
    look: Look,
    header: Sequence[str],
    rows: Sequence[Sequence[Cell]],
    fractions: Sequence[float],
    width: float = WIDTH,
) -> Table:
    """A table with a coloured header row that repeats on every page and light
    alternating rows. The fractions set the column widths."""
    data = [[_cell(title, look.cell_head) for title in header]]
    data += [[_cell(value, look.cell) for value in row] for row in rows]
    table = Table(
        data, colWidths=[width * fraction for fraction in fractions], repeatRows=1
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), look.colour),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ALTERNATE]),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
                ("BOX", (0, 0), (-1, -1), 0.8, look.colour),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    table.hAlign = "LEFT"
    return table


def _heading(text: str, look: Look, *, chapter: bool) -> list[Flowable]:
    """A heading in the vendor colour with a line under it, heavier for a chapter."""
    rule = HRFlowable(
        width="100%",
        thickness=1.6 if chapter else 0.5,
        color=look.colour if chapter else tint(look.colour, 0.55),
        spaceBefore=0,
        spaceAfter=6 if chapter else 4,
    )
    return [_paragraph(text, look.chapter if chapter else look.section), rule]


def _warning_triangle(size: float) -> Drawing:
    """The standard warning sign drawn as shapes. It holds no text."""
    drawing = Drawing(size, size)
    pad = size * 0.06
    apex = (size / 2, size - pad)
    left, right = (pad, pad + size * 0.05), (size - pad, pad + size * 0.05)
    drawing.add(
        Polygon(
            [*left, *right, *apex],
            fillColor=HAZARD,
            strokeColor=INK,
            strokeWidth=size * 0.09,
            strokeLineJoin=1,
        )
    )
    centre = size / 2
    drawing.add(
        Polygon(
            [
                centre - size * 0.05,
                size * 0.40,
                centre + size * 0.05,
                size * 0.40,
                centre + size * 0.035,
                size * 0.66,
                centre - size * 0.035,
                size * 0.66,
            ],
            fillColor=INK,
            strokeColor=INK,
            strokeWidth=0.3,
        )
    )
    drawing.add(
        Circle(centre, size * 0.29, size * 0.055, fillColor=INK, strokeColor=INK)
    )
    return drawing


def _notice(text: str, look: Look) -> Table:
    """A safety notice: a pale yellow box with a warning sign and bold text."""
    box = Table(
        [[_warning_triangle(NOTICE_ICON), _paragraph(text, look.notice)]],
        colWidths=[NOTICE_ICON + 16, WIDTH - NOTICE_ICON - 16],
    )
    box.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1.5, HAZARD),
                ("LINEBEFORE", (0, 0), (0, 0), 7, HAZARD),
                ("BACKGROUND", (0, 0), (-1, -1), NOTICE_FILL),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (0, 0), 8),
                ("RIGHTPADDING", (0, 0), (0, 0), 8),
                ("LEFTPADDING", (1, 0), (1, 0), 0),
            ]
        )
    )
    box.hAlign = "LEFT"
    return box


def _audience_box(text: str, look: Look) -> Table:
    """The cover line saying who the manual is for, in a tinted box."""
    box = Table([[_paragraph(text, look.cover_audience)]], colWidths=[WIDTH])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), tint(look.colour, 0.9)),
                ("LINEBEFORE", (0, 0), (0, 0), 5, look.colour),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ]
        )
    )
    box.hAlign = "LEFT"
    return box


def _seal_icon(drawing: Drawing, index: int, x: float, y: float, colour: colors.Color):
    """A small drawing of one seal part, in the order the seals go in."""
    pale = tint(colour, 0.75)
    base = {"fillColor": colour, "strokeColor": colour, "strokeWidth": 0.4}
    if index == 0:
        drawing.add(
            Polygon([x - 7, y - 4, x + 7, y - 4, x + 7, y - 1, x - 7, y - 1], **base)
        )
        drawing.add(
            Polygon([x - 5, y - 1, x + 5, y - 1, x + 1, y + 6, x - 1, y + 6], **base)
        )
    elif index == 1:
        drawing.add(Circle(x, y, 6, **base))
        drawing.add(
            Circle(x, y, 2.6, fillColor=colors.white, strokeColor=pale, strokeWidth=0.4)
        )
    elif index == 2:
        for side in (-1, 1):
            left = x + side * 6 - 1.8
            drawing.add(
                Polygon(
                    [left, y - 6, left + 3.6, y - 6, left + 3.6, y + 6, left, y + 6],
                    **base,
                )
            )
        drawing.add(
            Polygon(
                [
                    x - 4.2,
                    y - 1.2,
                    x + 4.2,
                    y - 1.2,
                    x + 4.2,
                    y + 1.2,
                    x - 4.2,
                    y + 1.2,
                ],
                fillColor=pale,
                strokeColor=colour,
                strokeWidth=0.4,
            )
        )
    else:
        cup = [
            x - 6,
            y + 6,
            x - 3.4,
            y + 6,
            x - 3.4,
            y - 2,
            x + 3.4,
            y - 2,
            x + 3.4,
            y + 6,
            x + 6,
            y + 6,
            x + 6,
            y - 6,
            x - 6,
            y - 6,
        ]
        drawing.add(Polygon(cup, **base))


def seal_order_figure(colour: colors.Color = colors.black) -> Drawing:
    """The figure showing the order the seals go in: four labelled boxes joined by
    arrows, each with a small drawing of its part."""
    box_width, box_height, height = 34 * mm, 19 * mm, 27 * mm
    gap = (WIDTH - len(SEAL_ORDER) * box_width) / (len(SEAL_ORDER) - 1)
    bottom = (height - box_height) / 2
    middle = bottom + box_height / 2
    ink = colour
    drawing = Drawing(WIDTH, height)
    drawing.add(
        Polygon(
            [0, 0, WIDTH, 0, WIDTH, height, 0, height],
            fillColor=ALTERNATE,
            strokeColor=RULE,
            strokeWidth=0.4,
        )
    )
    for index, label in enumerate(SEAL_ORDER):
        left = index * (box_width + gap)
        drawing.add(
            Rect(
                left,
                bottom,
                box_width,
                box_height,
                rx=2.5,
                ry=2.5,
                strokeColor=ink,
                strokeWidth=1,
                fillColor=colors.white,
            )
        )
        _seal_icon(
            drawing, index, left + box_width / 2, bottom + box_height * 0.64, ink
        )
        drawing.add(
            String(
                left + box_width / 2,
                bottom + 3.2 * mm,
                label,
                textAnchor="middle",
                fontName="Helvetica-Bold",
                fontSize=9,
                fillColor=INK,
            )
        )
        if index < len(SEAL_ORDER) - 1:
            start, end = left + box_width + 1.5 * mm, left + box_width + gap - 1.5 * mm
            drawing.add(
                Line(
                    start, middle, end - 2 * mm, middle, strokeWidth=1, strokeColor=ink
                )
            )
            head = [end, middle, end - 2.5 * mm, middle + 1.3 * mm]
            head += [end - 2.5 * mm, middle - 1.3 * mm]
            drawing.add(Polygon(head, fillColor=ink, strokeColor=ink))
    return drawing


# --- Cover art ------------------------------------------------------------------------
# Drawn with simple shapes when the cover page starts, before any text.


def _gear(canvas: Canvas, radius: float, hole: colors.Color) -> None:
    canvas.setFillColor(colors.white)
    for tooth in range(8):
        canvas.saveState()
        canvas.rotate(tooth * 45)
        canvas.rect(-radius * 0.15, radius * 0.7, radius * 0.3, radius * 0.3, 0, 1)
        canvas.restoreState()
    canvas.circle(0, 0, radius * 0.84, 0, 1)
    canvas.setFillColor(hole)
    canvas.circle(0, 0, radius * 0.46, 0, 1)
    canvas.setFillColor(colors.white)
    canvas.circle(0, 0, radius * 0.2, 0, 1)


def _droplet(canvas: Canvas, radius: float, hole: colors.Color) -> None:
    canvas.setStrokeColor(colors.white)
    canvas.setLineWidth(radius * 0.09)
    canvas.circle(0, 0, radius * 0.96, 1, 0)
    canvas.setFillColor(colors.white)
    canvas.circle(0, -radius * 0.2, radius * 0.45, 0, 1)
    top = canvas.beginPath()
    top.moveTo(0, radius * 0.7)
    top.lineTo(-radius * 0.41, -radius * 0.05)
    top.lineTo(radius * 0.41, -radius * 0.05)
    top.close()
    canvas.drawPath(top, stroke=0, fill=1)
    canvas.setFillColor(hole)
    canvas.circle(-radius * 0.14, -radius * 0.28, radius * 0.1, 0, 1)


def _orbit(canvas: Canvas, radius: float, hole: colors.Color) -> None:
    canvas.setStrokeColor(colors.white)
    canvas.setFillColor(colors.white)
    canvas.setLineWidth(radius * 0.07)
    for angle in (35, -35, 90):
        canvas.saveState()
        canvas.rotate(angle)
        canvas.ellipse(-radius, -radius * 0.36, radius, radius * 0.36, 1, 0)
        canvas.circle(radius, 0, radius * 0.09, 0, 1)
        canvas.restoreState()
    canvas.circle(0, 0, radius * 0.27, 0, 1)
    canvas.setFillColor(hole)
    canvas.circle(0, 0, radius * 0.1, 0, 1)


def _gauge(canvas: Canvas, radius: float, hole: colors.Color) -> None:
    canvas.setStrokeColor(colors.white)
    canvas.setFillColor(colors.white)
    canvas.translate(0, -radius * 0.3)
    canvas.setLineWidth(radius * 0.1)
    canvas.arc(-radius, -radius, radius, radius, 0, 180)
    canvas.setLineWidth(radius * 0.05)
    for tick in range(7):
        angle = math.radians(tick * 30)
        canvas.line(
            math.cos(angle) * radius * 0.72,
            math.sin(angle) * radius * 0.72,
            math.cos(angle) * radius * 0.88,
            math.sin(angle) * radius * 0.88,
        )
    canvas.setLineWidth(radius * 0.08)
    canvas.line(
        0,
        0,
        math.cos(math.radians(55)) * radius * 0.66,
        math.sin(math.radians(55)) * radius * 0.66,
    )
    canvas.circle(0, 0, radius * 0.15, 0, 1)
    canvas.setFillColor(hole)
    canvas.circle(0, 0, radius * 0.05, 0, 1)
    canvas.setLineWidth(radius * 0.06)
    canvas.line(-radius, -radius * 0.18, radius, -radius * 0.18)


EMBLEMS: dict[str, Callable[[Canvas, float, colors.Color], None]] = {
    "Kessler Werkzeugmaschinen": _gear,
    "Bramwell Hydraulics": _droplet,
    "Orbis Robotics": _orbit,
    "Veltra Test Systems": _gauge,
}


def _cover_art(vendor: str, look: Look) -> Callable[[Canvas, BaseDocTemplate], None]:
    """The cover design: a coloured band with the vendor emblem, and a bar at the foot."""

    def draw(canvas: Canvas, doc: BaseDocTemplate) -> None:
        width, height = PAGE_SIZE
        top = height - COVER_BAND_HEIGHT
        canvas.saveState()
        band = canvas.beginPath()
        band.rect(0, top, width, COVER_BAND_HEIGHT)
        canvas.clipPath(band, stroke=0, fill=0)
        canvas.setFillColor(look.colour)
        canvas.rect(0, top, width, COVER_BAND_HEIGHT, 0, 1)
        canvas.setFillColor(tint(look.colour, 0.12))
        canvas.circle(width - 30 * mm, height - 18 * mm, 58 * mm, 0, 1)
        canvas.setFillColor(tint(look.colour, 0.2))
        canvas.circle(width - 62 * mm, top + 4 * mm, 30 * mm, 0, 1)
        canvas.restoreState()
        canvas.saveState()
        canvas.setFillColor(tint(look.colour, 0.55))
        canvas.rect(0, top - 2.2 * mm, width, 2.2 * mm, 0, 1)
        canvas.restoreState()
        canvas.saveState()
        canvas.translate(
            width - MARGIN - 20 * mm, height - COVER_BAND_HEIGHT / 2 - 1 * mm
        )
        EMBLEMS[vendor](canvas, 20 * mm, look.colour)
        canvas.restoreState()
        canvas.saveState()
        canvas.setFillColor(look.colour)
        canvas.rect(0, 0, width, 8 * mm, 0, 1)
        canvas.restoreState()

    return draw


def _top_strip(
    look: Look, page_width: float
) -> Callable[[Canvas, BaseDocTemplate], None]:
    """A strip of the vendor colour along the top of a chapter page."""

    def draw(canvas: Canvas, doc: BaseDocTemplate) -> None:
        height = PAGE_SIZE[1] if page_width == PAGE_SIZE[0] else WIDE_SIZE[1]
        canvas.saveState()
        canvas.setFillColor(look.colour)
        canvas.rect(0, height - TOP_STRIP_HEIGHT, page_width, TOP_STRIP_HEIGHT, 0, 1)
        canvas.restoreState()

    return draw


# --- Chapters -------------------------------------------------------------------------


def _cover(model: ModelContent, look: Look) -> list[Flowable]:
    return [
        Spacer(1, 38 * mm),
        _paragraph(model.vendor, look.cover_vendor),
        Spacer(1, 38 * mm),
        _paragraph(model.spec.model, look.cover_model),
        _paragraph(model.machine_type, look.cover_type),
        Spacer(1, 3 * mm),
        HRFlowable(
            width=26 * mm,
            thickness=3,
            color=look.colour,
            hAlign="LEFT",
            spaceBefore=0,
            spaceAfter=0,
        ),
        Spacer(1, 12 * mm),
        _paragraph(model.spec.title, look.cover_title),
        _paragraph(REVISION, look.cover_revision),
        Spacer(1, 66 * mm),
        _audience_box(AUDIENCE, look),
    ]


def _safety(model: ModelContent, look: Look) -> list[Flowable]:
    return [
        *_heading("1 Safety", look, chapter=True),
        *_heading("1.1 General safety", look, chapter=False),
        _paragraph(model.spec.safety, look.body),
        *_heading("1.2 Lockout and tagout", look, chapter=False),
        _paragraph(model.spec.lockout, look.body),
    ]


def _alarm_table(model: ModelContent, look: Look) -> list[Flowable]:
    rows = [alarm_row(model, alarm) for alarm in model.alarms]
    return [
        *_heading("2 Alarm table", look, chapter=True),
        _paragraph(
            "The control shows these codes. Likely causes are listed in order of "
            "likelihood.",
            look.body,
        ),
        _table(
            look,
            ("Code", "Message", "Severity", "Likely causes", "Action"),
            rows,
            ALARM_COLUMNS,
            WIDE_WIDTH,
        ),
    ]


def _troubleshooting(model: ModelContent, look: Look) -> list[Flowable]:
    rows = [troubleshooting_row(model, fm) for fm in model.failure_modes]
    return [
        *_heading("3 Troubleshooting table", look, chapter=True),
        _paragraph("Find the symptom, then follow the remedy.", look.body),
        _table(
            look,
            ("Symptom", "Probable cause", "Remedy"),
            rows,
            TROUBLESHOOTING_COLUMNS,
            WIDE_WIDTH,
        ),
    ]


def _procedure(model: ModelContent, proc: cat.ProcedureSpec, look: Look) -> Flowable:
    props = proc.props
    lockout = "yes" if props.requires_loto else "no"
    reference = (
        f"Plant ref: {props.id} | Lockout required: {lockout} | "
        f"Time: {props.est_minutes} min"
    )
    isolation = (
        _notice(LOCKOUT_WARNING, look)
        if props.requires_loto
        else _paragraph(NO_ISOLATION, look.body)
    )
    items: list[Flowable] = [
        *_heading(f"{model.section(props.id)} {props.title}", look, chapter=False),
        _paragraph(reference, look.body),
        isolation,
        Spacer(1, 6),
    ]
    items += [
        Paragraph(escape(f"{number}. {step}"), look.step)
        for number, step in enumerate(props.steps, start=1)
    ]
    if props.id == SEAL_ORDER_PROCEDURE:
        items += [
            Spacer(1, 4),
            seal_order_figure(look.colour),
            _paragraph(FIGURE_CAPTION, look.caption),
        ]
    items.append(Spacer(1, 4))
    if proc.parts:
        used = [used_part_row(part_no, qty) for part_no, qty in proc.parts]
        items.append(
            _table(
                look,
                ("Order no.", "Description", "Qty"),
                used,
                USED_PARTS_COLUMNS,
            )
        )
    else:
        items.append(_paragraph(NO_PARTS, look.body))
    items.append(Spacer(1, 10))
    # Keep the whole procedure on one page. Do not nest another keep-together inside
    # it, because a nested one would push the block to the next page every time.
    return KeepTogether(items)


def _procedures(model: ModelContent, look: Look) -> list[Flowable]:
    return [
        *_heading("4 Procedures", look, chapter=True),
        *(_procedure(model, proc, look) for proc in model.procedures),
    ]


def _spare_parts(model: ModelContent, look: Look) -> list[Flowable]:
    rows = [spare_part_row(model, part) for part in model.parts]
    return [
        *_heading("5 Spare parts list", look, chapter=True),
        _paragraph("Order spare parts by part number.", look.body),
        _table(
            look,
            ("Part number", "Description", "Fits"),
            rows,
            SPARE_PARTS_COLUMNS,
        ),
    ]


# Each chapter with its page type: "chapter" is portrait and "wide" is landscape.
# Both have a footer. The cover has its own page type, without one.
CHAPTERS = (
    (_safety, "chapter"),
    (_alarm_table, "wide"),
    (_troubleshooting, "wide"),
    (_procedures, "chapter"),
    (_spare_parts, "chapter"),
)


def _story(model: ModelContent, look: Look) -> list[Flowable]:
    story: list[Flowable] = _cover(model, look)
    for chapter, template in CHAPTERS:
        story += [NextPageTemplate(template), PageBreak(), *chapter(model, look)]
    return story


# --- The document ---------------------------------------------------------------------


def _footer(
    label: str, page_width: float, look: Look
) -> Callable[[Canvas, BaseDocTemplate], None]:
    """The line at the foot of a chapter page with the manual name and page number."""

    def draw(canvas: Canvas, doc: BaseDocTemplate) -> None:
        canvas.saveState()
        canvas.setStrokeColor(look.colour)
        canvas.setLineWidth(0.8)
        canvas.line(MARGIN, 14 * mm, page_width - MARGIN, 14 * mm)
        canvas.setFillColor(look.colour)
        canvas.rect(MARGIN, 13.6 * mm, 14 * mm, 1.2, 0, 1)
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica", 8)
        canvas.drawString(MARGIN, 9.5 * mm, label)
        canvas.drawRightString(page_width - MARGIN, 9.5 * mm, f"Page {doc.page}")
        canvas.restoreState()

    return draw


def _page_template(
    name: str,
    size: tuple[float, float],
    on_page: Callable[[Canvas, BaseDocTemplate], None],
    footer: Callable[[Canvas, BaseDocTemplate], None] | None = None,
) -> PageTemplate:
    """A page type of this size, with its design and, if given, a footer."""
    frame = Frame(
        MARGIN,
        BOTTOM_MARGIN,
        size[0] - 2 * MARGIN,
        size[1] - MARGIN - BOTTOM_MARGIN,
        id=name,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    options = {"onPageEnd": footer} if footer else {}
    return PageTemplate(
        id=name, frames=[frame], pagesize=size, onPage=on_page, **options
    )


def _build(model: ModelContent, path: Path) -> None:
    name = f"{model.vendor} {model.spec.model} {model.spec.title}"
    look = look_of(model.vendor)
    doc = BaseDocTemplate(
        str(path),
        pagesize=PAGE_SIZE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=BOTTOM_MARGIN,
        title=name,
        author=model.vendor,
        creator=model.vendor,
        subject=f"{model.spec.model}, {REVISION}",
        pageCompression=0,
        invariant=1,
    )
    label = f"{name}, {REVISION}"
    doc.addPageTemplates(
        [
            _page_template("cover", PAGE_SIZE, _cover_art(model.vendor, look)),
            _page_template(
                "chapter",
                PAGE_SIZE,
                _top_strip(look, PAGE_SIZE[0]),
                _footer(label, PAGE_SIZE[0], look),
            ),
            _page_template(
                "wide",
                WIDE_SIZE,
                _top_strip(look, WIDE_SIZE[0]),
                _footer(label, WIDE_SIZE[0], look),
            ),
        ]
    )
    doc.build(_story(model, look))


@contextmanager
def _invariant_output() -> Iterator[None]:
    """Make the PDF library produce identical bytes on every run. It fixes the file
    dates and ID, and sets aside an outside date setting until the work is done."""
    previous = rl_config.invariant
    epoch = os.environ.pop("SOURCE_DATE_EPOCH", None)
    rl_config.invariant = 1
    try:
        yield
    finally:
        rl_config.invariant = previous
        if epoch is not None:
            os.environ["SOURCE_DATE_EPOCH"] = epoch


def _mentions(model: ModelContent) -> ManualMentions:
    return ManualMentions(
        assets=list(model.assets),
        alarm_codes=[alarm.props.code for alarm in model.alarms],
        failure_modes=[fm.props.id for fm in model.failure_modes],
        procedures=[proc.props.id for proc in model.procedures],
        parts=model.part_numbers(),
    )


def render_manuals(world: World, rng: random.Random, out: Path) -> Mentions:
    """Write the four manuals and return what each mentions. The manuals do not
    depend on the seed, so the world and random stream go unused."""
    folder = out / "raw" / "manuals"
    folder.mkdir(parents=True, exist_ok=True)
    mentions: Mentions = {}
    with _invariant_output():
        for spec in MANUALS:
            model = collect_model(spec)
            path = folder / spec.file_name
            _build(model, path)
            mentions[file_key(out, path)] = _mentions(model)
    return mentions
