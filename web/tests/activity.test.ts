import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { afterIngest, getActivity, onActivityStale, resetSources } from "../src/lib/api";
import { downtimeText, seenAt, sourceRequest } from "../src/lib/activity";

const real = globalThis.fetch;
let calls: string[];

beforeEach(() => {
  resetSources();
  calls = [];
  globalThis.fetch = (async (url: string) => {
    calls.push(String(url));
    const body = String(url).startsWith("/api/activity")
      ? { breakdowns: [], alarms: [] }
      : String(url).endsWith("/api/sources")
        ? [{ id: "DOC-aaaaaaaa", path: "logs/hp.log", doc_type: "log", bytes: 1 }]
        : {};
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = real;
});

describe("the activity of the line", () => {
  test("it is read from the activity endpoint with the limits asked for", async () => {
    expect(await getActivity()).toEqual({ breakdowns: [], alarms: [] });
    await getActivity(6, 3);
    expect(calls).toEqual(["/api/activity", "/api/activity?limit=6&alarm_limit=3"]);
  });

  test("a live drop that is done tells the listeners that the activity is stale", async () => {
    let told = 0;
    const off = onActivityStale(() => told++);
    await afterIngest();
    expect(told).toBe(1);
    off();
    await afterIngest();
    expect(told).toBe(1);
  });

  test("a listener that throws does not stop the others or the source reload", async () => {
    let told = 0;
    const a = onActivityStale(() => {
      throw new Error("x");
    });
    const b = onActivityStale(() => told++);
    const list = await afterIngest();
    expect(list).toHaveLength(1);
    expect(told).toBe(1);
    a();
    b();
  });
});

describe("words for the rail", () => {
  test("downtime in minutes and hours", () => {
    expect(downtimeText(45)).toBe("45 min");
    expect(downtimeText(60)).toBe("1 h");
    expect(downtimeText(125)).toBe("2 h 5 min");
    expect(downtimeText(null)).toBe("");
  });

  test("when an alarm was last seen, in plant time", () => {
    const now = new Date("2026-10-29T10:00:00+08:00");
    expect(seenAt("2026-09-28T09:51:40+00:00", now)).toBe("28 Sep, 17:51");
    expect(seenAt("2026-10-29T02:10:00+00:00", now)).toBe("10:10");
    expect(seenAt("2026-09-30T13:58:00+00:00", now)).toBe("30 Sep, 21:58");
    expect(seenAt(null, now)).toBe("");
  });
});

describe("the source of a row", () => {
  const sources = [{ id: "DOC-d62200db", path: "maintenance/export.csv", doc_type: "maintenance" }];

  test("a row opens its document by the id, with the path from the source list", () => {
    expect(sourceRequest("INC-2026-060", "DOC-d62200db", sources)).toEqual({
      label: "INC-2026-060",
      citations: [
        {
          doc_path: "maintenance/export.csv",
          page: null,
          chunk_id: "DOC-d62200db",
          quote: "",
        },
      ],
    });
  });

  test("a document that is not in the list still opens by its id", () => {
    const request = sourceRequest("E-217", "DOC-zzzzzzzz", sources);
    expect(request?.citations[0]).toMatchObject({ doc_path: "DOC-zzzzzzzz", quote: "" });
  });

  test("a row with no document has no source to open", () => {
    expect(sourceRequest("INC-1", null, sources)).toBeNull();
  });
});
