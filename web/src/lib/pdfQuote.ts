import { plainLabel } from "./format";
import { findQuote } from "./quote";
import type { FactRef } from "./sourceMarks";
import type { Span } from "./types";

/** A stretch of the text of one item of a page: characters `from` up to `to`. */
export interface Segment {
  item: number;
  from: number;
  to: number;
}

const compactOf = (s: string) => s.replace(/\s+/g, "");

/** Where a quote sits among the text items of a page. PDF text comes in pieces whose line breaks
 * and spaces differ from the quote's, so whitespace is left out of both sides: the match runs
 * across items, and the result says which characters of which items it covers. Case is kept
 * first and ignored only when the exact text is not there. Null when the page does not hold it. */
export function matchQuote(items: { str: string }[], quote: string): Segment[] | null {
  const wanted = compactOf(quote);
  if (!wanted) return null;
  let page = "";
  const itemOf: number[] = [];
  const charOf: number[] = [];
  items.forEach((it, i) => {
    for (let j = 0; j < it.str.length; j++) {
      const ch = it.str[j]!;
      if (/\s/.test(ch)) continue;
      page += ch;
      itemOf.push(i);
      charOf.push(j);
    }
  });
  let at = page.indexOf(wanted);
  if (at === -1) at = page.toLowerCase().indexOf(wanted.toLowerCase());
  if (at === -1) return null;
  const out: Segment[] = [];
  for (let k = at; k < at + wanted.length; k++) {
    const last = out[out.length - 1];
    if (last && last.item === itemOf[k]) last.to = charOf[k]! + 1;
    else out.push({ item: itemOf[k]!, from: charOf[k]!, to: charOf[k]! + 1 });
  }
  return out;
}

export interface ItemBox {
  str: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The rectangle of each segment: its share of the box of its item, by characters. */
export function segmentRects(segments: Segment[], boxes: ItemBox[]): Rect[] {
  const out: Rect[] = [];
  for (const s of segments) {
    const box = boxes[s.item];
    if (!box || !box.str.length || s.to <= s.from) continue;
    const n = box.str.length;
    out.push({ x: box.x + (box.w * s.from) / n, y: box.y, w: (box.w * (s.to - s.from)) / n, h: box.h });
  }
  return out;
}

/** The page that holds an offset in the text of the whole document. */
export function pageOfOffset(pages: { page: number; start: number }[], offset: number): number | null {
  let found: number | null = pages[0]?.page ?? null;
  for (const p of pages) if (p.start <= offset) found = p.page;
  return found;
}

/** A quote to find on a page, and what to draw for it. */
export interface PageMark {
  page: number;
  quote: string;
  strong: boolean;
  facts: FactRef[];
}

/** The quotes to find on each page of a manual: the cited quote is strong, on the page it was
 * cited from (or the page of its text); each other fact with a page is faint on that page. */
export function pdfMarks(
  doc: { text: string; spans: Span[]; pages?: { page: number; text: string; start: number }[] },
  focus: { quotes: string[]; fact: string | null; page: number | null },
): PageMark[] {
  const marks = new Map<string, PageMark>();
  const put = (page: number, quote: string, strong: boolean, facts: FactRef[]) => {
    const key = `${page}:${compactOf(quote)}`;
    let m = marks.get(key);
    if (!m) marks.set(key, (m = { page, quote, strong, facts: [] }));
    m.strong ||= strong;
    for (const f of facts) if (!m.facts.some((x) => x.id === f.id)) m.facts.push(f);
  };

  const pages = doc.pages ?? [];
  const cited = focus.quotes.filter((q) => q.trim());
  cited.forEach((quote, i) => {
    const found = findQuote(doc.text, quote);
    const page = (i === 0 ? focus.page : null) ?? (found ? pageOfOffset(pages, found[0]) : null);
    if (page !== null) put(page, quote, true, []);
  });

  const opened = (s: Span) =>
    focus.fact !== null && (s.fact_id === focus.fact || s.fact_id.split("|").includes(focus.fact));
  doc.spans.forEach((s, at) => {
    if (s.page === null || !s.quote.trim()) return;
    put(s.page, s.quote, cited.length === 0 && opened(s), [{ id: s.fact_id, label: plainLabel(s.label), at }]);
  });
  return [...marks.values()];
}
