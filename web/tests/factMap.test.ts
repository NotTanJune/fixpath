import { describe, expect, test } from "bun:test";
import {
  caption,
  centreName,
  clickAction,
  drawOrder,
  facing,
  turnAim,
  firstSpanIndex,
  fitView,
  keyMove,
  lightUp,
  nearestPoint,
  typeWords,
  type FactMapData,
  type MapPoint,
} from "../src/lib/factMap";
import { plainLabel } from "../src/lib/format";
import type { Span } from "../src/lib/types";

const p = (over: Partial<MapPoint>): MapPoint => ({
  id: "x", kind: "node", type: "Part", label: "", docs: [], passages: [], x: 0, y: 0, z: 0, ...over,
});
const DATA: FactMapData = {
  model: "m", umap: {}, kg_sha256: "s", counts: { passages: 2, nodes: 2, links: 1 },
  points: [
    p({ id: "DOC-a-0-0", kind: "passage", type: "manual", docs: ["DOC-a"] }),
    p({ id: "DOC-b-0-0", kind: "passage", type: "handover", docs: ["DOC-b"] }),
    p({ id: "E-217", type: "AlarmCode", docs: ["DOC-a"], passages: ["DOC-a-0-0"] }),
    p({ id: "HP-01", type: "Asset", docs: ["DOC-a", "DOC-b"], passages: ["DOC-a-0-0", "DOC-b-0-0"] }),
    p({ id: "HP-01|RAISED|E-217", kind: "link", type: "RAISED", docs: ["DOC-c"], passages: [] }),
    p({ id: "DOC-a-0-1", kind: "passage", type: "manual", docs: ["DOC-a"] }),
    p({ id: "PART-1", type: "Part", docs: ["DOC-a"], passages: ["DOC-a-0-1"] }),
  ],
};
const span = (fact_id: string, kind: "node" | "edge" = "node"): Span => ({
  quote: "", start: 0, end: 0, page: null, fact_kind: kind, fact_id, type: "", label: "", extractor: "", confidence: 0.9,
});

describe("lightUp", () => {
  test("no shown set yet: the file's facts and passages are lit, the rest faint, no centre", () => {
    const lit = lightUp(DATA, "DOC-a", [span("E-217"), span("HP-01"), span("HP-01")], null);
    expect(lit.states).toEqual(["passage", "faint", "fact", "fact", "faint", "passage", "faint"]);
    expect(lit.facts).toBe(2);
    expect(lit.passages).toBe(2);
    expect(lit.centre).toBe(-1);
  });
  test("the centre is its own state, and so is a fact the pane shows", () => {
    const lit = lightUp(DATA, "DOC-a", [span("E-217"), span("HP-01")], { centre: "HP-01", ids: ["HP-01", "E-217"] });
    expect(lit.states[3]).toBe("centre");
    expect(lit.states[2]).toBe("fact");
    expect(lit.centre).toBe(3);
  });
  test("a fact of the file that the pane does not show is faint, and so is a passage nothing lit was quoted from", () => {
    const lit = lightUp(DATA, "DOC-a", [span("E-217"), span("HP-01"), span("PART-1")], { centre: "HP-01", ids: ["HP-01", "E-217"] });
    expect(lit.states).toEqual(["passage", "faint", "fact", "centre", "faint", "faint", "faint"]);
    expect(lit.facts).toBe(2);
    expect(lit.passages).toBe(1);
  });
  test("a shown id that the file does not hold is not blue", () => {
    const lit = lightUp(DATA, "DOC-a", [span("E-217")], { centre: "HP-01", ids: ["HP-01", "E-217", "PART-1"] });
    expect(lit.states[6]).toBe("faint");
    expect(lit.states[3]).toBe("centre");
  });
  test("the centre counts as a fact of the file only when the file holds it", () => {
    expect(lightUp(DATA, "DOC-a", [span("E-217"), span("HP-01")], { centre: "HP-01", ids: ["HP-01", "E-217"] }).facts).toBe(2);
    const other = lightUp(DATA, "DOC-a", [span("E-217")], { centre: "HP-01", ids: ["HP-01", "E-217"] });
    expect(other.facts).toBe(1);
    expect(other.states[3]).toBe("centre");
  });
  test("a centre from another file is yellow and lights nothing else of the file", () => {
    const lit = lightUp(DATA, "DOC-b", [], { centre: "HP-01", ids: ["HP-01"] });
    expect(lit.states).toEqual(["faint", "faint", "faint", "centre", "faint", "faint", "faint"]);
    expect(lit.facts).toBe(0);
    expect(lit.passages).toBe(0);
    expect(lit.lines).toEqual([]);
  });
  test("lines join a lit fact or the centre to its passages in the open file only", () => {
    expect(lightUp(DATA, "DOC-a", [span("E-217"), span("HP-01")], null).lines).toEqual([[2, 0], [3, 0]]);
    expect(lightUp(DATA, "DOC-a", [span("E-217"), span("HP-01")], { centre: "HP-01", ids: ["HP-01"] }).lines).toEqual([[3, 0]]);
  });
  test("an edge fact is lit by its edge id", () => {
    const lit = lightUp(DATA, "DOC-c", [span("HP-01|RAISED|E-217", "edge")], { centre: "E-217", ids: ["E-217", "HP-01|RAISED|E-217"] });
    expect(lit.states[4]).toBe("fact");
    expect(lit.states[2]).toBe("centre");
  });
  test("a span whose fact is not in the map is left out", () => {
    expect(lightUp(DATA, "DOC-a", [span("NOPE")], null).facts).toBe(0);
  });
});

describe("centreName", () => {
  test("an id and its name", () => {
    expect(centreName(p({ id: "HP-01", label: "Hydraulic press" }))).toBe("HP-01 Hydraulic press");
    expect(centreName(p({ id: "CNC-01", label: "Machining centre 1" }))).toBe("CNC-01 Machining centre 1");
  });
  test("a label that is only the id is the id", () => {
    expect(centreName(p({ id: "E-217", label: "E-217" }))).toBe("E-217");
    expect(centreName(p({ id: "E-217", label: "" }))).toBe("E-217");
  });
  test("a root cause is its name, as the right pane writes it", () => {
    expect(centreName(p({ id: "FM-AXIS-2-OVERLOAD", type: "FailureMode", label: "axis 2 overload" }))).toBe("Axis 2 overload");
  });
  test("a failure-mode id inside a label is read as its name", () => {
    expect(centreName(p({ id: "INC-1", type: "Incident", label: "Stopped by FM-AXIS-2-OVERLOAD" }))).toBe(
      `INC-1 ${plainLabel("Stopped by FM-AXIS-2-OVERLOAD")}`,
    );
  });
});

describe("caption", () => {
  const lead = "Close together means worded alike.";
  const none = "They come from a table or a log, so there are no passages.";
  test("a centre and blue facts, with passages", () => {
    expect(caption({ facts: 32, passages: 4, centre: "HP-01 Hydraulic press" })).toBe(
      `${lead} Blue: this file's 32 facts in the graph on the right. Yellow: HP-01 Hydraulic press. Black squares: the passages they came from.`,
    );
  });
  test("one blue fact", () => {
    expect(caption({ facts: 1, passages: 1, centre: "CNC-01 Machining centre 1" })).toBe(
      `${lead} Blue: this file's 1 fact in the graph on the right. Yellow: CNC-01 Machining centre 1. Black squares: the passages they came from.`,
    );
  });
  test("blue facts and no passages say why", () => {
    expect(caption({ facts: 5, passages: 0, centre: "HP-01 Hydraulic press" })).toBe(
      `${lead} Blue: this file's 5 facts in the graph on the right. Yellow: HP-01 Hydraulic press. ${none}`,
    );
  });
  test("a centre and no blue facts", () => {
    expect(caption({ facts: 0, passages: 0, centre: "INC-1 Stopped" })).toBe(
      `${lead} Yellow: INC-1 Stopped. None of its facts here come from this file.`,
    );
  });
  test("no centre yet", () => {
    expect(caption({ facts: 32, passages: 4, centre: null })).toBe(
      `${lead} Blue: this file's 32 facts. Black squares: its passages.`,
    );
    expect(caption({ facts: 1, passages: 1, centre: null })).toBe(`${lead} Blue: this file's 1 fact. Black squares: its passages.`);
  });
  test("no centre and no passages say why", () => {
    expect(caption({ facts: 3, passages: 0, centre: null })).toBe(`${lead} Blue: this file's 3 facts. ${none}`);
  });
});

describe("clickAction", () => {
  const spans = [span("E-217"), span("HP-01"), span("PART-1")];
  const at = (id: string) => DATA.points.find((q) => q.id === id)!;
  test("a fact of the open file picks its first span", () => {
    expect(clickAction(at("HP-01"), "DOC-a", [span("X"), span("HP-01"), span("HP-01")], DATA)).toEqual({ kind: "pick", index: 1 });
    expect(clickAction(at("E-217"), "DOC-a", spans, DATA)).toEqual({ kind: "pick", index: 0 });
  });
  test("a passage of the open file picks the first fact quoted from it, in span order", () => {
    expect(clickAction(at("DOC-a-0-0"), "DOC-a", [span("HP-01"), span("E-217")], DATA)).toEqual({ kind: "pick", index: 0 });
    expect(clickAction(at("DOC-a-0-0"), "DOC-a", [span("PART-1"), span("E-217")], DATA)).toEqual({ kind: "pick", index: 1 });
  });
  test("a passage of the open file that no fact of it was quoted from does nothing", () => {
    expect(clickAction(at("DOC-a-0-1"), "DOC-a", [span("E-217")], DATA)).toEqual({ kind: "none" });
  });
  test("a fact from another file opens the first of its files at that fact", () => {
    expect(clickAction(at("HP-01"), "DOC-z", [], DATA)).toEqual({ kind: "open", docId: "DOC-a", factId: "HP-01" });
  });
  test("a passage of another file opens that file with no fact", () => {
    expect(clickAction(at("DOC-b-0-0"), "DOC-a", spans, DATA)).toEqual({ kind: "open", docId: "DOC-b", factId: null });
  });
  test("a fact with no files does nothing", () => {
    expect(clickAction(p({ id: "LOST", docs: [] }), "DOC-a", spans, DATA)).toEqual({ kind: "none" });
  });
});

describe("nearestPoint", () => {
  const screen = [{ x: 0, y: 0 }, { x: 6, y: 0 }, { x: 100, y: 100 }];
  test("the nearest point within the radius", () => {
    expect(nearestPoint(screen, ["faint", "faint", "faint"], { x: 1, y: 0 })).toBe(0);
  });
  test("a lit point wins over a nearer faint one inside the radius", () => {
    expect(nearestPoint(screen, ["faint", "fact", "faint"], { x: 1, y: 0 })).toBe(1);
  });
  test("nothing within the radius", () => {
    expect(nearestPoint(screen, ["fact", "fact", "fact"], { x: 50, y: 50 })).toBeNull();
  });
});

test("drawOrder draws faint first and the centre last", () => {
  expect(drawOrder(["centre", "faint", "fact", "passage", "faint"])).toEqual([1, 4, 3, 2, 0]);
});

test("typeWords names a point in words", () => {
  expect(typeWords(p({ type: "AlarmCode" }))).toBe("Alarm code");
  expect(typeWords(p({ type: "Asset" }))).toBe("Machine");
  expect(typeWords(p({ kind: "link", type: "INDICATES" }))).toBe("Link: points to");
  expect(typeWords(p({ kind: "passage", type: "manual" }))).toBe("Manual passage");
  expect(typeWords(p({ kind: "passage", type: "maintenance_note" }))).toBe("Loose note passage");
});

test("firstSpanIndex finds the first span of a fact, or -1", () => {
  expect(firstSpanIndex([span("A"), span("B"), span("B")], "B")).toBe(1);
  expect(firstSpanIndex([span("A")], "Z")).toBe(-1);
});

describe("facing", () => {
  test("the camera moves onto the line from the centre through the point", () => {
    const [x, y, z] = facing([0, 0, 0.5], 3)!;
    expect([x, y, z].map((v) => +v.toFixed(6))).toEqual([0, 0, 3]);
  });
  test("a point at the centre gives no move", () => {
    expect(facing([0.01, 0, 0], 3)).toBeNull();
  });
});

describe("turnAim", () => {
  const near = (v: number[], w: number[]) => v.forEach((x, i) => expect(x).toBeCloseTo(w[i]!, 6));
  test("a point well inside the view is faced about the same target, as at home", () => {
    const aim = turnAim([0, 0, 0.5], [0, 0, 0], 3)!;
    expect(aim.target).toEqual([0, 0, 0]);
    near(aim.offset, [0, 0, 3]);
  });
  test("the target is the pivot, not the origin, once the view has moved", () => {
    const aim = turnAim([1, 0, 0.5], [1, 0, 0], 3)!;
    expect(aim.target).toEqual([1, 0, 0]);
    near(aim.offset, [0, 0, 3]);
  });
  test("a point far from the target, which a turn about it would leave behind the camera, becomes the target", () => {
    // 2 away from the pivot with the camera 3 back: the camera would stand beside the point
    const aim = turnAim([2, 0, 0], [0, 0, 0], 3)!;
    expect(aim.target).toEqual([2, 0, 0]);
    near(aim.offset, [3, 0, 0]);
  });
  test("the camera stays the same distance from the new target", () => {
    const aim = turnAim([0.3, 0.4, 1.2], [0, 0, 0], 0.8)!;
    expect(Math.hypot(...aim.offset)).toBeCloseTo(0.8, 6);
  });
  test("a point at the pivot gives no turn", () => {
    expect(turnAim([1.01, 0, 0], [1, 0, 0], 3)).toBeNull();
  });
});

describe("keyMove", () => {
  const home: [number, number, number] = [0, 0, 3];
  test("ArrowLeft turns about the vertical axis and keeps the distance", () => {
    const next = keyMove([0, 0, 3], "ArrowLeft", home)!;
    expect(Math.hypot(...next)).toBeCloseTo(3, 6);
    expect(next[1]).toBeCloseTo(0, 6);
    expect(next[0]).not.toBeCloseTo(0, 3);
  });
  test("+ moves closer, - further, within limits", () => {
    expect(Math.hypot(...keyMove([0, 0, 3], "+", home)!)).toBeLessThan(3);
    expect(Math.hypot(...keyMove([0, 0, 3], "-", home)!)).toBeGreaterThan(3);
    expect(Math.hypot(...keyMove([0, 0, 8], "-", home)!)).toBeLessThanOrEqual(8);
  });
  test("zoom stops at the limits it is given", () => {
    const limits = { near: 0.5, far: 2 };
    expect(Math.hypot(...keyMove([0, 0, 0.55], "+", home, limits)!)).toBeCloseTo(0.5, 6);
    expect(Math.hypot(...keyMove([0, 0, 1.9], "-", home, limits)!)).toBeCloseTo(2, 6);
    expect(Math.hypot(...keyMove([0, 0, 1.0], "+", home, limits)!)).toBeCloseTo(0.85, 6);
  });
  test("0 goes home, other keys do nothing", () => {
    expect(keyMove([1, 1, 1], "0", home)).toEqual(home);
    expect(keyMove([1, 1, 1], "a", home)).toBeNull();
  });
});

describe("fitView", () => {
  const tanV = Math.tan((45 / 2) * (Math.PI / 180)); // 0.41421356
  test("looks at the middle of the y spread", () => {
    expect(fitView([{ x: 0, y: -1, z: 0 }, { x: 0.2, y: 0.5, z: 0 }], 2).target).toEqual([0, -0.25, 0]);
  });
  test("points on the axis: the height alone sets the distance, hand-computed", () => {
    // half-height 1, nothing off the axis: 1 / (0.9 * tan 22.5 degrees) = 2.6824
    const { distance } = fitView([{ x: 0, y: -1, z: 0 }, { x: 0, y: 1, z: 0 }], 2);
    expect(distance).toBeCloseTo(1 / (0.9 * tanV), 6);
    expect(distance).toBeCloseTo(2.6824, 3);
  });
  test("a point off the axis swings nearer the camera, so the camera stands back by its radius", () => {
    // 1 off the axis, no height, aspect 1: width binds, sqrt(1 + 1 / (0.9 * tanV)^2) = 2.8628
    expect(fitView([{ x: 0, y: 0, z: 1 }], 1).distance).toBeCloseTo(Math.sqrt(1 + 1 / (0.9 * tanV) ** 2), 6);
    // 0.5 off the axis and 0.8 above the middle, wide view: 0.5 + 0.8 / (0.9 * tanV) = 2.6459
    const wide = fitView([{ x: 0.5, y: 0.8, z: 0 }, { x: 0, y: -0.8, z: 0 }], 100);
    expect(wide.distance).toBeCloseTo(0.5 + 0.8 / (0.9 * tanV), 6);
  });
  test("the fill share sets how much of the view the cloud takes", () => {
    const two = [{ x: 0, y: -1, z: 0 }, { x: 0, y: 1, z: 0 }];
    expect(fitView(two, 2, 45, 0.5).distance).toBeCloseTo(1 / (0.5 * tanV), 6);
  });
  test("a wider aspect never gives a larger distance", () => {
    const cloud = [{ x: 2, y: 0, z: 0 }, { x: 0, y: 1, z: 1 }, { x: -1, y: 0.4, z: 0 }];
    let last = Infinity;
    for (const aspect of [0.5, 1, 1.5, 2.2, 3, 6]) {
      const d = fitView(cloud, aspect).distance;
      expect(d).toBeLessThanOrEqual(last + 1e-9);
      last = d;
    }
  });
  test("no point leaves the view at any angle of the turn", () => {
    const cloud = [
      { x: 1, y: -0.9, z: 0.2 }, { x: -0.7, y: 0.6, z: 0.7 }, { x: 0.1, y: 0.1, z: -1 },
      { x: 0.9, y: 0.5, z: 0.1 }, { x: -0.3, y: -0.4, z: 0.3 },
    ];
    for (const aspect of [0.8, 1, 2.2, 4]) {
      const { target, distance } = fitView(cloud, aspect);
      for (const q of cloud) {
        const r = Math.hypot(q.x, q.z);
        const a0 = Math.atan2(q.x, q.z);
        for (let k = 0; k < 360; k += 2) {
          const a = a0 + (k * Math.PI) / 180;
          const depth = distance - r * Math.cos(a);
          expect(Math.abs(q.y - target[1]) / depth / tanV).toBeLessThanOrEqual(0.9 + 1e-9);
          expect(Math.abs(r * Math.sin(a)) / depth / (tanV * aspect)).toBeLessThanOrEqual(0.9 + 1e-9);
        }
      }
    }
  });
  test("an empty set gives the old home", () => {
    expect(fitView([], 2)).toEqual({ target: [0, 0, 0], distance: 3 });
  });
  test("one point still leaves the camera off it", () => {
    const one = fitView([{ x: 0.3, y: 0.2, z: 0 }], 2);
    expect(one.distance).toBeGreaterThanOrEqual(1);
    expect(one.target).toEqual([0, 0.2, 0]);
  });
});
