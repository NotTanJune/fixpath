// The motion of a run on the fact map, as pure functions: a plan of which point comes when, and
// what each point looks like at a moment. The scene (FactMap.tsx) draws what these say, so a test
// can run the motion without WebGL.
//
// A passage square flies in from outside the cloud to its place, one after another, and lands with
// a ring. When the last has landed the fact dots pop in, one after another, each with its line to the
// passages it was quoted from. A point of the map that the file matched flashes a yellow ring.

import { POINT_SIZE, type Vec3 } from "./factMap";
import type { MapRun } from "./liveIngest";

export type Tone = "ink" | "blue" | "mark";

export interface Planned {
  key: string;
  kind: "fly" | "pop" | "ring";
  shape: "circle" | "square" | "ring";
  tone: Tone;
  from: Vec3;
  to: Vec3;
  /** In ms, on the clock of the page. */
  start: number;
  dur: number;
  /** The size in px at rest; of a ring, the size it swells to. */
  size: number;
  /** For a fact: the ids of the passages it hangs from. */
  line?: string[];
}

export interface Sprite {
  x: number;
  y: number;
  z: number;
  size: number;
  alpha: number;
  shape: Planned["shape"];
  tone: Tone;
}

export interface Times {
  fly: number;
  flyStagger: number;
  factPop: number;
  flash: number;
}

/** How far outside the cloud a passage sets off, in radii of the cloud. */
const OUTSIDE = 1.8;
const FLASH_SIZE = 34;
const LAND_SIZE = 24;

/** A point `factor` radii out from the middle of the cloud, on the line from the middle through the
 * point (along x for a point at the middle). */
export function outside(point: Vec3, cloud: { centre: Vec3; radius: number }, factor: number): Vec3 {
  const d: Vec3 = [point[0] - cloud.centre[0], point[1] - cloud.centre[1], point[2] - cloud.centre[2]];
  const len = Math.hypot(...d);
  const u: Vec3 = len < 1e-6 ? [1, 0, 0] : [d[0] / len, d[1] / len, d[2] / len];
  const r = cloud.radius * factor;
  return [cloud.centre[0] + u[0] * r, cloud.centre[1] + u[1] * r, cloud.centre[2] + u[2] * r];
}

/** What to draw for a run. `existing` holds the positions of the map's own points by id (the
 * ones the file matched); `at` is the moment the run's points arrived. `animate` says which parts
 * are new to the viewer: a part that is not has no flash and no flight and is simply there. */
export function planRun(
  run: MapRun,
  existing: Record<string, Vec3>,
  at: number,
  t: Times,
  cloud: { centre: Vec3; radius: number },
  animate: { flash: boolean; placed: boolean },
): Planned[] {
  const out: Planned[] = [];
  if (animate.flash && t.flash > 0) {
    for (const id of run.flash) {
      const pos = existing[id];
      if (!pos) continue;
      out.push({ key: `flash:${id}`, kind: "ring", shape: "ring", tone: "mark", from: pos, to: pos, start: at, dur: t.flash, size: FLASH_SIZE });
    }
  }
  const moving = animate.placed;
  const n = run.passages.length;
  run.passages.forEach((p, i) => {
    const to: Vec3 = [p.x, p.y, p.z];
    const start = at + (moving ? i * t.flyStagger : 0);
    const dur = moving ? t.fly : 0;
    out.push({
      key: `p:${p.id}`,
      kind: "fly",
      shape: "square",
      tone: "ink",
      from: moving ? outside(to, cloud, OUTSIDE) : to,
      to,
      start,
      dur,
      size: POINT_SIZE.passage,
    });
    if (moving && t.flash > 0) {
      out.push({ key: `land:${p.id}`, kind: "ring", shape: "ring", tone: "ink", from: to, to, start: start + dur, dur: t.flash * 0.6, size: LAND_SIZE });
    }
  });
  const base = at + (moving && n ? (n - 1) * t.flyStagger + t.fly : 0);
  run.facts.forEach((f, j) => {
    const to: Vec3 = [f.x, f.y, f.z];
    out.push({
      key: `f:${f.id}`,
      kind: "pop",
      shape: "circle",
      tone: "blue",
      from: to,
      to,
      start: base + (moving ? j * t.factPop : 0),
      dur: moving ? t.factPop : 0,
      size: POINT_SIZE.fact,
      line: f.passages ?? [],
    });
  });
  return out;
}

const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);
const easeOutBack = (t: number) => {
  const c1 = 1.70158;
  const c3 = c1 + 1;
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
};

/** One planned point at `now`; null while it is not there (not yet, or a ring that has ended). */
export function spriteAt(p: Planned, now: number): Sprite | null {
  if (now < p.start) return null;
  const t = p.dur > 0 ? Math.min(1, (now - p.start) / p.dur) : 1;
  const at = (pos: Vec3, size: number, alpha: number): Sprite => ({
    x: pos[0],
    y: pos[1],
    z: pos[2],
    size,
    alpha,
    shape: p.shape,
    tone: p.tone,
  });
  if (p.kind === "ring") {
    if (t >= 1) return null;
    const e = easeOut(t);
    return at(p.to, p.size * (0.3 + 0.7 * e), 1 - t);
  }
  if (p.kind === "pop") return at(p.to, p.size * (t < 1 ? easeOutBack(t) : 1), 1);
  const e = easeOut(t);
  const pos: Vec3 = [p.from[0] + (p.to[0] - p.from[0]) * e, p.from[1] + (p.to[1] - p.from[1]) * e, p.from[2] + (p.to[2] - p.from[2]) * e];
  return at(pos, p.size, 1);
}

/** The lines to draw at `now`: from each fact that has appeared to each of its passages that has landed. */
export function visibleLines(planned: Planned[], now: number): [Vec3, Vec3][] {
  const landed = new Map(planned.filter((p) => p.kind === "fly" && now >= p.start + p.dur).map((p) => [p.key, p.to]));
  const lines: [Vec3, Vec3][] = [];
  for (const f of planned) {
    if (f.kind !== "pop" || now < f.start) continue;
    for (const id of f.line ?? []) {
      const to = landed.get(`p:${id}`);
      if (to) lines.push([f.to, to]);
    }
  }
  return lines;
}
