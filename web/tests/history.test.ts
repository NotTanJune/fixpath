import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import {
  HISTORY_KEY,
  MAX_RUNS,
  clearRuns,
  loadRuns,
  restoredState,
  saveRun,
  savedAt,
  type SavedEvent,
} from "../src/lib/history";
import { askReducer, initialAsk, type AskState } from "../src/lib/askReducer";

/** A Storage in a Map; `fail` makes the named methods throw, as blocked site data does. */
function memory(fail: ("getItem" | "setItem" | "removeItem")[] = [], limit = Infinity) {
  const map = new Map<string, string>();
  return {
    map,
    getItem(key: string) {
      if (fail.includes("getItem")) throw new Error("blocked");
      return map.get(key) ?? null;
    },
    setItem(key: string, value: string) {
      if (fail.includes("setItem") || value.length > limit) throw new Error("blocked or full");
      map.set(key, value);
    },
    removeItem(key: string) {
      if (fail.includes("removeItem")) throw new Error("blocked");
      map.delete(key);
    },
  };
}

const events = (tag: string): SavedEvent[] => [
  { name: "node_start", data: { node: "triage", tag } },
  { name: "done", data: { cost_usd: 0, calls: 0, cache_hits: 0 } },
];

function transcript(n: number): SavedEvent[] {
  const path = new URL(`../../data/recordings/api/ask_${n}.jsonl`, import.meta.url);
  return readFileSync(path, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => {
      const e = JSON.parse(line);
      return { name: e.event, data: e.data };
    });
}

describe("saving runs", () => {
  test("a saved run is read back with its question, time and events", () => {
    const store = memory();
    saveRun("HP-01 down", events("a"), "2026-10-29T09:30:00.000Z", store);
    expect(loadRuns(store)).toEqual([
      { question: "HP-01 down", at: "2026-10-29T09:30:00.000Z", events: events("a") },
    ]);
    expect(store.map.has(HISTORY_KEY)).toBe(true);
  });

  test("the newest run is first", () => {
    const store = memory();
    saveRun("one", events("1"), "2026-10-29T09:00:00.000Z", store);
    saveRun("two", events("2"), "2026-10-29T09:05:00.000Z", store);
    expect(loadRuns(store).map((r) => r.question)).toEqual(["two", "one"]);
  });

  test("at most 12 runs are kept, the oldest go first", () => {
    const store = memory();
    for (let i = 1; i <= MAX_RUNS + 3; i++) saveRun(`q${i}`, events(`${i}`), "2026-10-29T09:00:00Z", store);
    const runs = loadRuns(store);
    expect(runs).toHaveLength(MAX_RUNS);
    expect(runs[0]?.question).toBe(`q${MAX_RUNS + 3}`);
    expect(runs.at(-1)?.question).toBe("q4");
  });

  test("the same question moves to the top with its new events and time", () => {
    const store = memory();
    saveRun("HP-01 down", events("old"), "2026-10-29T09:00:00Z", store);
    saveRun("other", events("x"), "2026-10-29T09:01:00Z", store);
    const after = saveRun("  hp-01   DOWN ", events("new"), "2026-10-29T09:02:00Z", store);
    expect(after.map((r) => r.question)).toEqual(["  hp-01   DOWN ".trim(), "other"]);
    expect(after[0]?.events).toEqual(events("new"));
    expect(after[0]?.at).toBe("2026-10-29T09:02:00Z");
    expect(loadRuns(store)).toEqual(after);
  });

  test("clearing removes every run", () => {
    const store = memory();
    saveRun("a", events("a"), "2026-10-29T09:00:00Z", store);
    clearRuns(store);
    expect(loadRuns(store)).toEqual([]);
    expect(store.map.has(HISTORY_KEY)).toBe(false);
  });

  test("a store too small for 12 runs keeps as many of the newest as fit", () => {
    const store = memory([], 700);
    for (let i = 1; i <= 5; i++) saveRun(`q${i}`, events(`${i}`), "2026-10-29T09:00:00Z", store);
    const runs = loadRuns(store);
    expect(runs.length).toBeGreaterThan(0);
    expect(runs.length).toBeLessThan(5);
    expect(runs[0]?.question).toBe("q5");
  });
});

describe("storage that does not work", () => {
  test("reading that throws gives no runs", () => {
    expect(loadRuns(memory(["getItem"]))).toEqual([]);
  });

  test("saving that throws still returns the run for this visit and does not throw", () => {
    const store = memory(["setItem"]);
    const runs = saveRun("HP-01 down", events("a"), "2026-10-29T09:00:00Z", store);
    expect(runs.map((r) => r.question)).toEqual(["HP-01 down"]);
    expect(loadRuns(store)).toEqual([]);
  });

  test("clearing that throws does not throw", () => {
    expect(() => clearRuns(memory(["removeItem", "setItem"]))).not.toThrow();
  });

  test("no storage at all works as an empty one", () => {
    expect(loadRuns(null)).toEqual([]);
    expect(saveRun("q", events("a"), "2026-10-29T09:00:00Z", null)).toHaveLength(1);
    expect(() => clearRuns(null)).not.toThrow();
  });
});

describe("a stored value that is not usable", () => {
  test.each([
    ["not json", "{oops"],
    ["not a list", '{"question":"a"}'],
    ["null", "null"],
    ["a number", "42"],
  ])("%s gives no runs", (_name, raw) => {
    const store = memory();
    store.map.set(HISTORY_KEY, raw);
    expect(loadRuns(store)).toEqual([]);
  });

  test("a bad entry is dropped and the good ones stay", () => {
    const store = memory();
    const good = { question: "q", at: "2026-10-29T09:00:00Z", events: events("a") };
    store.map.set(
      HISTORY_KEY,
      JSON.stringify([
        { question: 3, at: "x", events: [] },
        { question: "no events", at: "x" },
        { question: "bad event", at: "x", events: [{ data: 1 }] },
        good,
      ]),
    );
    expect(loadRuns(store)).toEqual([good]);
  });

  test("a stored list longer than the cap is cut to it", () => {
    const store = memory();
    const many = Array.from({ length: 20 }, (_, i) => ({
      question: `q${i}`,
      at: "2026-10-29T09:00:00Z",
      events: events("a"),
    }));
    store.map.set(HISTORY_KEY, JSON.stringify(many));
    expect(loadRuns(store)).toHaveLength(MAX_RUNS);
  });

  test("saving over a corrupt value starts a clean list", () => {
    const store = memory();
    store.map.set(HISTORY_KEY, "{oops");
    const runs = saveRun("q", events("a"), "2026-10-29T09:00:00Z", store);
    expect(runs).toHaveLength(1);
    expect(loadRuns(store)).toHaveLength(1);
  });
});

describe("reopening a saved run", () => {
  test("feeding the saved events through the reducer gives the state of the live run", () => {
    const saved = transcript(2);
    let live = askReducer(initialAsk, { type: "start", question: "q" });
    for (const e of saved) live = askReducer(live, { type: "event", name: e.name, data: e.data });
    const store = memory();
    saveRun("q", saved, "2026-10-29T09:00:00Z", store);
    const run = loadRuns(store)[0]!;
    const back = restoredState(run);
    expect({ ...back, saved: null } as AskState).toEqual(live);
    expect(back.saved).toEqual({ at: "2026-10-29T09:00:00Z" });
    expect(back.phase).toBe("done");
  });

  test("the events are read in the order they were saved", () => {
    const run = {
      question: "q",
      at: "2026-10-29T09:00:00Z",
      events: [
        { name: "node_end", data: { node: "triage", seconds: 1 } },
        { name: "node_end", data: { node: "synthesize", seconds: 2 } },
      ],
    };
    expect(restoredState(run).nodes.map((n) => n.node)).toEqual(["triage", "synthesize"]);
  });

  test("a run saved without its done event is shown as finished, not running", () => {
    const saved = transcript(2).filter((e) => e.name !== "done");
    const back = restoredState({ question: "q", at: "2026-10-29T09:00:00Z", events: saved });
    expect(back.card).not.toBeNull();
    expect(back.phase).toBe("done");
  });

  test("starting a new question clears the saved mark", () => {
    const back = restoredState({ question: "q", at: "2026-10-29T09:00:00Z", events: transcript(2) });
    expect(askReducer(back, { type: "start", question: "next" }).saved).toBeNull();
  });
});

describe("the time of a saved answer", () => {
  const now = new Date("2026-10-29T10:00:00+08:00");
  test("today shows the time in Singapore", () => {
    expect(savedAt("2026-10-29T06:32:00.000Z", now)).toBe("14:32");
  });
  test("another day shows the day and the time", () => {
    expect(savedAt("2026-10-27T06:32:00.000Z", now)).toBe("27 Oct, 14:32");
  });
  test("a value that is not a time shows nothing", () => {
    expect(savedAt("nonsense", now)).toBe("");
  });
});
