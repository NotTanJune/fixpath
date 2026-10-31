import { describe, expect, test } from "bun:test";
import {
  anchorShift,
  BOX_PER_FONT,
  canvasScale,
  centreScroll,
  clampZoom,
  commonWidth,
  fitZoom,
  focusRect,
  READ_PX,
  READ_PX_MIN,
  neighbourRects,
  pageZoom,
  readingZoom,
  stepZoom,
  textPx,
  wheelZoom,
  ZOOM_MAX,
  ZOOM_MIN,
} from "../src/lib/pdfZoom";

// a page is 612 wide at zoom 1; a line of 10 pt text has a box of 10.8 pt
const line = (y: number, h = 10 * BOX_PER_FONT) => ({ x: 72, y, w: 300, h });

describe("the zoom that reads", () => {
  test("text of 10 pt is drawn at the reading size or larger, at the zoom that is chosen", () => {
    const zoom = readingZoom([line(100)], 0.8);
    expect(textPx(line(100).h, zoom)).toBeGreaterThanOrEqual(READ_PX - 0.01);
    expect(textPx(line(100).h, zoom)).toBeLessThan(READ_PX + 1);
  });

  test("a pane that already fits the text at reading size stays at fit", () => {
    expect(readingZoom([line(100)], 2)).toBe(2);
  });

  test("the middle height of the rectangles decides, so one big heading does not set the size", () => {
    const rects = [line(10, 20), line(40), line(60), line(80)];
    expect(readingZoom(rects, 0.5)).toBeCloseTo(readingZoom([line(40)], 0.5));
  });

  test("a line wider than the view is drawn smaller to fit it, but never below the least that reads", () => {
    const wide = [{ x: 72, y: 100, w: 400, h: 10 * BOX_PER_FONT }];
    const zoom = readingZoom(wide, 0.5, 500);
    expect(zoom).toBeLessThan(readingZoom(wide, 0.5));
    expect(textPx(wide[0]!.h, zoom)).toBeGreaterThanOrEqual(READ_PX_MIN - 0.01);
    // a view that is still too narrow gets the least that reads, and the line is panned
    expect(textPx(wide[0]!.h, readingZoom(wide, 0.5, 300))).toBeCloseTo(READ_PX_MIN, 1);
    // a view that holds the line at the reading size changes nothing
    expect(readingZoom(wide, 0.5, 2000)).toBe(readingZoom(wide, 0.5));
  });

  test("with no rectangle the zoom is the fit", () => {
    expect(readingZoom([], 0.9)).toBe(0.9);
  });

  test("tiny text cannot push the zoom past the limit", () => {
    expect(readingZoom([line(10, 0.5)], 0.5)).toBe(ZOOM_MAX);
  });
});

describe("fit to the pane", () => {
  test("the widest page fills the pane", () => {
    expect(fitZoom(800, 612)).toBeCloseTo(800 / 612);
  });
  test("a pane of no width still gives a usable zoom", () => {
    expect(fitZoom(0, 612)).toBeGreaterThanOrEqual(ZOOM_MIN);
    expect(fitZoom(-50, 612)).toBeGreaterThanOrEqual(ZOOM_MIN);
  });
  test("a very wide pane stops at the largest zoom", () => {
    expect(fitZoom(9000, 100)).toBe(ZOOM_MAX);
  });
});

describe("fit to the pane with pages of mixed sizes", () => {
  const portrait = 595;
  const landscape = 842;
  const manual = [portrait, portrait, landscape, landscape, portrait, portrait, portrait];

  test("the common width is the width most pages have", () => {
    expect(commonWidth(manual)).toBe(portrait);
    expect(commonWidth([landscape])).toBe(landscape);
  });
  test("widths that differ by rounding count as one", () => {
    expect(commonWidth([595.28, 595.3, 842, 595.32])).toBeCloseTo(595.28, 0);
  });
  test("a tie goes to the narrower width; no pages gives no width", () => {
    expect(commonWidth([842, 595])).toBe(595);
    expect(commonWidth([])).toBe(0);
  });
  test("fit is set by the common width, so portrait pages fill the pane", () => {
    expect(fitZoom(590, commonWidth(manual))).toBeCloseTo(590 / portrait);
  });
  test("in fit mode a page wider than the common width fills the pane at its own zoom", () => {
    const base = fitZoom(590, portrait);
    expect(pageZoom(base, true, 590, landscape, portrait)).toBeCloseTo(590 / landscape);
    expect(pageZoom(base, true, 590, portrait, portrait)).toBeCloseTo(base);
  });
  test("a page narrower than the common width keeps the common zoom", () => {
    const base = fitZoom(590, portrait);
    expect(pageZoom(base, true, 590, 400, portrait)).toBeCloseTo(base);
  });
  test("once a zoom is set one zoom applies to every page", () => {
    expect(pageZoom(1.5, false, 590, landscape, portrait)).toBe(1.5);
    expect(pageZoom(1.5, false, 590, portrait, portrait)).toBe(1.5);
  });
});

describe("steps and limits", () => {
  test("a step goes to the next stop above or below, whatever the zoom is now", () => {
    expect(stepZoom(1, 1)).toBeCloseTo(1.25);
    expect(stepZoom(1, -1)).toBeCloseTo(0.75);
    expect(stepZoom(1.1, 1)).toBeCloseTo(1.25);
    expect(stepZoom(1.1, -1)).toBeCloseTo(1);
    expect(stepZoom(0.6, -1)).toBeCloseTo(0.5);
  });
  test("the steps end at the limits", () => {
    expect(stepZoom(ZOOM_MAX, 1)).toBe(ZOOM_MAX);
    expect(stepZoom(ZOOM_MIN, -1)).toBe(ZOOM_MIN);
    expect(clampZoom(99)).toBe(ZOOM_MAX);
    expect(clampZoom(0.01)).toBe(ZOOM_MIN);
    expect(clampZoom(Number.NaN)).toBe(1);
  });
  test("a wheel with Ctrl or Cmd zooms by the distance it turned, up when it turns away", () => {
    expect(wheelZoom(1, -100)).toBeGreaterThan(1);
    expect(wheelZoom(1, 100)).toBeLessThan(1);
    expect(wheelZoom(1, 0)).toBe(1);
    expect(wheelZoom(ZOOM_MAX, -500)).toBe(ZOOM_MAX);
  });
});

describe("the rectangle to show", () => {
  test("a quote of one or two lines is shown whole, in its box", () => {
    const r = focusRect([line(100, 10), line(114, 10)], 400);
    expect(r).toEqual({ x: 72, y: 100, w: 300, h: 24 });
  });
  test("a quote taller than the view is shown from its first line", () => {
    const r = focusRect([line(100), line(500)], 300);
    expect(r).toEqual(line(100));
  });
  test("nothing to show gives null", () => {
    expect(focusRect([], 300)).toBeNull();
  });
});

describe("centring the quote in the view", () => {
  const view = { w: 600, h: 500 };
  const page = { x: 24, y: 300 }; // the page's top left in the content, px
  test("the middle of the rectangle goes to the middle of the view", () => {
    const rect = { x: 100, y: 200, w: 200, h: 20 };
    const zoom = 2;
    const at = centreScroll(rect, zoom, page, view, { w: 2000, h: 4000 });
    // the rectangle's middle in the content: x 24 + 2 * 200, y 300 + 2 * 210
    expect(at).toEqual({ left: 24 + 400 - 300, top: 300 + 420 - 250 });
  });
  test("a quote near the top left cannot scroll to a negative place", () => {
    expect(centreScroll({ x: 0, y: 0, w: 10, h: 10 }, 1, { x: 24, y: 40 }, view, { w: 2000, h: 4000 })).toEqual({
      left: 0,
      top: 0,
    });
  });
  test("a quote near the end cannot scroll past the end of the content", () => {
    const at = centreScroll({ x: 590, y: 790, w: 10, h: 10 }, 1, { x: 24, y: 3900 }, view, { w: 700, h: 4000 });
    expect(at).toEqual({ left: 100, top: 3500 });
  });
  test("a rectangle wider than the view starts at its left edge, with a little room", () => {
    const at = centreScroll({ x: 100, y: 0, w: 400, h: 10 }, 2, { x: 24, y: 0 }, view, { w: 2000, h: 4000 });
    expect(at.left).toBe(24 + 200 - 24);
  });
  describe("when the view pans sideways, the lines around the quote start at the edge", () => {
    const content = { w: 2000, h: 4000 };
    const around = [{ x: 72, y: 190, w: 400, h: 10 }]; // the lines around start at x 72
    test("the start of the surrounding lines goes to the edge while the whole quote stays in view", () => {
      const quote = { x: 100, y: 200, w: 200, h: 20 }; // 200 px at 2x: from 72 it ends at 456 of 600
      const at = centreScroll(quote, 2, { x: 24, y: 0 }, view, content, [quote, ...around]);
      expect(at.left).toBe(24 + 72 * 2 - 24);
    });
    test("a quote that would run out of the view that way keeps the quote-left rule", () => {
      const quote = { x: 100, y: 200, w: 300, h: 20 }; // from 72 it ends at 656 of 600
      const at = centreScroll(quote, 2, { x: 24, y: 0 }, view, content, [quote, ...around]);
      expect(at.left).toBe(24 + 100 * 2 - 24);
    });
    test("a quote that fits the view but not with the lines around it is centred, as before", () => {
      const quote = { x: 100, y: 200, w: 260, h: 20 };
      const wide = [{ x: 20, y: 190, w: 400, h: 10 }];
      const at = centreScroll(quote, 2, { x: 24, y: 0 }, view, content, wide);
      expect(at.left).toBe(24 + 2 * 230 - 300);
    });
    test("with no lines around it, nothing changes", () => {
      const quote = { x: 100, y: 200, w: 200, h: 20 };
      expect(centreScroll(quote, 2, { x: 24, y: 0 }, view, content, []).left).toBe(24 + 2 * 200 - 300);
    });
    test("content that fits the view does not scroll sideways at all", () => {
      const quote = { x: 100, y: 200, w: 200, h: 20 };
      expect(centreScroll(quote, 1, { x: 24, y: 0 }, view, { w: 500, h: 4000 }, [quote, ...around]).left).toBe(0);
    });
  });
  test("content narrower than the view gives no horizontal scroll", () => {
    expect(centreScroll({ x: 100, y: 0, w: 10, h: 10 }, 1, { x: 24, y: 0 }, view, { w: 500, h: 4000 }).left).toBe(0);
  });
});

describe("the canvas for a page", () => {
  test("it is as sharp as the screen, at zoom times the device ratio", () => {
    expect(canvasScale(1.5, 2, 612, 792)).toBe(2);
  });
  test("a huge page at a high zoom is held under the limit of canvas pixels", () => {
    const k = canvasScale(4, 2, 612, 792);
    expect(612 * 4 * k * (792 * 4 * k)).toBeLessThanOrEqual(16_000_000 + 1);
    expect(k).toBeGreaterThan(0);
    expect(k).toBeLessThan(2);
  });
  test("a screen with no ratio is treated as 1", () => {
    expect(canvasScale(1, 0, 612, 792)).toBe(1);
  });
});

describe("zooming around a point", () => {
  // the point of the page under the pointer is in page units; after the zoom it must be under it again
  test("how far to scroll so that the anchored point sits under the pointer", () => {
    const anchor = { fx: 100, fy: 50 }; // page units
    // after the zoom, the page's top left is at (10, 20) on screen, and the pointer is at (300, 200)
    const shift = anchorShift(anchor, 2, { x: 10, y: 20 }, { x: 300, y: 200 });
    // the anchored point is now at 10 + 100 * 2 = 210 across and 20 + 50 * 2 = 120 down
    expect(shift).toEqual({ dx: 210 - 300, dy: 120 - 200 });
  });
});

describe("the zoom keeps the cited line and the lines around it whole", () => {
  const box = (y: number, w: number, h = 10 * BOX_PER_FONT) => ({ str: "x", x: 72, y, w, h });

  test("the least size is 12 px and the target stays 15 px", () => {
    expect(READ_PX_MIN).toBe(12);
    expect(READ_PX).toBe(15);
  });

  test("the lines near the quote on the page are taken with it, and a far or differently sized one is not", () => {
    const quote = [{ x: 72, y: 500, w: 300, h: 10.8 }];
    const boxes = [
      box(300, 470), // far above
      box(486, 440), // a procedure line just above
      box(500, 300), // the quote
      box(516, 470), // just below
      box(530, 480, 24), // a heading, another size
      box(900, 500), // far below
    ];
    const near = neighbourRects(boxes, quote);
    expect(near.map((r) => r.w).sort((a, b) => a - b)).toEqual([300, 440, 470]);
  });

  describe("the HP-01 warning quote: its lead-in and the quote fit together, or the zoom sits at the least size", () => {
    // measured on the BH-250 manual, page 5: the quote's boxes are 10.26 px tall at zoom 1, so 12 px is 126%
    const quote = [{ x: 101.04, y: 400, w: 325.69, h: 10.26 }];
    const around = [
      { x: 51.02, y: 380, w: 399.74, h: 10.26 },
      { x: 51.02, y: 420, w: 380, h: 10.26 },
    ];
    const lead = 426.73 - 51.02; // from the start of the lines around to the end of the quote, page units
    const floor = (READ_PX_MIN * BOX_PER_FONT) / 10.26;

    test("when the lead-in and the quote fit in a wide view at 12 px or more, the zoom lets them fit", () => {
      const view = 700;
      const z = readingZoom(quote, 0.7, view, READ_PX, around);
      expect(z).toBeGreaterThanOrEqual(floor - 1e-9);
      expect(lead * z).toBeLessThanOrEqual(view - 48 + 0.01);
    });

    test("when they cannot fit at 12 px, the zoom stops at 12 px and not below", () => {
      for (const view of [454, 469, 484]) {
        const z = readingZoom(quote, 0.7, view, READ_PX, around);
        expect(z).toBeCloseTo(floor, 9);
        expect(lead * z).toBeGreaterThan(view - 48); // so centreScroll keeps the quote-left rule
      }
    });
  });

  test("a long line next to the cited one sets the zoom, so that it is not cut on the right", () => {
    const quote = [{ x: 72, y: 500, w: 300, h: 10.8 }];
    const around = [{ x: 72, y: 516, w: 470, h: 10.8 }];
    const view = 640; // pane width in px
    const alone = readingZoom(quote, 0.5, view);
    const withNeighbours = readingZoom(quote, 0.5, view, READ_PX, around);
    expect(alone).toBeCloseTo((READ_PX * BOX_PER_FONT) / 10.8, 5); // the quote alone fits at 15 px
    expect(withNeighbours).toBeLessThan(alone);
    expect(470 * withNeighbours).toBeLessThanOrEqual(view - 48 + 0.01); // the whole line is in view
    expect(textPx(10.8, withNeighbours)).toBeGreaterThanOrEqual(12 - 0.01);
  });

  test("as large as possible: a view that holds the lines at 15 px stays at 15 px", () => {
    const quote = [{ x: 72, y: 500, w: 300, h: 10.8 }];
    const around = [{ x: 72, y: 516, w: 470, h: 10.8 }];
    expect(readingZoom(quote, 0.5, 2000, READ_PX, around)).toBeCloseTo(readingZoom(quote, 0.5), 5);
  });

  test("only when even 12 px cannot hold the line is it panned sideways", () => {
    const quote = [{ x: 72, y: 500, w: 300, h: 10.8 }];
    const around = [{ x: 72, y: 516, w: 470, h: 10.8 }];
    const zoom = readingZoom(quote, 0.5, 400, READ_PX, around);
    expect(textPx(10.8, zoom)).toBeCloseTo(12, 1);
    expect(470 * zoom).toBeGreaterThan(400 - 48);
  });
});
