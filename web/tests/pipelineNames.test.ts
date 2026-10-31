import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { basename } from "../src/lib/format";
import { sourceLabel } from "../src/lib/sourceName";
import type { SourceSummary } from "../src/lib/types";

const sources = JSON.parse(
  readFileSync(new URL("../../data/recordings/api/sources.json", import.meta.url), "utf8"),
) as SourceSummary[];

describe("the Pipeline names a file in plain words", () => {
  test("the primary name is the plain name the Ask sheet uses, never the raw file name", () => {
    expect(sources.length).toBeGreaterThan(100);
    for (const s of sources) {
      const label = sourceLabel(s);
      expect(label.name.trim().length).toBeGreaterThan(0);
      expect(label.name).not.toMatch(/_/);
      expect(label.name).not.toMatch(/\.(pdf|csv|xlsx?|txt|log)\b/i);
      expect(label.name).not.toBe(basename(s.path));
      expect(label.name).not.toMatch(/^DOC-/);
    }
  });

  test("the DOC id and the raw file name are kept, as the secondary label and the title", () => {
    const manual = sources.find((s) => s.path === "manuals/Orbis_OR6_cell_maintenance_guide.pdf")!;
    expect(sourceLabel(manual)).toEqual({
      name: "Orbis OR6 cell maintenance guide",
      id: manual.id,
      raw: "Orbis_OR6_cell_maintenance_guide.pdf",
    });
    const handover = sources.find((s) => s.path.startsWith("handovers/"))!;
    expect(sourceLabel(handover).raw).toBe(basename(handover.path));
    expect(sourceLabel(handover).id).toMatch(/^DOC-/);
  });

  test("two files in one group do not both read the same", () => {
    const names = new Map<string, number>();
    for (const s of sources) {
      const n = sourceLabel(s).name;
      names.set(n, (names.get(n) ?? 0) + 1);
    }
    // a few shifts repeat a day; but the list is not one name over and over
    expect(names.size).toBeGreaterThan(sources.length * 0.8);
  });

  test("a word is never longer than a row can hold, so that no row breaks inside a word", () => {
    for (const s of sources) {
      for (const word of sourceLabel(s).name.split(/\s+/)) expect(word.length).toBeLessThanOrEqual(16);
    }
  });
});
