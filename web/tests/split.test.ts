import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import {
  BAR,
  HIT,
  hitSpan,
  STEP,
  clampFirst,
  keyFirst,
  loadFraction,
  saveFraction,
  separatorValues,
  type Bounds,
} from "../src/lib/split";

const B: Bounds = { width: 1000, minFirst: 300, minSecond: 280, bar: 12 };
// the first pane may span 300 up to 1000 - 280 - 12 = 708 px

function fakeStorage(seed: Record<string, string> = {}) {
  const data = { ...seed };
  return {
    data,
    getItem: (k: string) => (k in data ? (data[k] as string) : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
  };
}

const throwing = {
  getItem: () => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("quota");
  },
};

describe("clamping", () => {
  test("keeps a size inside both minimums", () => {
    expect(clampFirst(500, B)).toBe(500);
    expect(clampFirst(120, B)).toBe(300);
    expect(clampFirst(900, B)).toBe(708);
  });

  test("a container too small for both minimums shares the room by their ratio", () => {
    const small: Bounds = { width: 400, minFirst: 300, minSecond: 100, bar: 12 };
    const first = clampFirst(380, small);
    expect(first).toBeGreaterThan(0);
    expect(first + small.bar).toBeLessThanOrEqual(small.width);
    expect(clampFirst(10, small)).toBe(first);
  });

  test("an unmeasured container (width 0) gives 0, never NaN", () => {
    expect(clampFirst(300, { ...B, width: 0 })).toBe(0);
  });
});

describe("keyboard", () => {
  const key = (k: string, first: number): number | null => keyFirst(k, first, B);

  test("arrow keys move 24 px and stop at the minimums", () => {
    expect(STEP).toBe(24);
    expect(key("ArrowRight", 500)).toBe(524);
    expect(key("ArrowLeft", 500)).toBe(476);
    expect(key("ArrowLeft", 310)).toBe(300);
    expect(key("ArrowRight", 700)).toBe(708);
  });

  test("Home and End go to the minimums of the first and the second pane", () => {
    expect(key("Home", 500)).toBe(300);
    expect(key("End", 500)).toBe(708);
  });

  test("other keys do nothing, and Enter is left to the caller", () => {
    expect(key("a", 500)).toBeNull();
    expect(key("Enter", 500)).toBeNull();
  });
});

describe("separator values", () => {
  test("report the position and both limits in percent", () => {
    expect(separatorValues(500, B)).toEqual({ now: 50, min: 30, max: 71 });
  });

  test("an unmeasured container reports zeros", () => {
    expect(separatorValues(0, { ...B, width: 0 })).toEqual({ now: 0, min: 0, max: 0 });
  });
});

describe("persistence", () => {
  test("a saved fraction comes back under split:<id>", () => {
    const store = fakeStorage();
    saveFraction("ask", 0.625, store);
    expect(store.data["split:ask"]).toBe("0.625");
    expect(loadFraction("ask", 0.5, store)).toBe(0.625);
  });

  test("falls back to the initial fraction when nothing, or nonsense, is stored", () => {
    expect(loadFraction("ask", 0.58, fakeStorage())).toBe(0.58);
    for (const bad of ["", "abc", "NaN", "0", "1", "-0.2", "7"]) {
      expect(loadFraction("ask", 0.58, fakeStorage({ "split:ask": bad }))).toBe(0.58);
    }
  });

  test("each id has its own key", () => {
    const store = fakeStorage();
    saveFraction("ask", 0.4, store);
    saveFraction("pipeline", 0.7, store);
    expect(loadFraction("ask", 0.5, store)).toBe(0.4);
    expect(loadFraction("pipeline", 0.5, store)).toBe(0.7);
  });

  test("storage that throws never breaks reading or writing", () => {
    expect(loadFraction("ask", 0.58, throwing)).toBe(0.58);
    expect(() => saveFraction("ask", 0.4, throwing)).not.toThrow();
  });

  test("no storage at all (a null) is the same as storage that throws", () => {
    expect(loadFraction("ask", 0.58, null)).toBe(0.58);
    expect(() => saveFraction("ask", 0.4, null)).not.toThrow();
  });
});

describe("the separator is easy to grab", () => {
  const css = readFileSync(new URL("../src/styles/split.css", import.meta.url), "utf8");

  test("the visible bar stays 24 px and the hit area is 44 px", () => {
    expect(BAR).toBe(24);
    expect(HIT).toBeGreaterThanOrEqual(44);
  });

  test("the hit area grows into the second pane only, so it never covers the scrollbar of the first", () => {
    expect(hitSpan()).toEqual({ before: 0, after: HIT - BAR });
    expect(hitSpan().before).toBe(0);
  });

  test("the stylesheet draws exactly that hit area on the bar, and the pointer shows col-resize over it", () => {
    const block = /\.split-bar::before\s*\{([^}]*)\}/.exec(css)?.[1] ?? "";
    expect(block).toMatch(new RegExp(`width:\\s*${HIT}px`));
    expect(block).toMatch(/left:\s*0/);
    expect(block).not.toMatch(/background:\s*var/); // transparent
    expect(/\.split-bar\s*\{[^}]*cursor:\s*col-resize/.test(css)).toBe(true);
    expect(/\.split-bar\s*\{[^}]*z-index/.test(css)).toBe(true);
  });
});
