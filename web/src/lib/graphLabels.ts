// Where the words of the lines of an answer's graph are drawn. Every label is placed here, in
// pure geometry, so that a test can check it: one label for a fan-out of lines that share a
// magnet and a relation, on the line they share, never within MARGIN of a line that is not its
// own, never on a magnet, never on another label.

import type { GEdge, GNode } from "./askGraph";
import type { Measure } from "./textMetrics";

/** The size of the words on a line, in px of the graph plane. */
export const EDGE_PX = 14;
const LABEL_H = 23; // the line of the words and the backing around them
const LABEL_PAD = 8; // the backing, both sides, beyond the words
/** The room kept between a label and a line that is not its own. */
export const MARGIN = 12;
const NODE_MARGIN = 3;
const LABEL_MARGIN = 6;
const STEP = 2; // how finely a line is searched for a place

/** One straight piece of a line, in px of the graph plane. */
export interface Seg {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** A label: the words and the box that backs them (the centre, and the size), and the edges
 * that it names. */
export interface GLabel {
  id: string;
  text: string;
  x: number;
  y: number;
  w: number;
  h: number;
  edges: string[];
}

/** The pieces of the line that an edge is drawn as, the way the graph draws it: a line between two
 * magnets of one column runs straight from the edge of one to the edge of the other; a line between
 * the column of the path and the column beside it leaves sideways, turns in the middle of the gap,
 * and enters sideways. */
export function edgeSegments(edge: Pick<GEdge, "source" | "target" | "from">, nodes: GNode[], gap: number): Seg[] {
  const a = nodes.find((n) => n.id === edge.source);
  const b = nodes.find((n) => n.id === edge.target);
  if (!a || !b) return [];
  if (edge.from !== undefined) {
    // the line from a fix down to the page that hangs under it, leaving right of the stroke
    const x = a.x + a.w / 2 + edge.from;
    const y1 = a.y + a.h / 2;
    const y2 = b.y - b.h / 2;
    return y2 > y1 ? [{ x1: x, y1, x2: x, y2 }] : [];
  }
  if (a.side === b.side) {
    const x = a.x + a.w / 2;
    const [up, down] = a.y <= b.y ? [a, b] : [b, a];
    const y1 = up.y + up.h / 2;
    const y2 = down.y - down.h / 2;
    return y2 > y1 ? [{ x1: x, y1, x2: x, y2 }] : [];
  }
  const [near, far] = a.side ? [b, a] : [a, b]; // the magnet of the path, and the one beside it
  const from = near.x + near.w;
  const turn = from + gap / 2;
  const segs: Seg[] = [
    { x1: from, y1: near.y, x2: turn, y2: near.y },
    { x1: turn, y1: near.y, x2: turn, y2: far.y },
    { x1: turn, y1: far.y, x2: far.x, y2: far.y },
  ];
  return segs.filter((s) => s.x1 !== s.x2 || s.y1 !== s.y2);
}

/** The gap between a box (centre and size) and a straight piece; 0 when they touch or cross. */
function clearance(x: number, y: number, w: number, h: number, s: Seg): number {
  const dx = Math.max(0, Math.min(s.x1, s.x2) - (x + w / 2), x - w / 2 - Math.max(s.x1, s.x2));
  const dy = Math.max(0, Math.min(s.y1, s.y2) - (y + h / 2), y - h / 2 - Math.max(s.y1, s.y2));
  return Math.hypot(dx, dy);
}

function apart(a: { x: number; y: number; w: number; h: number }, b: { x: number; y: number; w: number; h: number }, room: number) {
  return (
    a.x + a.w / 2 + room <= b.x - b.w / 2 ||
    b.x + b.w / 2 + room <= a.x - a.w / 2 ||
    a.y + a.h / 2 + room <= b.y - b.h / 2 ||
    b.y + b.h / 2 + room <= a.y - a.h / 2
  );
}

/** The lines that share one label: the lines between one magnet of the path and the column beside
 * it that say the same thing in the same direction (three past breakdowns that were the cause, a
 * group of cited pages); any other line is its own group. */
function groupsOf(edges: GEdge[], nodes: GNode[]): GEdge[][] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const groups = new Map<string, GEdge[]>();
  for (const e of edges) {
    const a = byId.get(e.source);
    const b = byId.get(e.target);
    if (!a || !b) continue;
    const cross = a.side !== b.side;
    const key = cross ? `${a.side ? e.target : e.source}|${e.rel}|${a.side ? "in" : "out"}` : e.id;
    groups.set(key, [...(groups.get(key) ?? []), e]);
  }
  return [...groups.values()];
}

/** One label for each group of lines, on a line of the group, as near the middle of its longest
 * piece as the room allows. A place is free when its box is clear of every magnet, MARGIN from
 * every line of another group and clear of the labels placed before it. A group with no free
 * place gets the one that is least in the way. */
export function layoutLabels(edges: GEdge[], nodes: GNode[], gap: number, m: Measure): GLabel[] {
  const segsOf = new Map(edges.map((e) => [e.id, edgeSegments(e, nodes, gap)]));
  const groups = groupsOf(edges, nodes);
  const placed: GLabel[] = [];
  // the groups with the fewest places are placed first, so that they are not crowded out
  const order = groups
    .map((g, i) => ({ g, i, len: g.flatMap((e) => segsOf.get(e.id) ?? []).length }))
    .sort((p, q) => p.len - q.len || p.i - q.i);
  for (const { g } of order) {
    const text = g[0]!.label;
    const w = Math.ceil(m(text, EDGE_PX, 600)) + LABEL_PAD;
    const h = LABEL_H;
    const own = new Set(g.map((e) => e.id));
    const mine = [...new Map(g.flatMap((e) => segsOf.get(e.id) ?? []).map((s) => [`${s.x1},${s.y1},${s.x2},${s.y2}`, s])).values()];
    const others = edges.filter((e) => !own.has(e.id)).flatMap((e) => segsOf.get(e.id) ?? []);
    const boxes = nodes.map((n) => ({ x: n.x + n.w / 2, y: n.y, w: n.w + 2 * NODE_MARGIN, h: n.h + 2 * NODE_MARGIN }));
    // the middle of the longest piece is where the words read best
    const longest = [...mine].sort((p, q) => Math.hypot(q.x2 - q.x1, q.y2 - q.y1) - Math.hypot(p.x2 - p.x1, p.y2 - p.y1))[0];
    const ideal = longest ? { x: (longest.x1 + longest.x2) / 2, y: (longest.y1 + longest.y2) / 2 } : { x: 0, y: 0 };

    let best: { x: number; y: number; cost: number; need: number } | null = null;
    for (const s of mine) {
      const length = Math.hypot(s.x2 - s.x1, s.y2 - s.y1);
      const steps = Math.max(1, Math.floor(length / STEP));
      for (let i = 0; i <= steps; i++) {
        const t = i / steps;
        const x = s.x1 + (s.x2 - s.x1) * t;
        const y = s.y1 + (s.y2 - s.y1) * t;
        // how far this place is from being free: 0 when it is
        let need = 0;
        for (const o of others) need = Math.max(need, MARGIN - clearance(x, y, w, h, o));
        for (const b of boxes) if (!apart({ x, y, w, h }, b, 0)) need = Math.max(need, MARGIN);
        for (const l of placed) if (!apart({ x, y, w, h }, l, LABEL_MARGIN)) need = Math.max(need, MARGIN);
        const cost = Math.hypot(x - ideal.x, y - ideal.y);
        if (!best || need < best.need || (need === best.need && cost < best.cost)) best = { x, y, cost, need };
      }
    }
    const at = best ?? ideal;
    placed.push({
      id: g.map((e) => e.id).join("+"),
      text,
      x: at.x,
      y: at.y,
      w,
      h,
      edges: g.map((e) => e.id),
    });
  }
  return placed;
}
