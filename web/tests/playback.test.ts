import { describe, expect, test } from "bun:test";
import { PACE } from "../src/lib/pace";
import { holdAfter, play, toRunEvents } from "../src/lib/playback";
import type { RunEvent } from "../src/lib/liveIngest";

const ev = (event: string, data: unknown = {}, at = 0): RunEvent => ({ event, data, at });

describe("a recording as events", () => {
  test("each event keeps its name and data, and its time becomes ms", () => {
    const out = toRunEvents([
      { event: "step", data: { node: "prepare" }, at: "2026-01-01T00:00:00Z" },
      { event: "done", data: {}, at: "2026-01-01T00:00:03Z" },
    ]);
    expect(out.map((e) => e.event)).toEqual(["step", "done"]);
    expect(out[1]!.at - out[0]!.at).toBe(3000);
    expect(out[0]!.data).toEqual({ node: "prepare" });
  });
});

describe("how long the playback holds after an event", () => {
  const f = PACE.ingest;
  test("an ordinary event holds the base time, at the pace", () => {
    expect(holdAfter(ev("step"), 1)).toBe(f.event);
    expect(holdAfter(ev("step"), 2)).toBe(f.event * 2);
    expect(holdAfter(ev("step"), 0.5)).toBe(f.event * 0.5);
  });
  test("the chunks are marked one after another before it goes on", () => {
    const chunks = [{}, {}, {}, {}, {}, {}];
    expect(holdAfter(ev("chunked", { chunks }), 1)).toBe(Math.max(f.event, chunks.length * f.chunkMark));
    expect(holdAfter(ev("chunked", { chunks: [{}] }), 1)).toBe(f.event);
  });
  test("the squares fly in and the dots appear before it goes on", () => {
    const placed = ev("placed", { passages: [{}, {}], facts: [{}, {}, {}] });
    expect(holdAfter(placed, 1)).toBe(f.fly + 1 * f.flyStagger + 3 * f.factPop);
    expect(holdAfter(ev("placed", { passages: [], facts: [] }), 1)).toBe(f.event);
    expect(holdAfter(placed, 2)).toBe((f.fly + f.flyStagger + 3 * f.factPop) * 2);
  });
  test("at pace 0 nothing waits", () => {
    expect(holdAfter(ev("placed", { passages: [{}], facts: [{}] }), 0)).toBe(0);
    expect(holdAfter(ev("step"), 0)).toBe(0);
  });
});

describe("playing a recording", () => {
  const events = [ev("a"), ev("b"), ev("chunked", { chunks: [{}, {}, {}, {}, {}, {}] }), ev("done")];

  test("each event is delivered in order, and the wait comes after it, not after the last", async () => {
    const log: string[] = [];
    await play(events, {
      deliver: (e) => log.push(e.event),
      wait: async (ms) => void log.push(`wait ${ms}`),
      factor: 1,
    });
    expect(log).toEqual([
      "a",
      `wait ${PACE.ingest.event}`,
      "b",
      `wait ${PACE.ingest.event}`,
      "chunked",
      `wait ${6 * PACE.ingest.chunkMark}`,
      "done",
    ]);
  });

  test("at pace 0 every event is delivered at once, with no wait", async () => {
    const log: string[] = [];
    await play(events, { deliver: (e) => log.push(e.event), wait: async () => void log.push("wait"), factor: 0 });
    expect(log).toEqual(["a", "b", "chunked", "done"]);
  });

  test("an abort stops it before the next event", async () => {
    const stop = new AbortController();
    const log: string[] = [];
    await play(events, {
      deliver: (e) => {
        log.push(e.event);
        if (e.event === "b") stop.abort();
      },
      wait: async () => {},
      factor: 1,
      signal: stop.signal,
    });
    expect(log).toEqual(["a", "b"]);
  });

  test("an empty recording delivers nothing", async () => {
    const log: string[] = [];
    await play([], { deliver: (e) => log.push(e.event), wait: async () => {}, factor: 1 });
    expect(log).toEqual([]);
  });
});
