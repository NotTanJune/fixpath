// The Run report page of Ingestion: the shape of /api/pipeline/latest, the totals row, and the
// plain words for a merge decision and a review item. Pure, so the tests can run it.
import { basename, fixed, money } from "./format";
import type { Route } from "./router";

export interface Merge {
  backend: string | null;
  candidate: string | null;
  decision: string;
  doc_path: string;
  mention: string;
  p: number | null;
  type: string;
}

export interface FileRun {
  doc_path: string;
  doc_type: string;
  extractor: string;
  candidates: { chunks: number; nodes: Record<string, number>; edges: Record<string, number> };
  flags: { rule: string; severity: string; subject: string; detail: string }[];
  merges: Merge[];
}

export interface ReviewItem {
  id: number;
  kind: string;
  payload: Record<string, any> & { doc_paths?: string[]; reason?: string };
  confidence: number | null;
}

export interface PipelineReport {
  files: FileRun[];
  run: { loaded: Record<string, number>; mode: string; ran_at: string; seconds: number };
  totals: {
    merges: Record<string, number>;
    review: Record<string, number>;
    flags: { warning: number; error: number };
    cost: { usd: number; calls: number; cache_hits: number; paid_calls: number };
  };
  review_queue: ReviewItem[];
}

/** The part of the Ingestion route that opens the Run report instead of a file. */
export const REPORT_ARG = "report";

export function reportView(route: Route): boolean {
  return route.view === "ingestion" && route.arg === REPORT_ARG;
}

export interface Figure {
  key: string;
  value: string;
  label: string;
}

const count = (n: number | undefined, one: string, many: string) => ((n ?? 0) === 1 ? one : many);

/** The totals row: what the run loaded, what it flagged, how long it took and what it cost. */
export function runFigures(report: PipelineReport): Figure[] {
  const { run, totals } = report;
  const files = run.loaded["documents"] ?? 0;
  const nodes = run.loaded["nodes"] ?? 0;
  const links = run.loaded["edges"] ?? 0;
  const { error, warning } = totals.flags;
  return [
    { key: "files", value: String(files), label: count(files, "file", "files") },
    { key: "nodes", value: String(nodes), label: count(nodes, "node", "nodes") },
    { key: "links", value: String(links), label: count(links, "link", "links") },
    { key: "errors", value: String(error), label: count(error, "error", "errors") },
    { key: "warnings", value: String(warning), label: count(warning, "warning", "warnings") },
    { key: "time", value: `${run.seconds.toFixed(1)} s`, label: "time" },
    { key: "cost", value: money(totals.cost.usd), label: "cost" },
  ];
}

const DECISIONS: Record<string, string> = {
  merge: "same",
  new: "different",
  queue: "unsure",
  unresolved: "no match",
  rule: "by rule",
};

/** What a merge decision says about a mention and the node it was matched to. */
export function decisionWord(decision: string): string {
  return DECISIONS[decision] ?? decision;
}

export interface ReviewRow {
  id: number;
  kind: string;
  text: string;
  about: string;
  paths: string[];
}

/** A review item as a row: what kind it is, what is wrong, what it is about and which files it names. */
export function reviewRow(item: ReviewItem): ReviewRow {
  const p = item.payload;
  const kind = item.kind === "validator" ? String(p["severity"] ?? "warning") : item.kind === "link" ? "root cause" : item.kind;
  const base: string = p.reason ?? (p["detail"] as string | undefined) ?? "";
  const diffs = Object.entries((p["differences"] ?? {}) as Record<string, unknown[]>)
    .map(([key, values]) => `${key} ${values.map(String).join(" vs ")}`)
    .join(", ");
  const records = ((p["records"] ?? []) as string[][]).map((r) => basename(r[1] ?? ""));
  const chance = item.kind === "merge" && typeof p["p"] === "number" ? `probability ${fixed(p["p"])}` : "";
  const text = [base, diffs ? `${base ? ": " : ""}${diffs}` : ""].join("") + (chance ? `${base ? ", " : ""}${chance}` : "");
  const about = String(p["mention"] ?? p["key"] ?? p["subject"] ?? (records.length ? records.join(" and ") : ""));
  return { id: item.id, kind, text, about, paths: p.doc_paths ?? [] };
}


/** The node a merge was matched to. A pair of incident records is "file::file#n": the record of the
 * other file is its file name and number. */
export function matchName(candidate: string | null): string {
  if (candidate === null) return "none";
  const at = candidate.indexOf("::");
  return at < 0 ? candidate : basename(candidate.slice(at + 2));
}
