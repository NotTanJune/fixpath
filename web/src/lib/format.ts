import type { Claim } from "./types";
/** "FM-MAIN-CYLINDER-SEAL-WEAR" as "Main cylinder seal wear". */
export function failureName(id: string): string {
  const words = id.replace(/^FM-/, "").toLowerCase().replace(/-/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** "FM-MAIN-CYLINDER-SEAL-WEAR" as its name; every other id stays as it is. */
export function nameOfId(id: string): string {
  return id.startsWith("FM-") ? sentenceCase(failureName(id)) : id;
}

/** A label with the failure-mode ids in it replaced by their names. */
export function plainLabel(label: string): string {
  return label.replace(/\bFM-[A-Z0-9]+(?:-[A-Z0-9]+)*/g, (id) => failureName(id).toLowerCase());
}

export const VERBS: Record<string, string> = {
  HAS_MACHINE: "has machine",
  HAS_COMPONENT: "is built from",
  RAISED: "raised",
  INDICATES: "points to",
  AFFECTS: "damages",
  RESOLVED_BY: "is fixed by",
  REQUIRES: "needs",
  FITS: "fits",
  OCCURRED_ON: "happened on",
  TRIGGERED_BY: "started with",
  ROOT_CAUSE: "was caused by",
  USED: "used",
};

/** The ends of an edge fact id "A|REL|B", or null for the id of a node. */
export function edgeParts(factId: string): { from: string; rel: string; to: string } | null {
  const [from, rel, to, ...rest] = factId.split("|");
  return from && rel && to && !rest.length ? { from, rel, to } : null;
}

/** An edge fact as a plain sentence: "E-217 points to Main cylinder seal wear". */
export function factSentence(factId: string): string {
  const e = edgeParts(factId);
  if (!e) return factId;
  const verb = VERBS[e.rel] ?? e.rel.toLowerCase().replace(/_/g, " ");
  return `${nameOfId(e.from)} ${verb} ${nameOfId(e.to)}`;
}

export function sentenceCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function fixed(n: number | null | undefined, digits = 2): string {
  return n === null || n === undefined ? "n/a" : n.toFixed(digits);
}

export function percent(n: number | null | undefined): string {
  return n === null || n === undefined ? "n/a" : `${Math.round(n * 100)}%`;
}

export function money(n: number): string {
  if (n === 0) return "$0.00";
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

const GENERIC = /^(service|maintenance|manual|guide|cell|operator|rev\d*|en|export|notes?)$/i;

/** A short name for a cited page: "BH-250 manual p.3", "spares 2026-10" for a sheet. */
export function sourceLabel(doc_path: string, page: number | null): string {
  const raw = basename(doc_path);
  const pdf = /\.pdf$/i.test(raw);
  const words = raw.replace(/\.[a-z0-9]+$/i, "").split(/[\s_]+/);
  const kept = words.filter((w) => !GENERIC.test(w));
  const name = (kept.length ? kept : words).join(" ");
  return `${name}${pdf ? " manual" : ""}${page ? ` p.${page}` : ""}`;
}

export function basename(path: string): string {
  return path.split("/").pop() ?? path;
}

export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-09-28" as "28 Sep". The year is added when it is not the one given. */
export function shortDate(iso: string, year?: number): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return iso;
  const base = `${d} ${MONTHS[m - 1]}`;
  return year !== undefined && y !== year ? `${base} ${y}` : base;
}

/** The id of a document from the chunk id of a citation: "DOC-5926a330-3-2" is "DOC-5926a330". */
export function docIdOfChunk(chunkId: string | null): string | null {
  const match = chunkId?.match(/^(DOC-[0-9a-f]{8})/);
  return match?.[1] ?? null;
}

export const MAX_QUESTION = 500;

/** The question that asks again with a pick added: Line 4 robot is down. Robot workcell (RW-01). */
export function composeAsk(question: string, pick: string): string {
  const tail = ` ${sentenceCase(pick.trim())}.`;
  const base = question.trim().replace(/[.\s]+$/, "");
  const room = MAX_QUESTION - tail.length - 1;
  return `${base.length > room ? base.slice(0, room).trimEnd() : base}.${tail}`;
}

/** The error of a run, in words for the person who is looking at it. The server says what
 * to do when its replay cache has no answer: ask a suggested question or add keys. Only
 * someone who runs the server can add keys, so the live app says something else. */
export function errorText(message: string | null, replay: boolean): string {
  const text = message ?? "Something went wrong. Try again.";
  if (replay || !/replay cache|add keys/i.test(text)) return text;
  return "This question could not be answered. Try again, or ask it in other words.";
}

/** A claim's trace label comes from the list it sits in (kept or unverified), and its
 * score is the support mass the keep rule uses, or the top choice's p for older cards. */
export function claimMark(
  claim: Claim,
  kept: boolean,
): { label: "Kept" | "Unverified"; score: number | null } {
  return { label: kept ? "Kept" : "Unverified", score: claim.support ?? claim.p };
}

