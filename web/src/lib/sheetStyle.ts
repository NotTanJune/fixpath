// The styles of a workbook, apart from the component so that a test can read them. The API gives
// them per sheet, keyed by cell name ("A1"), column letter and row number; rows and columns here are
// counted from 0.

import { columnLetter } from "./sheetGrid";

export interface BorderSide {
  style: string;
  color: string;
}

export interface CellStyle {
  fill?: string;
  bold?: boolean;
  italic?: boolean;
  color?: string;
  size?: number;
  align?: string;
  borders?: Partial<Record<"left" | "right" | "top" | "bottom", BorderSide>>;
  format?: string;
}

/** The look of one sheet as the API gives it. */
export interface SheetStyle {
  columns: Record<string, number>;
  rows: Record<string, number>;
  merged: string[];
  freeze: string | null;
  cells: Record<string, CellStyle>;
}

/** The column a letter names, from 0 (A is 0, Z is 25, AA is 26); -1 when it is not letters. */
export function columnIndex(letters: string): number {
  if (!/^[A-Za-z]+$/.test(letters)) return -1;
  let n = 0;
  for (const ch of letters.toUpperCase()) n = n * 26 + (ch.charCodeAt(0) - 64);
  return n - 1;
}

/** The name of a cell: row 0 column 0 is "A1". */
export function cellRef(row: number, col: number): string {
  return `${columnLetter(col)}${row + 1}`;
}

const NUMBER = /^[-+]?\d[\d,]*(\.\d+)?$/;

/** A value as its number format shows it. Only a plain number is formatted ("#,##0" groups
 * thousands, a trailing ".00" sets the decimals, "0" and "0.00" do not group); text, a number with
 * a unit ("10 pcs") and a format that is not known are kept as written. */
export function formatValue(raw: string, format?: string): string {
  const text = raw.trim();
  if (!format || !text || !NUMBER.test(text)) return raw;
  const m = /^(#,##)?0(?:\.(0+))?$/.exec(format);
  if (!m) return raw;
  const digits = m[2]?.length ?? 0;
  return Number(text.replace(/,/g, "")).toLocaleString("en-US", {
    useGrouping: Boolean(m[1]),
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export interface CellRange {
  r0: number;
  c0: number;
  r1: number;
  c1: number;
}

function parseCell(ref: string): { r: number; c: number } | null {
  const m = /^([A-Za-z]+)(\d+)$/.exec(ref.trim());
  if (!m) return null;
  const c = columnIndex(m[1]!);
  const r = Number(m[2]) - 1;
  return c < 0 || r < 0 ? null : { r, c };
}

/** A range such as "A1:H1" (or one cell, "C3") as its first and last row and column. */
export function parseRange(ref: string): CellRange | null {
  const [a, b = a] = ref.split(":");
  const from = a ? parseCell(a) : null;
  const to = b ? parseCell(b) : null;
  if (!from || !to) return null;
  return { r0: Math.min(from.r, to.r), c0: Math.min(from.c, to.c), r1: Math.max(from.r, to.r), c1: Math.max(from.c, to.c) };
}

/** What merged ranges do to a table: the first cell of each spans it (keys are "row:col"), and
 * the others are covered and not drawn. */
export function mergeMap(ranges: string[]): { anchors: Map<string, { rows: number; cols: number }>; covered: Set<string> } {
  const anchors = new Map<string, { rows: number; cols: number }>();
  const covered = new Set<string>();
  for (const ref of ranges) {
    const r = parseRange(ref);
    if (!r) continue;
    anchors.set(`${r.r0}:${r.c0}`, { rows: r.r1 - r.r0 + 1, cols: r.c1 - r.c0 + 1 });
    for (let row = r.r0; row <= r.r1; row++)
      for (let col = r.c0; col <= r.c1; col++) if (row !== r.r0 || col !== r.c0) covered.add(`${row}:${col}`);
  }
  return { anchors, covered };
}

/** The rows and columns a frozen pane keeps in place: "A4" keeps the 3 rows above it. */
export function freezeCounts(ref: string | null | undefined): { rows: number; cols: number } {
  const at = ref ? parseCell(ref) : null;
  return at ? { rows: at.r, cols: at.c } : { rows: 0, cols: 0 };
}

const WIDTH: Record<string, number> = { hair: 1, thin: 1, dotted: 1, dashed: 1, medium: 2, mediumDashed: 2, thick: 3, double: 3 };
const LINE: Record<string, string> = { dotted: "dotted", hair: "dotted", dashed: "dashed", mediumDashed: "dashed", double: "double" };

/** A border side as a css border, or undefined when there is none. */
export function borderCss(side: BorderSide | undefined): string | undefined {
  if (!side) return undefined;
  return `${WIDTH[side.style] ?? 1}px ${LINE[side.style] ?? "solid"} ${side.color}`;
}

/** The sides a cell draws in a table whose cells do not overlap their borders: its own right and
 * bottom, or the side its neighbour has at that edge (the left of the cell to the right, the top
 * of the cell below), so that a line between two cells is drawn once; and its top and left only
 * on the first row and column, where no cell draws them. */
export function effectiveBorders(
  cells: Record<string, CellStyle>,
  row: number,
  col: number,
): Partial<Record<"left" | "right" | "top" | "bottom", BorderSide>> {
  const own = cells[cellRef(row, col)]?.borders ?? {};
  return {
    right: own.right ?? cells[cellRef(row, col + 1)]?.borders?.left,
    bottom: own.bottom ?? cells[cellRef(row + 1, col)]?.borders?.top,
    top: row === 0 ? own.top : undefined,
    left: col === 0 ? own.left : undefined,
  };
}

/** A font size in points as px: a sheet's 11 pt default is the 14 px of the app's body text. */
export function sizePx(points: number | undefined): number {
  return Math.round(((points ?? 11) * 14 * 10) / 11) / 10;
}
