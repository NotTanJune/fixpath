import { plainLabel } from "./format";
import { findQuote } from "./quote";
import type { Span } from "./types";

export interface FactRef {
  id: string;
  label: string;
  /** The index of the span of the file that this fact is: where a click on the mark finds its row. */
  at?: number;
}

/** A stretch of a source's text that is drawn: strong for the cited quote (or, with no quote
 * to find, the fact that was opened), faint for every other fact the file holds. The ranges
 * of a source never overlap. */
export interface MarkRange {
  start: number;
  end: number;
  strong: boolean;
  facts: FactRef[];
}

const overlaps = (a: { start: number; end: number }, b: { start: number; end: number }) =>
  a.start < b.end && b.start < a.end;

function join(into: FactRef[], more: FactRef[]) {
  for (const f of more) if (!into.some((x) => x.id === f.id)) into.push(f);
}

const isFact = (factId: string, fact: string | null) =>
  fact !== null && fact !== "" && (factId === fact || factId.split("|").includes(fact));

/** Every mark to draw in a source's text. */
export function markRanges(
  doc: { text: string; spans: Span[] },
  focus: { quotes: string[]; fact: string | null; range?: { start: number; end: number } | null },
): MarkRange[] {
  // spans on one range share one mark
  const groups = new Map<string, MarkRange & { opened: boolean }>();
  doc.spans.forEach((s, at) => {
    const key = `${s.start}:${s.end}`;
    let g = groups.get(key);
    if (!g) {
      g = { start: s.start, end: s.end, strong: false, facts: [], opened: false };
      groups.set(key, g);
    }
    join(g.facts, [{ id: s.fact_id, label: plainLabel(s.label), at }]);
    if (isFact(s.fact_id, focus.fact)) g.opened = true;
  });

  // the cited quotes found in the text; with none found the opened fact's spans stand in
  let strong: MarkRange[] = [];
  if (focus.range) {
    // the reader picked this very span: it is the strong mark, wherever else its quote is found
    strong.push({ start: focus.range.start, end: focus.range.end, strong: true, facts: [] });
  } else {
    for (const quote of focus.quotes) {
      if (!quote.trim()) continue;
      const found = findQuote(doc.text, quote);
      if (found) strong.push({ start: found[0], end: found[1], strong: true, facts: [] });
    }
  }
  const cited = strong.length > 0;
  if (!cited) {
    for (const g of groups.values()) {
      if (g.opened) strong.push({ start: g.start, end: g.end, strong: true, facts: [] });
    }
  }
  strong.sort((a, b) => a.start - b.start || b.end - a.end);
  const union: MarkRange[] = [];
  for (const m of strong) {
    const last = union[union.length - 1];
    if (last && m.start < last.end) last.end = Math.max(last.end, m.end);
    else union.push({ ...m });
  }
  strong = union;

  const faint: MarkRange[] = [];
  const rest = [...groups.values()].sort((a, b) => a.start - b.start || b.end - a.end);
  for (const g of rest) {
    const under = strong.filter((m) => overlaps(m, g));
    if (under.length) {
      for (const m of under) join(m.facts, g.facts);
      continue;
    }
    const last = faint[faint.length - 1];
    if (last && g.start < last.end) join(last.facts, g.facts);
    else faint.push({ start: g.start, end: g.end, strong: false, facts: [...g.facts] });
  }
  return [...strong, ...faint].sort((a, b) => a.start - b.start);
}

export interface Line {
  no: number;
  start: number;
  end: number;
  text: string;
}

/** A text as numbered lines with the offsets of each, as the file has them. */
export function splitLines(text: string): Line[] {
  const lines: Line[] = [];
  let start = 0;
  while (start < text.length) {
    let nl = text.indexOf("\n", start);
    if (nl === -1) nl = text.length;
    let end = nl;
    if (end > start && text[end - 1] === "\r") end -= 1;
    lines.push({ no: lines.length + 1, start, end, text: text.slice(start, end) });
    start = nl + 1;
  }
  return lines;
}

/** The mark of each line: the strongest of the ranges that touch it, with their facts. */
export function lineMarks(
  lines: { start: number; end: number }[],
  ranges: MarkRange[],
): ({ strong: boolean; facts: FactRef[] } | null)[] {
  return lines.map((line) => {
    const hit = ranges.filter((r) => overlaps(r, line));
    if (!hit.length) return null;
    const facts: FactRef[] = [];
    for (const r of hit) join(facts, r.facts);
    return { strong: hit.some((r) => r.strong), facts };
  });
}

/** The labels a mark shows when it is hovered or focused: four at most, then a count. */
export function factNames(facts: FactRef[]): string[] {
  const names = facts.slice(0, 4).map((f) => f.label);
  if (facts.length > 4) names.push(`and ${facts.length - 4} more`);
  return names;
}
