// The fact map: what is lit for the open file, the lines from its facts to its passages, the
// caption under the map, which point is under the pointer, and the keyboard moves. The scene
// itself is FactMap.tsx; everything here is pure so the tests can run it without WebGL.
import { plainLabel, sentenceCase, VERBS } from "./format";
import type { Span } from "./types";

export interface MapPoint {
  id: string;
  kind: "passage" | "node" | "link";
  type: string;
  label: string;
  docs: string[];
  passages?: string[];
  x: number;
  y: number;
  z: number;
}

export interface FactMapData {
  model: string;
  umap: Record<string, number | string>;
  kg_sha256: string;
  counts: { passages: number; nodes: number; links: number };
  points: MapPoint[];
}

export type PointState = "faint" | "passage" | "fact" | "centre";

/** What the graph pane draws: the node it is centred on, and the ids of the nodes and edges it shows. */
export interface Shown {
  centre: string;
  ids: string[];
}

export interface Lit {
  states: PointState[];
  /** The facts of the open file that are blue, plus the centre when the file holds it. */
  facts: number;
  passages: number;
  lines: [number, number][];
  /** The index of the centre point, or -1. */
  centre: number;
}

export type Vec3 = [number, number, number];

/** The Map / List choice, kept per viewer. */
export const MAP_VIEW_KEY = "facts-view";

/** Point sizes in CSS px. The centre is the largest so it reads first. */
export const POINT_SIZE: Record<PointState, number> = { faint: 4, passage: 8, fact: 7, centre: 14 };

const DRAW_RANK: Record<PointState, number> = { faint: 0, passage: 1, fact: 2, centre: 3 };

/** What is lit. The centre of the graph pane is yellow, wherever its file is. A fact of the open
 * file that the pane shows is blue, and so is its passage of the open file; everything else is
 * faint. Before the pane has anything to show, every fact and passage of the file is lit. */
export function lightUp(data: FactMapData, docId: string, spans: Span[], shown: Shown | null): Lit {
  const factIds = new Set(spans.map((s) => s.fact_id));
  const ids = shown ? new Set(shown.ids) : null;
  const isFact = (p: MapPoint) => p.kind !== "passage" && factIds.has(p.id);
  const states: PointState[] = data.points.map((p) => {
    if (shown && p.kind !== "passage" && p.id === shown.centre) return "centre";
    if (p.kind === "passage") return !shown && p.docs.includes(docId) ? "passage" : "faint";
    return isFact(p) && (!ids || ids.has(p.id)) ? "fact" : "faint";
  });
  const passageAt = new Map<string, number>();
  data.points.forEach((p, i) => {
    if (p.kind === "passage") passageAt.set(p.id, i);
  });
  // the passages that a lit point of the open file was quoted from
  const lines: [number, number][] = [];
  data.points.forEach((p, i) => {
    if (p.kind === "passage" || states[i] === "faint" || !isFact(p)) return;
    for (const chunk of p.passages ?? []) {
      const j = passageAt.get(chunk);
      if (j === undefined || !data.points[j]!.docs.includes(docId)) continue;
      states[j] = "passage";
      lines.push([i, j]);
    }
  });
  return {
    states,
    facts: data.points.filter((p, i) => (states[i] === "fact" || states[i] === "centre") && isFact(p)).length,
    passages: states.filter((s) => s === "passage").length,
    lines,
    centre: states.indexOf("centre"),
  };
}

/** A centre point in the words the graph pane uses: its ID and its name, or only the name for a
 * root cause. */
export function centreName(point: MapPoint): string {
  const label = plainLabel(point.label);
  if (point.type === "FailureMode") return sentenceCase(label || point.id);
  return label && label !== point.id ? `${point.id} ${label}` : point.id;
}

/** The first sentence under the map, whatever it shows. */
export const LEAD = "Close together means worded alike.";
const NO_PASSAGES = "They come from a table or a log, so there are no passages.";

/** The line under the map: what close together means, then what blue, yellow and the black squares are. */
export function caption(input: { facts: number; passages: number; centre: string | null }): string {
  const { facts, passages, centre } = input;
  const some = `${facts} ${facts === 1 ? "fact" : "facts"}`;
  if (centre !== null && facts === 0) {
    return `${LEAD} Yellow: ${centre}. None of its facts here come from this file.`;
  }
  if (centre !== null) {
    const tail = passages === 0 ? NO_PASSAGES : "Black squares: the passages they came from.";
    return `${LEAD} Blue: this file's ${some} in the graph on the right. Yellow: ${centre}. ${tail}`;
  }
  const tail = passages === 0 ? NO_PASSAGES : "Black squares: its passages.";
  return `${LEAD} Blue: this file's ${some}. ${tail}`;
}

/** What a click on a point does. A point of the open file moves the document to its fact (a
 * passage, to the first fact quoted from it); a point of another file opens that file. */
export type ClickAction =
  | { kind: "pick"; index: number }
  | { kind: "open"; docId: string; factId: string | null }
  | { kind: "none" };

export function clickAction(point: MapPoint, docId: string, spans: Span[], data: FactMapData): ClickAction {
  if (point.kind === "passage") {
    if (!point.docs.includes(docId)) {
      const own = point.docs[0];
      return own ? { kind: "open", docId: own, factId: null } : { kind: "none" };
    }
    const quoting = new Set(
      data.points.filter((q) => q.kind !== "passage" && q.passages?.includes(point.id)).map((q) => q.id),
    );
    const index = spans.findIndex((s) => quoting.has(s.fact_id));
    return index >= 0 ? { kind: "pick", index } : { kind: "none" };
  }
  const index = firstSpanIndex(spans, point.id);
  if (index >= 0) return { kind: "pick", index };
  const own = point.docs[0];
  return own ? { kind: "open", docId: own, factId: point.id } : { kind: "none" };
}

/** The point under the pointer: within radius px, a lit point before a faint one, then the nearest. */
export function nearestPoint(
  screen: { x: number; y: number }[],
  states: PointState[],
  at: { x: number; y: number },
  radius = 10,
): number | null {
  let best: number | null = null;
  let bestLit = false;
  let bestD = Infinity;
  screen.forEach((s, i) => {
    const d = Math.hypot(s.x - at.x, s.y - at.y);
    if (d > radius) return;
    const lit = states[i] !== "faint";
    if ((lit && !bestLit) || (lit === bestLit && d < bestD)) {
      best = i;
      bestLit = lit;
      bestD = d;
    }
  });
  return best;
}

/** Point indices in the order to draw them: faint first, the centre last, so lit points sit on top. */
export function drawOrder(states: PointState[]): number[] {
  return states
    .map((s, i) => [DRAW_RANK[s], i] as const)
    .sort((a, b) => a[0] - b[0] || a[1] - b[1])
    .map(([, i]) => i);
}

const TYPE_WORDS: Record<string, string> = {
  AlarmCode: "Alarm code",
  Asset: "Machine",
  Component: "Component",
  FailureMode: "Failure mode",
  Incident: "Incident",
  Line: "Line",
  Part: "Part",
  Procedure: "Procedure",
};
const PASSAGE_WORDS: Record<string, string> = {
  manual: "Manual passage",
  handover: "Handover passage",
  maintenance_note: "Loose note passage",
};

export function typeWords(point: MapPoint): string {
  if (point.kind === "passage") return PASSAGE_WORDS[point.type] ?? "Passage";
  if (point.kind === "link") return `Link: ${VERBS[point.type] ?? point.type.toLowerCase().replace(/_/g, " ")}`;
  return TYPE_WORDS[point.type] ?? point.type;
}

export function firstSpanIndex(spans: Span[], factId: string): number {
  return spans.findIndex((s) => s.fact_id === factId);
}

/** Where the camera goes so the point faces the viewer: on the line from the centre through the
 * point, at the same distance. A point near the centre gives null (any side faces it). */
export function facing(point: Vec3, distance: number): Vec3 | null {
  const len = Math.hypot(...point);
  if (len < 0.05) return null;
  return [(point[0] / len) * distance, (point[1] / len) * distance, (point[2] / len) * distance];
}

/** Where the camera goes to face a point, as the target it looks at and its offset from that
 * target. A point within half the camera's distance of the pivot is faced about the pivot: the
 * camera swings onto the line from the pivot through the point. A point farther out would end up
 * beside or behind the camera, which happens when the view has been zoomed in on another place, so
 * the camera looks at that point instead, from the side the pivot is on. A point at the pivot gives
 * null (any side faces it). */
export function turnAim(point: Vec3, pivot: Vec3, distance: number): { target: Vec3; offset: Vec3 } | null {
  const rel: Vec3 = [point[0] - pivot[0], point[1] - pivot[1], point[2] - pivot[2]];
  const offset = facing(rel, distance);
  if (!offset) return null;
  return Math.hypot(...rel) <= distance / 2 ? { target: pivot, offset } : { target: point, offset };
}

const TURN = Math.PI / 18; // 10 degrees a key press

export interface ZoomLimits {
  near: number;
  far: number;
}
const LIMITS: ZoomLimits = { near: 1.2, far: 8 };

/** Where the camera looks and how far back it stands so the whole cloud stays inside `fill` of
 * the view while it turns about the vertical axis. A point at radius r from that axis swings to
 * r nearer the camera, where perspective makes it larger, so each point is fitted at its
 * nearest: the height needs d >= r + |dy| / (fill * tan v), and the width, at the angle where the
 * point is widest, needs d >= r * sqrt(1 + 1 / (fill * tan h)^2). v and h are the half field of
 * view across and along, with tan h = tan v * aspect. */
export function fitView(
  points: { x: number; y: number; z: number }[],
  aspect: number,
  fovDeg = 45,
  fill = 0.9,
): { target: Vec3; distance: number } {
  if (!points.length) return { target: [0, 0, 0], distance: 3 };
  let minY = Infinity;
  let maxY = -Infinity;
  for (const p of points) {
    minY = Math.min(minY, p.y);
    maxY = Math.max(maxY, p.y);
  }
  const middle = (minY + maxY) / 2;
  const tanV = Math.tan((fovDeg / 2) * (Math.PI / 180));
  const tanH = tanV * aspect;
  let distance = 0;
  for (const p of points) {
    const r = Math.hypot(p.x, p.z);
    const high = r + Math.abs(p.y - middle) / (fill * tanV);
    const wide = r * Math.sqrt(1 + 1 / (fill * tanH) ** 2);
    distance = Math.max(distance, high, wide);
  }
  return { target: [0, middle, 0], distance: Math.max(1, distance) };
}

/** The camera position after a key: arrows turn about the centre, + and - zoom, 0 goes home. */
export function keyMove(position: Vec3, key: string, home: Vec3, limits: ZoomLimits = LIMITS): Vec3 | null {
  if (key === "0") return [...home];
  const [x, y, z] = position;
  const r = Math.hypot(x, y, z);
  let theta = Math.atan2(x, z); // about the vertical axis
  let phi = Math.acos(Math.min(1, Math.max(-1, y / r))); // from the top
  let dist = r;
  if (key === "ArrowLeft") theta -= TURN;
  else if (key === "ArrowRight") theta += TURN;
  else if (key === "ArrowUp") phi = Math.max(0.1, phi - TURN);
  else if (key === "ArrowDown") phi = Math.min(Math.PI - 0.1, phi + TURN);
  else if (key === "+" || key === "=") dist = Math.max(limits.near, r * 0.85);
  else if (key === "-") dist = Math.min(limits.far, r / 0.85);
  else return null;
  return [dist * Math.sin(phi) * Math.sin(theta), dist * Math.cos(phi), dist * Math.sin(phi) * Math.cos(theta)];
}
