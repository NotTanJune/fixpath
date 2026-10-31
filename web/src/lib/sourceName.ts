import { basename, MONTHS, plainLabel } from "./format";

const SHIFTS: Record<string, string> = { a: "A shift", b: "B shift", c: "C shift", night: "night shift" };

function shiftWords(raw: string): string {
  return SHIFTS[raw.toLowerCase()] ?? `${raw} shift`;
}

function dayMonth(day: number, month: number): string {
  return `${day} ${MONTHS[month - 1] ?? ""}`.trim();
}

/** The handover a file name stands for, in the five styles the plant's files use, or null.
 * The year is left out: the shop floor says "22 Aug". */
function handoverName(stem: string): string | null {
  const shift = (day: number, month: number, raw: string) =>
    `Shift handover ${dayMonth(day, month)}, ${shiftWords(raw)}`;
  let m = /^handover_(\d{4})(\d{2})(\d{2})_(\w+)$/i.exec(stem);
  if (m) return shift(Number(m[3]), Number(m[2]), m[4]!);
  m = /^handover_(\d{4})-(\d{2})-(\d{2})_(\w+)$/i.exec(stem);
  if (m) return shift(Number(m[3]), Number(m[2]), m[4]!);
  m = /^HO (\d{2})-(\d{2}) (\w+?)(?: shift)?$/i.exec(stem); // month then day
  if (m) return shift(Number(m[2]), Number(m[1]), m[3]!);
  m = /^shift notes (\d{1,2}) ([a-z]{3}) (\w+)$/i.exec(stem);
  if (m) {
    const month = MONTHS.findIndex((x) => x.toLowerCase() === m![2]!.toLowerCase()) + 1;
    return month ? shift(Number(m[1]), month, m[3]!) : null;
  }
  return null;
}

const MACHINE = /^(CNC|HP|RW|LT)-?(\d{1,2})$/i;
const WORD_MAP: Record<string, string> = {
  maint: "maintenance",
  alarms: "alarm",
  events: "event",
  leaktest: "leak test",
};

/** One word of a file name: machine ids get their hyphen and capitals, a model number keeps its
 * capitals, anything else is lower case. */
function word(raw: string): string {
  const machine = MACHINE.exec(raw);
  if (machine) return `${machine[1]!.toUpperCase()}-${machine[2]!.padStart(2, "0")}`;
  if (/\d/.test(raw) && /[A-Z]/.test(raw)) return raw;
  if (/^[a-z]+-[a-z]+$/i.test(raw)) return raw.toLowerCase().replace("-", " ");
  const lower = raw.toLowerCase();
  return WORD_MAP[lower] ?? lower;
}

const upFirst = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

function stemOf(path: string): { folder: string; stem: string; ext: string } {
  const parts = path.split("/");
  const file = parts.pop() ?? path;
  const dot = file.lastIndexOf(".");
  return {
    folder: parts[parts.length - 1] ?? "",
    stem: dot > 0 ? file.slice(0, dot) : file,
    ext: dot > 0 ? file.slice(dot + 1).toLowerCase() : "",
  };
}

function manualName(stem: string): string {
  const words = stem.split(/[\s_]+/).filter(Boolean);
  let rev = "";
  const kept: string[] = [];
  for (const w of words) {
    const r = /^rev(\d+)$/i.exec(w);
    if (r) rev = `, rev ${r[1]}`;
    else if (w.toUpperCase() !== "EN") kept.push(w);
  }
  const out = kept.map((w, i) => {
    if (i === 0) return w;
    if (/\d/.test(w) || (w.length > 1 && w === w.toUpperCase())) return w;
    return w.toLowerCase();
  });
  if (!out.some((w) => /^(manual|guide)$/i.test(w))) out.push("manual");
  return out.join(" ") + rev;
}

function logName(stem: string): string {
  let range = "";
  const out: string[] = [];
  for (const w of stem.split(/[\s_]+/).filter(Boolean)) {
    const r = /^([a-z]{3})-([a-z]{3})$/i.exec(w);
    if (r) range = `, ${upFirst(r[1]!.toLowerCase())} to ${upFirst(r[2]!.toLowerCase())}`;
    else out.push(word(w));
  }
  if (!/^(log|export)$/.test(out[out.length - 1] ?? "")) out.push("log");
  return upFirst(out.join(" ")) + range;
}

/** A file in plain words: "BH-250 service manual", "Shift handover 22 Aug, B shift". `docType`
 * is the type the API gives the file, used when the folder says nothing. */
export function documentName(path: string, docType?: string): string {
  const { folder, stem, ext } = stemOf(path);
  const kind = ext === "pdf" ? "manual" : folder === "handovers" ? "handover" : (docType ?? folder);
  if (kind === "manual" || kind === "manuals") return manualName(stem);
  const handover = handoverName(stem);
  if (handover) return handover;
  if (folder === "logs" || kind === "log") return logName(stem);
  const spares = /^spares[_-](\d{4})-(\d{2})$/i.exec(stem);
  if (spares) return `Spares workbook, ${MONTHS[Number(spares[2]) - 1]} ${spares[1]}`;
  if (/^spares[_-]old$/i.test(stem)) return "Old spares list";
  const note = /^note_from_([a-z]+)_(.+)$/i.exec(stem);
  if (note) return `Note from ${upFirst(note[1]!.toLowerCase())}, ${note[2]!.split("_").map(word).join(" ")}`;
  return upFirst(stem.split(/[\s_]+/).filter(Boolean).map(word).join(" "));
}

/** What the Pipeline shows for a file: its plain name first, the DOC id as a small second label,
 * and the raw file name for the "Open the original file" link and a title. */
export function sourceLabel(s: { id: string; path: string; doc_type?: string }): {
  name: string;
  id: string;
  raw: string;
} {
  return { name: documentName(s.path, s.doc_type), id: s.id, raw: basename(s.path) };
}

interface FactRef {
  fact_id: string;
  label: string;
}

/** The fact a sheet is about, in one line: the id and what the file says it is; or, when the
 * file holds no fact of that id, the id and the quote with its line breaks flattened. */
export function citedFactLine(label: string, spans: FactRef[], quote: string): string {
  const named = (s: FactRef) => s.fact_id === label;
  const found = spans.find(named) ?? spans.find((s) => s.fact_id.split("|").includes(label));
  if (found) {
    const text = plainLabel(found.label);
    return text.startsWith(label) ? text : `${label} ${text}`;
  }
  const flat = quote.replace(/\s+/g, " ").trim();
  return flat ? `${label}: ${flat}` : label;
}
