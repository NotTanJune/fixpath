import { describe, expect, test } from "bun:test";
import { MAX_PACE, MIN_PACE, PACE, paceFactor, paced, type PaceEnv } from "../src/lib/pace";

function store(seed: Record<string, string> = {}) {
  return {
    getItem: (k: string) => seed[k] ?? null,
    setItem: () => {},
  };
}

const env = (over: PaceEnv = {}): PaceEnv => ({ search: "", hash: "", storage: store(), reduced: false, ...over });

describe("the pace table", () => {
  test("every duration is a positive number of ms", () => {
    const all = [...Object.values(PACE.stage), PACE.nodeSnap, PACE.edgeDraw, PACE.narrationFade];
    expect(all.length).toBeGreaterThanOrEqual(9);
    for (const ms of all) {
      expect(Number.isFinite(ms)).toBe(true);
      expect(ms).toBeGreaterThan(0);
    }
  });

  test("names the six stages of the reveal and starts at the agreed speed", () => {
    expect(Object.keys(PACE.stage)).toEqual([
      "question",
      "machineAlarm",
      "rootCause",
      "fix",
      "partsBin",
      "claims",
    ]);
    for (const ms of Object.values(PACE.stage)) expect(ms).toBe(1400);
    expect(PACE.nodeSnap).toBe(500);
    expect(PACE.edgeDraw).toBe(700);
    expect(PACE.narrationFade).toBe(300);
  });
});

describe("paceFactor", () => {
  test("is 1 when nothing is set", () => {
    expect(paceFactor(env())).toBe(1);
  });

  test("reads ?pace= from the URL", () => {
    expect(paceFactor(env({ search: "?pace=0.5" }))).toBe(0.5);
    expect(paceFactor(env({ search: "?x=1&pace=2" }))).toBe(2);
  });

  test("reads ?pace= after the route in the hash too", () => {
    expect(paceFactor(env({ hash: "#/?pace=3" }))).toBe(3);
  });

  test("a pace of exactly 0 switches the reveal off, in the URL or in storage", () => {
    expect(paceFactor(env({ search: "?pace=0" }))).toBe(0);
    expect(paceFactor(env({ hash: "#/?pace=0" }))).toBe(0);
    expect(paceFactor(env({ storage: store({ pace: "0" }) }))).toBe(0);
    expect(paced(1400, env({ search: "?pace=0" }))).toBe(0);
  });

  test("clamps to 0.1 and 5", () => {
    expect(MIN_PACE).toBe(0.1);
    expect(MAX_PACE).toBe(5);
    expect(paceFactor(env({ search: "?pace=0.01" }))).toBe(0.1);
    expect(paceFactor(env({ search: "?pace=0" }))).toBe(0);
    expect(paceFactor(env({ search: "?pace=40" }))).toBe(5);
  });

  test("falls back to localStorage pace when the URL has none", () => {
    expect(paceFactor(env({ storage: store({ pace: "1.5" }) }))).toBe(1.5);
    expect(paceFactor(env({ storage: store({ pace: "99" }) }))).toBe(5);
  });

  test("the URL wins over localStorage", () => {
    expect(paceFactor(env({ search: "?pace=0.25", storage: store({ pace: "3" }) }))).toBe(0.25);
  });

  test("ignores a value that is not a number, and goes on to the next source", () => {
    expect(paceFactor(env({ search: "?pace=fast", storage: store({ pace: "2" }) }))).toBe(2);
    expect(paceFactor(env({ search: "?pace=", storage: store({ pace: "abc" }) }))).toBe(1);
    expect(paceFactor(env({ search: "?pace=-2" }))).toBe(0.1);
  });

  test("a storage that throws, or none at all, is the same as an empty one", () => {
    const throwing = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {},
    };
    expect(paceFactor(env({ storage: throwing }))).toBe(1);
    expect(paceFactor(env({ storage: null }))).toBe(1);
  });

  test("reduced motion gives 0 whatever else is set", () => {
    expect(paceFactor(env({ reduced: true }))).toBe(0);
    expect(paceFactor(env({ reduced: true, search: "?pace=3", storage: store({ pace: "2" }) }))).toBe(0);
  });
});

describe("paced", () => {
  test("scales a duration by the factor", () => {
    expect(paced(1400, env())).toBe(1400);
    expect(paced(1400, env({ search: "?pace=0.5" }))).toBe(700);
    expect(paced(500, env({ search: "?pace=2" }))).toBe(1000);
  });

  test("is 0 under reduced motion, so that the final state shows at once", () => {
    expect(paced(1400, env({ reduced: true }))).toBe(0);
  });
});
