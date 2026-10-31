import { plainLabel } from "./format";
import type { FactRef } from "./sourceMarks";
import type { Span } from "./types";

/** The letter of a column as a spreadsheet names it: 0 is A, 25 is Z, 26 is AA. */
export function columnLetter(index: number): string {
  let n = index + 1;
  let out = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    out = String.fromCharCode(65 + r) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

/** A row as the text of the document has it: its filled cells joined by " | ". */
export function rowKey(cells: string[]): string {
  return cells
    .map((c) => c.trim())
    .filter(Boolean)
    .join(" | ");
}

/** The rows that are titles: one filled cell, in the first column, above the first row that has
 * more. A spreadsheet merges such a row across the width of the sheet. */
export function titleRows(rows: string[][]): number[] {
  const out: number[] = [];
  for (let i = 0; i < rows.length; i++) {
    const filled = rows[i]!.filter((c) => c.trim()).length;
    if (filled === 0) continue;
    if (filled === 1 && rows[i]![0]?.trim()) out.push(i);
    else break;
  }
  return out;
}

export interface Sheet {
  name: string;
  rows: string[][];
}

export interface RowMark {
  strong: boolean;
  facts: FactRef[];
}

const MIN_QUOTE = 8;

function keyMatches(key: string, quote: string): boolean {
  if (!key || !quote) return false;
  if (key === quote) return true;
  return quote.length >= MIN_QUOTE && key.length >= MIN_QUOTE && (key.startsWith(quote) || quote.startsWith(key));
}

/** The marks of a workbook's rows, by "sheet:row" (both from 0): the row the cited quote names
 * is strong, as are the rows of the fact that was opened when no quote names a row, and every
 * other row that holds a fact of the file is faint. */
export function workbookMarks(
  sheets: Sheet[],
  spans: Span[],
  focus: { quotes: string[]; fact: string | null },
): Map<string, RowMark> {
  const keys: { at: string; key: string }[] = [];
  sheets.forEach((sheet, s) => sheet.rows.forEach((cells, r) => keys.push({ at: `${s}:${r}`, key: rowKey(cells) })));
  const exact = new Map<string, string>();
  for (const k of keys) if (k.key && !exact.has(k.key)) exact.set(k.key, k.at);
  const find = (quote: string): string | null => {
    const q = quote.replace(/\s+/g, " ").trim();
    if (!q) return null;
    return exact.get(q) ?? keys.find((k) => keyMatches(k.key, q))?.at ?? null;
  };

  const marks = new Map<string, RowMark>();
  const opened = new Set<string>();
  spans.forEach((sp, index) => {
    const at = find(sp.quote);
    if (!at) return;
    let m = marks.get(at);
    if (!m) marks.set(at, (m = { strong: false, facts: [] }));
    if (!m.facts.some((f) => f.id === sp.fact_id)) m.facts.push({ id: sp.fact_id, label: plainLabel(sp.label), at: index });
    if (focus.fact && (sp.fact_id === focus.fact || sp.fact_id.split("|").includes(focus.fact))) opened.add(at);
  });

  const cited: string[] = [];
  for (const quote of focus.quotes) {
    const at = find(quote);
    if (at && !cited.includes(at)) cited.push(at);
  }
  for (const at of cited.length ? cited : [...opened]) {
    const m = marks.get(at);
    if (m) m.strong = true;
    else marks.set(at, { strong: true, facts: [] });
  }
  return marks;
}
