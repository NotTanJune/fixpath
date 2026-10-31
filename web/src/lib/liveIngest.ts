// The live ingestion view as a pure function: the events of one file's ingest in, the state of the
// view out. A live run and a recorded one feed it the same events, and everything the views draw
// (the step bar, the chunks, the points on the map, the nodes of the tree) is read from the state
// by the helpers below, so a test can run it without a browser.
//
// The server sends `converted`, `step` (one per graph node), `extracted`, `chunked`, `facts`,
// `matched`, `placed` and `done`; `error`, `skipped` and, in a recording, `refused` end a run early.
// `begin` and `reset` are not the server's: the page sends `begin` when it starts a run, with the
// clock in ms, and `reset` when it closes the view.

import { LEAD } from "./factMap";
import type { MapPoint } from "./factMap";

export type StepKey = "read" | "chunk" | "embed" | "facts" | "match" | "link" | "load";

/** The steps in the order of the bar. */
export const STEPS: { key: StepKey; label: string }[] = [
  { key: "read", label: "Read" },
  { key: "chunk", label: "Chunk" },
  { key: "embed", label: "Embed" },
  { key: "facts", label: "Facts" },
  { key: "match", label: "Match" },
  { key: "link", label: "Link" },
  { key: "load", label: "Load" },
];

/** The order the events end the steps in: the passages are embedded while the file is loaded, so
 * Embed ends after Link. */
const ARRIVAL: StepKey[] = ["read", "chunk", "facts", "match", "link", "embed", "load"];

/** The line a recorded run ends on. */
export const RECORDED_END = "Recorded run. The graph here is unchanged.";
const NO_WORDS = "The file was not added. Try again.";

export type StepStatus = "pending" | "active" | "done" | "failed";

export interface StepState {
  key: StepKey;
  label: string;
  status: StepStatus;
  /** Seconds since the event before, once the step has ended. */
  seconds: number | null;
}

export interface Chunk {
  id: string;
  page: number | null;
  ord: number;
  text: string;
}

export interface FactNode {
  id: string;
  type: string;
  label: string;
}

export interface FactEdge {
  id: string;
  src: string;
  rel: string;
  dst: string;
}

export interface PlacedPassage {
  id: string;
  x: number;
  y: number;
  z: number;
}

export interface PlacedFact {
  id: string;
  kind: "node" | "link";
  type: string;
  label: string;
  x: number;
  y: number;
  z: number;
  passages: string[];
}

export interface LiveState {
  status: "idle" | "running" | "done" | "failed";
  /** Which run this is, so the views can tell a new run from the one before. */
  run: number;
  /** The time of the last event, in ms. */
  clock: number;
  /** Runs ahead of this one, when the server has said. */
  queued: number | null;
  steps: StepState[];
  doc: { path: string; type: string } | null;
  converted: { from: string; to: string; chars: number } | null;
  chunks: Chunk[];
  facts: { nodes: FactNode[]; edges: FactEdge[] } | null;
  matched: { merged: { from: string; to: string }[]; new: string[] } | null;
  placed: { passages: PlacedPassage[]; facts: PlacedFact[] } | null;
  loaded: { nodes: number; edges: number } | null;
  error: string | null;
  failedAt: StepKey | null;
}

export interface RunEvent {
  event: string;
  data: unknown;
  /** When it happened, in ms. */
  at: number;
}

const blankSteps = (): StepState[] => STEPS.map((s) => ({ ...s, status: "pending", seconds: null }));

export const idleRun: LiveState = {
  status: "idle",
  run: 0,
  clock: 0,
  queued: null,
  steps: blankSteps(),
  doc: null,
  converted: null,
  chunks: [],
  facts: null,
  matched: null,
  placed: null,
  loaded: null,
  error: null,
  failedAt: null,
};

/** A recorded time ("2026-01-01T00:00:03Z") in ms; 0 for one that is not a time. */
export function parseAt(text: string): number {
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : 0;
}

/** The first step in the order of events that has not ended. */
function activeKey(steps: StepState[]): StepKey | null {
  return ARRIVAL.find((k) => steps.find((s) => s.key === k)!.status !== "done") ?? null;
}

/** The steps after one has ended at `at`: its seconds are the time since the event before. */
function ended(state: LiveState, key: StepKey, at: number): StepState[] {
  const seconds = Math.round(Math.max(0, at - state.clock)) / 1000;
  const steps = state.steps.map((s) => (s.key === key && s.status !== "done" ? { ...s, status: "done" as const, seconds } : s));
  return mark(steps);
}

/** The step the run is on is active; the others keep their state. */
function mark(steps: StepState[]): StepState[] {
  const now = activeKey(steps);
  return steps.map((s) => (s.status === "done" ? s : { ...s, status: s.key === now ? "active" : "pending" }));
}

const asRecord = (data: unknown): Record<string, any> => (data && typeof data === "object" ? (data as Record<string, any>) : {});
const list = <T>(value: unknown): T[] => (Array.isArray(value) ? (value as T[]) : []);

/** The run after one more event. An event of a kind it does not know, or one after the run has
 * ended, leaves the state as it was. */
export function liveIngest(state: LiveState, event: RunEvent): LiveState {
  const d = asRecord(event.data);
  if (event.event === "reset") return idleRun;
  if (event.event === "begin") {
    return { ...idleRun, status: "running", run: Number(d.run ?? state.run + 1), clock: event.at, steps: mark(blankSteps()) };
  }
  if (state.status !== "running") return state;
  const to = (patch: Partial<LiveState>, key?: StepKey): LiveState => ({
    ...state,
    ...patch,
    clock: event.at,
    ...(key ? { steps: ended(state, key, event.at) } : {}),
  });
  switch (event.event) {
    case "queued":
      return { ...state, queued: Number(d.position ?? 0), clock: event.at };
    case "converted":
      return { ...state, converted: { from: String(d.from ?? ""), to: String(d.to ?? ""), chars: Number(d.chars ?? 0) } };
    case "step": {
      if (d.node === "link") return to({}, "link");
      if (d.node === "load") return to({}, "load");
      return { ...state, clock: event.at };
    }
    case "extracted":
      return to({ doc: { path: String(d.doc_path ?? ""), type: String(d.doc_type ?? "") }, queued: null }, "read");
    case "chunked":
      return to({ chunks: list<Chunk>(d.chunks) }, "chunk");
    case "facts":
      return to({ facts: { nodes: list<FactNode>(d.nodes), edges: list<FactEdge>(d.edges) } }, "facts");
    case "matched":
      return to({ matched: { merged: list(d.merged), new: list<string>(d.new) } }, "match");
    case "placed":
      return to({ placed: { passages: list<PlacedPassage>(d.passages), facts: list<PlacedFact>(d.facts) } }, "embed");
    case "done": {
      const loaded = asRecord(asRecord(asRecord(d.report).run).loaded);
      const steps = ended(state, "load", event.at);
      return {
        ...state,
        status: "done",
        clock: event.at,
        steps: steps.map((s) => (s.status === "done" ? s : { ...s, status: "done" as const })),
        loaded: typeof loaded.nodes === "number" ? { nodes: loaded.nodes, edges: Number(loaded.edges ?? 0) } : null,
      };
    }
    case "error":
      return stopped(state, event.at, typeof d.message === "string" && d.message ? d.message : NO_WORDS);
    case "refused":
      return stopped(state, event.at, typeof d.detail === "string" && d.detail ? d.detail : NO_WORDS);
    case "skipped":
      return stopped(state, event.at, `Skipped ${d.doc_path}: ${d.reason}.`);
    default:
      return state;
  }
}

/** The run ended early: the step it was on failed, and the words are the server's. */
function stopped(state: LiveState, at: number, error: string): LiveState {
  const key = activeKey(state.steps) ?? "load";
  return {
    ...state,
    status: "failed",
    clock: at,
    error,
    failedAt: key,
    steps: state.steps.map((s) => (s.key === key ? { ...s, status: "failed" as const } : s)),
  };
}

/** The ids of the existing nodes that the file was matched to, each once, in the order of the match. */
export function flashIds(state: LiveState): string[] {
  return [...new Set((state.matched?.merged ?? []).map((m) => m.to))];
}

/** The ids of the nodes the file added: those the match called new and those that were placed. */
export function freshIds(state: LiveState): string[] {
  const placed = (state.placed?.facts ?? []).filter((f) => f.kind === "node").map((f) => f.id);
  return [...new Set([...(state.matched?.new ?? []), ...placed])];
}

/** The nodes and links that the load added to the graph, from the points it placed. A link point
 * is named "from|RELATION|to". */
export function runGraph(state: LiveState): {
  nodes: { id: string; label: string; type: string }[];
  edges: { id: string; source: string; target: string; rel: string }[];
} {
  const nodes: { id: string; label: string; type: string }[] = [];
  const edges: { id: string; source: string; target: string; rel: string }[] = [];
  for (const f of state.placed?.facts ?? []) {
    if (f.kind === "node") nodes.push({ id: f.id, label: f.label, type: f.type });
    else {
      const [source, rel, target, ...rest] = f.id.split("|");
      if (source && rel && target && !rest.length) edges.push({ id: f.id, source, target, rel });
    }
  }
  return { nodes, edges };
}

/** The node the tree centres on: the most connected of the nodes this file matched or added that
 * the graph draws (they are in `degree`), the lower id on a tie; null when there is none. */
export function runCentre(state: LiveState, degree: Map<string, number>): string | null {
  const candidates = [...new Set([...flashIds(state), ...freshIds(state)])].filter((id) => degree.has(id));
  if (!candidates.length) return null;
  return candidates.reduce((best, id) => {
    const d = degree.get(id)!;
    const b = degree.get(best)!;
    return d > b || (d === b && id < best) ? id : best;
  });
}

/** What the map draws for the run: the points that were placed, and the existing points the file
 * matched. `key` is the run, so that a map can tell a new run from the one before. */
export interface MapRun {
  key: number;
  passages: MapPoint[];
  facts: MapPoint[];
  flash: string[];
}

export function mapRun(state: LiveState): MapRun | null {
  if (!state.placed && !state.matched) return null;
  const text = new Map(state.chunks.map((c) => [c.id, c.text]));
  const type = state.doc?.type ?? "";
  const passages: MapPoint[] = (state.placed?.passages ?? []).map((p) => ({
    id: p.id,
    kind: "passage",
    type,
    label: text.get(p.id) ?? "",
    docs: [],
    x: p.x,
    y: p.y,
    z: p.z,
  }));
  const facts: MapPoint[] = (state.placed?.facts ?? []).map((f) => ({
    id: f.id,
    kind: f.kind,
    type: f.type,
    label: f.label,
    docs: [],
    passages: f.passages,
    x: f.x,
    y: f.y,
    z: f.z,
  }));
  return { key: state.run, passages, facts, flash: flashIds(state) };
}

/** The line under the map while a run is on it: what the squares and the dots are. */
export function runCaption(run: MapRun | null): string {
  if (!run || (!run.passages.length && !run.facts.length)) return LEAD;
  const n = run.passages.length;
  const m = run.facts.length;
  const squares = n ? `Black squares: the ${n} ${n === 1 ? "passage" : "passages"} of this file.` : "";
  const dots = m ? `Blue: the ${m} ${m === 1 ? "fact" : "facts"} placed${n ? " from it" : ""}.` : "It placed no new facts.";
  return [LEAD, squares, dots].filter(Boolean).join(" ");
}

/** How many of `total` marks show `elapsed` ms after the first, one every `step` ms, the first at
 * once. With no step they all show. */
export function marked(total: number, elapsed: number, step: number): number {
  if (total <= 0) return 0;
  if (step <= 0) return total;
  return Math.min(total, Math.floor(Math.max(0, elapsed) / step) + 1);
}
