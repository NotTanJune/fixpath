import { describe, expect, test } from "bun:test";
import { clampPan, EXPLORER_FLOOR, explorerZoom, FRAMED_FLOOR, framedFit, KEEP_PX, NAME_FLOOR, needsFit, snapZoom, startPan, type Column } from "../src/lib/graphFit";
import { DETAIL_PX, NAME_PX, TYPE_PX } from "../src/lib/askGraph";
import { EDGE_PX } from "../src/lib/graphLabels";
import { MAGNET_W } from "../src/lib/explorerGraph";

const pane = { w: 570, h: 762 };

describe("fitting the answer's graph to its pane", () => {
  test("a graph that fits the pane at full size is fitted to it, not left small", () => {
    const fit = framedFit(pane, { w: 400, h: 500 });
    expect(fit.zoom).toBeGreaterThan(1);
    expect(fit.zoom * 500 + 24).toBeLessThanOrEqual(762 + 0.5); // no scrolling
    expect(fit.scrolls).toBe(false);
  });

  test("a graph that would fit only by going under 14 px keeps its size and the pane scrolls", () => {
    const fit = framedFit(pane, { w: 400, h: 935 });
    expect(fit.zoom).toBe(FRAMED_FLOOR);
    expect(FRAMED_FLOOR).toBe(1); // the smallest words are drawn at 14 px
    expect(fit.scrolls).toBe(true);
    expect(fit.height).toBeGreaterThan(762);
  });

  test("a graph as tall as the pane at exactly full size does not scroll", () => {
    const fit = framedFit(pane, { w: 400, h: 762 - 24 });
    expect(fit.zoom).toBe(1);
    expect(fit.scrolls).toBe(false);
  });

  test("the width of the pane still limits the size", () => {
    const fit = framedFit({ w: 300, h: 900 }, { w: 562, h: 600 });
    expect(fit.zoom).toBeCloseTo((300 - 24) / 562, 5);
  });

  test("the zoom stays between the limits", () => {
    expect(framedFit({ w: 2000, h: 2000 }, { w: 100, h: 100 }).zoom).toBe(1.2);
    expect(framedFit({ w: 50, h: 2000 }, { w: 1000, h: 100 }, 12, 0.2, 1.2).zoom).toBe(0.2);
  });
});

describe("the Pipeline explorer keeps its words readable", () => {
  test("at the least zoom, a name is drawn at 14 px or more and the type, detail and line words at 12 px or more", () => {
    expect(NAME_PX * EXPLORER_FLOOR).toBeGreaterThanOrEqual(14);
    expect(TYPE_PX * EXPLORER_FLOOR).toBeGreaterThanOrEqual(12);
    expect(DETAIL_PX * EXPLORER_FLOOR).toBeGreaterThanOrEqual(12);
    expect(EDGE_PX * EXPLORER_FLOOR).toBeGreaterThanOrEqual(12);
  });

  test("a graph that is wider than the pane is panned and not shrunk below the floor", () => {
    const direct = 2 * MAGNET_W + 140; // the centre and one column of links
    const two = 3 * MAGNET_W + 140 + 170; // two columns
    for (const pane of [360, 420, 470, 520, 576, 700, 1000]) {
      for (const box of [direct, two]) {
        const zoom = explorerZoom(pane, box);
        expect(zoom).toBeGreaterThanOrEqual(EXPLORER_FLOOR);
        expect(zoom).toBeLessThanOrEqual(1);
        expect(NAME_PX * zoom).toBeGreaterThanOrEqual(14);
        expect(DETAIL_PX * zoom).toBeGreaterThanOrEqual(12);
      }
    }
  });

  test("a graph that fits is drawn at full size, and a middling one is fitted to the width", () => {
    expect(explorerZoom(1000, 548)).toBe(1);
    expect(explorerZoom(500, 548)).toBeCloseTo((500 - 24) / 548, 5);
  });
});

describe("the explorer's graph cannot be scrolled out of its pane", () => {
  const view = { w: 540, h: 585 };
  test("a graph in view keeps its pan", () => {
    expect(clampPan({ x: 10, y: 10 }, { x1: 20, y1: 20, x2: 400, y2: 900 }, view)).toEqual({ x: 10, y: 10 });
  });
  test("a graph scrolled far above the pane comes back to show its last stretch", () => {
    // rendered 2352 to 1901 px above the top, as the bug left it
    const next = clampPan({ x: 126, y: -2349 }, { x1: 120, y1: -2352, x2: 470, y2: -1901 }, view);
    expect(next.x).toBe(126);
    expect(-1901 + (next.y - -2349)).toBe(KEEP_PX);
  });
  test("a graph scrolled far below or to a side comes back the same way", () => {
    const below = clampPan({ x: 0, y: 3000 }, { x1: 0, y1: 3000, x2: 300, y2: 3400 }, view);
    expect(3000 + (below.y - 3000)).toBe(view.h - KEEP_PX);
    const left = clampPan({ x: -2000, y: 0 }, { x1: -2000, y1: 0, x2: -1700, y2: 300 }, view);
    expect(-1700 + (left.x - -2000)).toBe(KEEP_PX);
    const right = clampPan({ x: 2000, y: 0 }, { x1: 2000, y1: 0, x2: 2300, y2: 300 }, view);
    expect(2000 + (right.x - 2000)).toBe(view.w - KEEP_PX);
  });
  test("a graph smaller than the margin keeps all of itself in view", () => {
    const next = clampPan({ x: 0, y: -500 }, { x1: 0, y1: -500, x2: 40, y2: -470 }, view);
    expect(-500 + (next.y - -500)).toBe(0);
  });
});

describe("where the explorer's tree starts in its pane", () => {
  const pane = { w: 600, h: 700 };
  test("a tree that fits is centred across the pane and starts at the top", () => {
    const at = startPan(pane, { x1: 0, y1: 0, x2: 400 }, 1, 200, 12);
    expect(at.x).toBe(100);
    expect(at.y).toBe(12);
  });
  test("a tree wider than the pane starts with its top card in the middle of the pane", () => {
    // 1400 px wide at zoom 1, the top card's middle at 700: the pane shows 400 to 1000
    const at = startPan(pane, { x1: 0, y1: 0, x2: 1400 }, 1, 700, 12);
    expect(at.x).toBe(300 - 700);
    expect(at.y).toBe(12);
  });
  test("a top card near an edge does not leave a gap there", () => {
    const left = startPan(pane, { x1: 0, y1: 0, x2: 1400 }, 1, 100, 12);
    expect(left.x).toBe(12);
    const right = startPan(pane, { x1: 0, y1: 0, x2: 1400 }, 1, 1300, 12);
    expect(right.x).toBe(600 - 12 - 1400);
  });
  test("zoom scales the box and the top card", () => {
    const at = startPan(pane, { x1: 100, y1: 40, x2: 1500 }, 0.86, 800, 12);
    expect(at.x).toBeCloseTo(300 - 800 * 0.86, 6);
    expect(at.y).toBeCloseTo(12 - 40 * 0.86, 6);
  });
  test("with no top card the box starts at the left", () => {
    expect(startPan(pane, { x1: 0, y1: 0, x2: 1400 }, 1, undefined, 12).x).toBe(12);
  });
});

describe("where the explorer's tree starts, by whole columns", () => {
  // four columns of 180 px, 30 px apart; the centre card (204 wide) is over the middle two
  const col = (i: number, weight: number): Column => ({ x1: i * 210, x2: i * 210 + 180, weight });
  const cols = [col(0, 1), col(1, 3), col(2, 6), col(3, 17)];
  const box = { x1: 0, y1: 0, x2: 810 };
  const centre = { x1: 303, x2: 507 };
  const Z = EXPLORER_FLOOR;
  const pane = { w: 541 };
  const view = (pan: { x: number }, z = Z, w = pane.w) => ({ x1: (12 - pan.x) / z, x2: (w - 12 - pan.x) / z });
  const whole = (pan: { x: number }, z = Z, w = pane.w) => {
    const v = view(pan, z, w);
    return cols.filter((c) => c.x1 >= v.x1 - 1e-9 && c.x2 <= v.x2 + 1e-9);
  };
  const start = (c = cols, b = box, ce = centre, w = pane.w, z = Z) => startPan({ w }, b, z, ce.x1 / 2 + ce.x2 / 2, 12, { columns: c, centre: ce });

  test("the left edge of the view is the left edge of a column", () => {
    const at = start();
    const v = view(at);
    expect(cols.some((c) => Math.abs(c.x1 - v.x1) < 1e-9)).toBe(true);
  });

  test("it shows three whole columns and the centre card in a 541 px pane at the zoom floor", () => {
    const at = start();
    expect(whole(at)).toHaveLength(3);
    const v = view(at);
    expect(centre.x1).toBeGreaterThanOrEqual(v.x1);
    expect(centre.x2).toBeLessThanOrEqual(v.x2);
  });

  test("on a tie it shows the heaviest branch", () => {
    // columns 0 to 2 and columns 1 to 3 are both three whole columns, and both hold the centre card
    const at = start();
    expect(whole(at).map((c) => c.weight)).toContain(17);
    // the other way round: the heaviest on the left
    const flip = [col(0, 17), col(1, 6), col(2, 3), col(3, 1)];
    const v = view(start(flip));
    expect(flip.filter((c) => c.x1 >= v.x1 && c.x2 <= v.x2).map((c) => c.weight)).toContain(17);
  });

  test("the most whole columns win over the heaviest branch", () => {
    // a pane that holds two columns and not three
    const at = start(cols, box, centre, 420);
    expect(whole(at, Z, 420)).toHaveLength(2);
    const heavy = [col(0, 1), col(1, 1), col(2, 1), col(3, 99)];
    const v = view(start(heavy, box, centre, 420), Z, 420);
    expect(heavy.filter((c) => c.x1 >= v.x1 && c.x2 <= v.x2)).toHaveLength(2);
  });

  test("the centre card in view wins over more whole columns", () => {
    // the centre card is over the last column, which is the heaviest: the view has to hold it
    const right = { x1: 560, x2: 764 };
    const at = startPan({ w: 541 }, box, Z, 662, 12, { columns: cols, centre: right });
    const v = view(at);
    expect(right.x1).toBeGreaterThanOrEqual(v.x1);
    expect(right.x2).toBeLessThanOrEqual(v.x2);
  });

  test("a tree that fits is still centred, and the y is still the top", () => {
    const small = [col(0, 1), col(1, 2)];
    const at = startPan({ w: 541 }, { x1: 0, y1: 40, x2: 390 }, Z, 195, 12, { columns: small, centre: { x1: 93, x2: 297 } });
    expect(at.x).toBeCloseTo((541 - 390 * Z) / 2, 6);
    expect(at.y).toBeCloseTo(12 - 40 * Z, 6);
  });

  test("with no columns it starts as before", () => {
    expect(startPan({ w: 600 }, { x1: 0, y1: 0, x2: 1400 }, 1, 700, 12, { columns: [], centre: { x1: 600, x2: 800 } }).x).toBe(300 - 700);
  });
});

describe("a first view that ends in a gap, not in a column", () => {
  const col = (i: number, weight: number): Column => ({ x1: i * 210, x2: i * 210 + 180, weight });
  const cols = [col(0, 1), col(1, 3), col(2, 6), col(3, 17)];
  const centre = { x1: 303, x2: 507 };
  const Z = EXPLORER_FLOOR;
  const cutAt = (w: number, z: number) => {
    const at = startPan({ w }, { x1: 0, y1: 0, x2: 810 }, z, 405, 12, { columns: cols, centre });
    const v1 = (12 - at.x) / z;
    const v2 = (w - 12 - at.x) / z;
    return cols.filter((c) => c.x1 < v2 && c.x2 > v2 + 1e-6 && c.x2 > v1);
  };

  test("a pane that shows a column in part is drawn a little smaller, so that the column is whole", () => {
    // 524 px: 500 px of view at the floor is 581 graph units, 19 short of three columns
    expect(cutAt(524, Z)).toHaveLength(1);
    const z = snapZoom(524, 810, Z, cols, centre);
    expect(z).toBeCloseTo(500 / 600, 6);
    expect(z).toBeLessThan(Z);
    expect(cutAt(524, z)).toHaveLength(0);
  });

  test("it never goes below the zoom that keeps a name at 14 px", () => {
    expect(NAME_FLOOR).toBeCloseTo(14 / 17, 10);
    // 480 px: three columns would need 0.76
    expect(snapZoom(480, 810, Z, cols, centre)).toBe(Z);
  });

  test("a pane that cuts no column keeps its zoom", () => {
    expect(cutAt(541, Z)).toHaveLength(0);
    expect(snapZoom(541, 810, Z, cols, centre)).toBe(Z);
  });

  test("a tree that fits keeps its zoom", () => {
    expect(snapZoom(900, 810, 1, cols, centre)).toBe(1);
  });
});

describe("when the graph is fitted to its pane again", () => {
  test("a graph that is not kept is fitted whenever its nodes change", () => {
    expect(needsFit({ keepView: false, fitted: true, changed: true })).toBe(true);
    expect(needsFit({ keepView: false, fitted: true, changed: false })).toBe(false);
  });
  test("a kept view is fitted once, and the nodes that come after do not move it", () => {
    expect(needsFit({ keepView: true, fitted: false, changed: true })).toBe(true);
    expect(needsFit({ keepView: true, fitted: true, changed: true })).toBe(false);
    expect(needsFit({ keepView: true, fitted: true, changed: false })).toBe(false);
  });
});
