import { describe, expect, test } from "bun:test";
import type { MapPoint } from "../src/lib/factMap";
import type { MapRun } from "../src/lib/liveIngest";
import { outside, planRun, spriteAt, visibleLines, type Planned } from "../src/lib/mapRun";

const point = (id: string, kind: MapPoint["kind"], x: number, y: number, z: number, passages?: string[]): MapPoint => ({
  id,
  kind,
  type: "T",
  label: id,
  docs: [],
  x,
  y,
  z,
  ...(passages ? { passages } : {}),
});

const run: MapRun = {
  key: 1,
  passages: [point("P1", "passage", 1, 0, 0), point("P2", "passage", 0, 1, 0)],
  facts: [point("F1", "node", 0.5, 0, 0, ["P1"]), point("F2", "link", 0, 0.5, 0, ["P1", "P2"])],
  flash: ["X", "GONE"],
};
const times = { fly: 1000, flyStagger: 200, factPop: 300, flash: 800 };
const existing = { X: [0.1, 0.2, 0.3] as [number, number, number] };
const plan = (animate = { flash: true, placed: true }, over = times) =>
  planRun(run, existing, 5000, over, { centre: [0, 0, 0], radius: 1 }, animate);
const by = (list: Planned[], key: string) => list.find((p) => p.key === key)!;

describe("the edge of the map", () => {
  test("a point goes out along the line from the middle of the cloud, past its radius", () => {
    expect(outside([1, 0, 0], { centre: [0, 0, 0], radius: 1 }, 2)).toEqual([2, 0, 0]);
    expect(outside([0, 0.5, 0], { centre: [0, 0, 0], radius: 1 }, 3)).toEqual([0, 3, 0]);
    const o = outside([1, 1, 1], { centre: [1, 1, 1], radius: 1 }, 2);
    expect(Math.hypot(o[0] - 1, o[1] - 1, o[2] - 1)).toBeCloseTo(2);
  });
});

describe("the plan of a run", () => {
  test("the matched points that the map has flash; the ones it does not have are left out", () => {
    const p = plan();
    expect(p.filter((q) => q.key.startsWith("flash:")).map((q) => q.key)).toEqual(["flash:X"]);
    const f = by(p, "flash:X");
    expect([f.kind, f.tone, f.from, f.to, f.start, f.dur]).toEqual(["ring", "mark", [0.1, 0.2, 0.3], [0.1, 0.2, 0.3], 5000, 800]);
  });

  test("each passage sets off a little after the one before, from the edge, and lands with a ring", () => {
    const p = plan();
    const a = by(p, "p:P1");
    const b = by(p, "p:P2");
    expect([a.kind, a.shape, a.tone, a.start, a.dur]).toEqual(["fly", "square", "ink", 5000, 1000]);
    expect([b.start, b.dur]).toEqual([5200, 1000]);
    expect(a.to).toEqual([1, 0, 0]);
    expect(a.from[0]).toBeGreaterThan(1);
    const ring = by(p, "land:P1");
    expect([ring.kind, ring.start]).toEqual(["ring", 6000]);
    expect(by(p, "land:P2").start).toBe(6200);
  });

  test("the facts appear one after another once the last passage has landed", () => {
    const p = plan();
    const f1 = by(p, "f:F1");
    const f2 = by(p, "f:F2");
    expect([f1.kind, f1.shape, f1.tone]).toEqual(["pop", "circle", "blue"]);
    expect(f1.start).toBe(5000 + 200 + 1000);
    expect(f2.start).toBe(f1.start + 300);
    expect(f1.dur).toBe(300);
  });

  test("with no motion everything is where it ends, at once, and nothing flashes", () => {
    const p = plan({ flash: true, placed: true }, { fly: 0, flyStagger: 0, factPop: 0, flash: 0 });
    expect(p.some((q) => q.kind === "ring")).toBe(false);
    for (const q of p) expect(q.dur).toBe(0);
    expect(spriteAt(by(p, "p:P1"), 5000)).toMatchObject({ x: 1, y: 0, z: 0, alpha: 1 });
    expect(spriteAt(by(p, "f:F1"), 5000)).toMatchObject({ x: 0.5, size: 7 });
  });

  test("a scene that is built again leaves the run where it was, with no flash and no flight", () => {
    const p = plan({ flash: false, placed: false });
    expect(p.some((q) => q.key.startsWith("flash:") || q.kind === "ring")).toBe(false);
    expect(spriteAt(by(p, "p:P2"), 5000)).toMatchObject({ x: 0, y: 1 });
  });

  test("a run with nothing placed plans only the flash", () => {
    const only = planRun({ ...run, passages: [], facts: [] }, existing, 0, times, { centre: [0, 0, 0], radius: 1 }, { flash: true, placed: true });
    expect(only.map((q) => q.key)).toEqual(["flash:X"]);
  });
});

describe("a point at a moment", () => {
  const p = plan();
  test("a passage is not there before its turn, flies in, and stays where it lands", () => {
    const a = by(p, "p:P1");
    expect(spriteAt(a, 4999)).toBeNull();
    const start = spriteAt(a, 5000)!;
    expect(start.x).toBeCloseTo(a.from[0]);
    const mid = spriteAt(a, 5500)!;
    expect(mid.x).toBeLessThan(a.from[0]);
    expect(mid.x).toBeGreaterThan(1);
    expect(spriteAt(a, 6000)).toMatchObject({ x: 1, y: 0, z: 0, size: 8, shape: "square", tone: "ink", alpha: 1 });
    expect(spriteAt(a, 99999)).toMatchObject({ x: 1 });
  });

  test("a fact pops: it grows past its size and settles on it", () => {
    const f = by(p, "f:F1");
    expect(spriteAt(f, f.start - 1)).toBeNull();
    const early = spriteAt(f, f.start + 30)!;
    expect(early.size).toBeLessThan(7);
    const top = Math.max(...Array.from({ length: 30 }, (_, i) => spriteAt(f, f.start + (i * f.dur) / 30)!.size));
    expect(top).toBeGreaterThan(7);
    expect(spriteAt(f, f.start + f.dur)).toMatchObject({ size: 7, x: 0.5, shape: "circle", tone: "blue" });
  });

  test("a ring swells and fades, and is gone when it ends", () => {
    const r = by(p, "flash:X");
    const a = spriteAt(r, 5100)!;
    const b = spriteAt(r, 5600)!;
    expect(b.size).toBeGreaterThan(a.size);
    expect(b.alpha).toBeLessThan(a.alpha);
    expect(a.shape).toBe("ring");
    expect(spriteAt(r, 4999)).toBeNull();
    expect(spriteAt(r, 5800)).toBeNull();
  });
});

describe("the lines from facts to their passages", () => {
  const p = plan();
  test("a fact has a line to a passage once both are there", () => {
    expect(visibleLines(p, 5000)).toEqual([]);
    // the facts start at 6200; the passages have landed by 6200 (P2 at 6200)
    expect(visibleLines(p, 6200)).toEqual([[[0.5, 0, 0], [1, 0, 0]]]);
    expect(visibleLines(p, 6500)).toEqual([
      [[0.5, 0, 0], [1, 0, 0]],
      [[0, 0.5, 0], [1, 0, 0]],
      [[0, 0.5, 0], [0, 1, 0]],
    ]);
  });
  test("a passage that was not placed is skipped", () => {
    const few = p.filter((q) => q.key !== "p:P2");
    expect(visibleLines(few, 9000)).toHaveLength(2);
  });
});
