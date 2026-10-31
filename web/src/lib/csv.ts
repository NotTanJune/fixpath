export interface CsvRow {
  /** 1 for the first row under the header. */
  no: number;
  cells: string[];
  /** Where the row sits in the text, line breaks inside quotes included, its own terminator not. */
  start: number;
  end: number;
}

export interface CsvTable {
  delimiter: string;
  /** Title and comment lines above the header, without their comment marker. */
  caption: string[];
  header: string[];
  rows: CsvRow[];
}

const DELIMITERS = [",", ";", "\t", "|"];

/** The delimiter of a file: the one whose per-line count, outside quotes, is the steadiest and
 * above zero. A title line above the header has no delimiter and does not spoil it. */
function sniff(text: string): string {
  const counts = new Map<string, number[]>(DELIMITERS.map((d) => [d, [0]]));
  let quoted = false;
  const limit = Math.min(text.length, 6000);
  for (let i = 0; i < limit; i++) {
    const ch = text[i]!;
    if (ch === '"') quoted = !quoted;
    else if (!quoted && ch === "\n") for (const c of counts.values()) c.push(0);
    else if (!quoted && counts.has(ch)) counts.get(ch)![counts.get(ch)!.length - 1]! += 1;
  }
  let best = ",";
  let bestScore = 0;
  for (const d of DELIMITERS) {
    const line = counts.get(d)!.filter((n) => n > 0);
    if (!line.length) continue;
    const tally = new Map<number, number>();
    for (const n of line) tally.set(n, (tally.get(n) ?? 0) + 1);
    const steady = Math.max(...tally.values());
    if (steady > bestScore) {
      bestScore = steady;
      best = d;
    }
  }
  return best;
}

interface Raw {
  cells: string[];
  start: number;
  end: number;
}

function records(text: string, delimiter: string): Raw[] {
  const out: Raw[] = [];
  let cells: string[] = [];
  let field = "";
  let quoted = false;
  let wasQuoted = false;
  let start = text.charCodeAt(0) === 0xfeff ? 1 : 0;
  let i = start;

  const finish = (end: number) => {
    cells.push(field);
    const blank = cells.length === 1 && cells[0] === "" && !wasQuoted;
    if (!blank) out.push({ cells, start, end });
    cells = [];
    field = "";
    wasQuoted = false;
  };

  for (; i < text.length; i++) {
    const ch = text[i]!;
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else quoted = false;
      } else field += ch;
    } else if (ch === '"' && field === "" && !wasQuoted) {
      quoted = true;
      wasQuoted = true;
    } else if (ch === delimiter) {
      cells.push(field);
      field = "";
      wasQuoted = false;
    } else if (ch === "\n" || ch === "\r") {
      finish(i);
      if (ch === "\r" && text[i + 1] === "\n") i++;
      start = i + 1;
    } else field += ch;
  }
  if (start < text.length || field !== "" || cells.length) finish(text.length);
  return out;
}

/** A csv file as a table: the delimiter sniffed, title and comment lines taken out as a caption,
 * the first full-width row as the header, and each row with its place in the text. */
export function parseCsv(text: string): CsvTable {
  const delimiter = sniff(text);
  const all = records(text, delimiter);
  const counts = new Map<number, number>();
  for (const r of all) counts.set(r.cells.length, (counts.get(r.cells.length) ?? 0) + 1);
  let width = 1;
  let most = 0;
  for (const [n, k] of counts) {
    if (k > most || (k === most && n > width)) {
      width = n;
      most = k;
    }
  }
  const isComment = (r: Raw) => r.cells[0]?.trimStart().startsWith("#") ?? false;
  const headAt = all.findIndex((r) => (width < 2 ? !isComment(r) : r.cells.length === width && !isComment(r)));
  if (headAt === -1) {
    const caption = all.map((r) => text.slice(r.start, r.end).replace(/^\s*#+\s*/, "").trim());
    return { delimiter, caption, header: [], rows: [] };
  }
  const caption = all
    .slice(0, headAt)
    .map((r) => text.slice(r.start, r.end).replace(/^\s*#+\s*/, "").trim())
    .filter(Boolean);
  const header = all[headAt]!.cells;
  const rows = all.slice(headAt + 1).map((r, i) => ({ no: i + 1, cells: r.cells, start: r.start, end: r.end }));
  return { delimiter, caption, header, rows };
}

const norm = (s: string) => s.replace(/\s+/g, " ").trim().toLowerCase();

/** The row a quote names by its cells, for a quote that is not the raw line (spacing or quoting
 * differs). It needs at least two cells in common and most of the quote's cells, and it names no
 * row when two rows fit equally well. */
export function rowByCells(table: CsvTable, quote: string): CsvRow | null {
  const wanted = quote.split(table.delimiter).map(norm).filter(Boolean);
  if (!wanted.length) return null;
  let best: CsvRow | null = null;
  let bestHits = 0;
  let tie = false;
  for (const row of table.rows) {
    const have = new Set(row.cells.map(norm));
    const hits = wanted.filter((w) => have.has(w)).length;
    if (hits > bestHits) {
      best = row;
      bestHits = hits;
      tie = false;
    } else if (hits === bestHits && hits > 0) tie = true;
  }
  const enough = bestHits >= Math.min(2, wanted.length) && bestHits / wanted.length >= 0.6;
  return best && enough && !tie ? best : null;
}

const NUMBER = /^[-+]?\d[\d,]*(\.\d+)?$/;

/** Which columns hold numbers, to be right-aligned: nine in ten of the filled cells are numbers. */
export function numericColumns(table: CsvTable): boolean[] {
  const width = Math.max(table.header.length, ...table.rows.map((r) => r.cells.length), 0);
  return Array.from({ length: width }, (_, c) => {
    const filled = table.rows.map((r) => (r.cells[c] ?? "").trim()).filter(Boolean);
    return filled.length > 0 && filled.filter((v) => NUMBER.test(v)).length / filled.length >= 0.9;
  });
}
