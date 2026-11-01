import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { dueCount, dueLine, itemFor } from "../src/lib/upkeep";
import type { Upkeep, UpkeepItem } from "../src/lib/types";

const recording: Upkeep = JSON.parse(
  readFileSync(new URL("../../data/recordings/api/upkeep.json", import.meta.url), "utf8"),
);

const item = (over: Partial<UpkeepItem>): UpkeepItem => ({
  machine: "HP-01",
  machine_label: "Hydraulic press",
  cause: "FM-X",
  cause_label: "x wear",
  episodes: 4,
  last: "2026-08-31",
  status: "later",
  gap_days: 17,
  due: "2026-09-17",
  breakdowns: [],
  ...over,
});

describe("upkeep helpers", () => {
  test("counts the items that are overdue or due soon", () => {
    const n = recording.items.filter((i) => i.status === "overdue" || i.status === "due_soon").length;
    expect(n).toBeGreaterThan(0);
    expect(dueCount(recording)).toBe(n);
  });

  test("writes the due line for each status", () => {
    expect(dueLine(item({ status: "overdue" }))).toBe(
      "Usually every 17 days. Last on 31 Aug. Past due since 17 Sep.",
    );
    expect(dueLine(item({ status: "due_soon", gap_days: 35, due: "2026-10-05" }))).toBe(
      "Usually every 35 days. Last on 31 Aug. Due around 5 Oct.",
    );
    expect(dueLine(item({ status: "later", gap_days: 39, last: "2026-09-28", due: "2026-11-06" }))).toBe(
      "Usually every 39 days. Last on 28 Sep. Due around 6 Nov.",
    );
    expect(dueLine(item({ status: "not_enough_history", gap_days: null, due: null }))).toBe(
      "Not enough history to predict.",
    );
  });

  test("finds the first overdue or due soon item of a machine", () => {
    const got = itemFor(recording, "HP-01");
    expect(got?.machine).toBe("HP-01");
    expect(["overdue", "due_soon"]).toContain(got?.status ?? "");
    expect(itemFor(recording, "RW-01")).toBeNull();
    expect(itemFor(recording, "NOPE")).toBeNull();
  });
});
