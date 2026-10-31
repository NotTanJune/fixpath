import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import {
  afterIngest,
  resetSources,
  sourceDoc,
  sourceIdOfPath,
  sourcesOnce,
  mapOnce,
} from "../src/lib/api";

const real = globalThis.fetch;
let listed: { id: string; path: string }[];
let calls: string[];

function row(id: string, path: string) {
  return { id, path, doc_type: "handover", title: id };
}

beforeEach(() => {
  resetSources();
  calls = [];
  listed = [row("D-1", "handovers/a.txt")];
  globalThis.fetch = (async (url: string) => {
    calls.push(String(url));
    const body = String(url).endsWith("/api/sources")
      ? listed
      : { id: String(url).split("/").pop(), path: "p", text: `text ${calls.length}`, spans: [] };
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = real;
});

const listCalls = () => calls.filter((c) => c.endsWith("/api/sources")).length;

describe("the source list after a live drop", () => {
  test("the list is fetched once and kept", async () => {
    await sourcesOnce();
    await sourcesOnce();
    expect(listCalls()).toBe(1);
  });

  test("a file dropped later is not in the kept list", async () => {
    await sourcesOnce();
    listed = [...listed, row("D-2", "handovers/new.txt")];
    expect(await sourceIdOfPath("handovers/a.txt")).toBe("D-1");
    expect((await sourcesOnce()).map((s) => s.id)).toEqual(["D-1"]);
  });

  test("a lookup miss refetches the list once and finds the new file", async () => {
    await sourcesOnce();
    listed = [...listed, row("D-2", "handovers/new.txt")];
    expect(await sourceIdOfPath("handovers/new.txt")).toBe("D-2");
    expect(listCalls()).toBe(2);
  });

  test("a path that is not there stays a miss after one refetch", async () => {
    await sourcesOnce();
    expect(await sourceIdOfPath("handovers/none.txt")).toBeNull();
    expect(listCalls()).toBe(2);
  });

  test("afterIngest drops the cached list and documents and loads the list again", async () => {
    await sourcesOnce();
    const first = await sourceDoc("D-1");
    expect(await sourceDoc("D-1")).toBe(first);
    listed = [...listed, row("D-2", "handovers/new.txt")];
    const fresh = await afterIngest();
    expect(fresh.map((s) => s.id)).toEqual(["D-1", "D-2"]);
    expect(listCalls()).toBe(2);
    expect(await sourceDoc("D-1")).not.toBe(first);
    expect((await sourcesOnce()).map((s) => s.id)).toEqual(["D-1", "D-2"]);
    expect(listCalls()).toBe(2);
  });

  test("a document fetched before the reset is not stored after it", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    globalThis.fetch = (async () => {
      await gate;
      return new Response(JSON.stringify({ id: "D-1", path: "p", text: "old", spans: [] }));
    }) as unknown as typeof fetch;
    const pending = sourceDoc("D-1");
    resetSources();
    release();
    await pending;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ id: "D-1", path: "p", text: "new", spans: [] }))) as unknown as typeof fetch;
    expect((await sourceDoc("D-1")).text).toBe("new");
  });

  test("afterIngest also drops the kept fact map, which the load put new points on", async () => {
    let served = 0;
    globalThis.fetch = (async (url: string) => {
      if (String(url).endsWith("/api/map")) {
        served += 1;
        return new Response(JSON.stringify({ points: [], served }));
      }
      return new Response(JSON.stringify([]));
    }) as unknown as typeof fetch;
    await mapOnce();
    await mapOnce();
    expect(served).toBe(1);
    await afterIngest();
    expect(((await mapOnce()) as unknown as { served: number }).served).toBe(2);
  });
});
