import { describe, expect, test } from "bun:test";
import { Follow, followsStage, followTo, STAGE_SECTIONS, union } from "../src/lib/follow";
import { STAGES } from "../src/lib/reveal";

const pane = { top: 100, bottom: 800 }; // a pane 700 px high, 100 px down the page

describe("which region follows", () => {
  test("the region that is already in view does not scroll the pane", () => {
    expect(followTo(pane, { top: 300, bottom: 600 }, 0)).toBeNull();
    expect(followTo(pane, { top: 100, bottom: 800 }, 40)).toBeNull();
  });

  test("a region below the fold is brought to the top of the pane, with room above", () => {
    // 900 px down the page and the pane scrolled 50: it needs 900 - 100 - 16 more
    expect(followTo(pane, { top: 900, bottom: 1100 }, 50, 16)).toBe(50 + 784);
  });

  test("a region that is only partly in view is brought up too", () => {
    expect(followTo(pane, { top: 700, bottom: 900 }, 0, 16)).toBe(584);
  });

  test("a region above the pane scrolls back up and never below zero", () => {
    expect(followTo(pane, { top: 20, bottom: 90 }, 200, 16)).toBe(200 - 80 - 16);
    expect(followTo(pane, { top: 20, bottom: 90 }, 10, 16)).toBe(0);
  });

  test("a region taller than the pane shows its start", () => {
    expect(followTo(pane, { top: 500, bottom: 1700 }, 0, 16)).toBe(384);
  });

  test("the box of several sections spans all of them", () => {
    expect(union([])).toBeNull();
    expect(union([{ top: 400, bottom: 700 }, { top: 720, bottom: 1000 }])).toEqual({ top: 400, bottom: 1000 });
  });
});

describe("which stages follow", () => {
  test("a paced reveal follows one stage at a time", () => {
    expect(followsStage(2, 3)).toBe(true);
    expect(followsStage(0, 1)).toBe(true);
  });

  test("a jump over several stages, or no change, does not follow", () => {
    expect(followsStage(0, 7)).toBe(false);
    expect(followsStage(3, 3)).toBe(false);
    expect(followsStage(4, 2)).toBe(false);
  });

  test("only the stages that bring a section have a selector, and every selector is real", () => {
    for (const stage of Object.keys(STAGE_SECTIONS)) expect([...STAGES]).toContain(stage as (typeof STAGES)[number]);
    expect(Object.keys(STAGE_SECTIONS)).toEqual(["rootCause", "fix", "partsBin", "claims", "done"]);
  });
});

describe("the reader takes over", () => {
  test("after the reader scrolls, the reveal is not followed until the next question", () => {
    const f = new Follow();
    expect(f.wants(1, 2, 3)).toBe(true);
    f.stop();
    expect(f.wants(1, 3, 4)).toBe(false);
    expect(f.wants(1, 4, 5)).toBe(false);
    expect(f.wants(2, 0, 1)).toBe(true); // a new run follows again
    expect(f.wants(2, 1, 2)).toBe(true);
  });
});
