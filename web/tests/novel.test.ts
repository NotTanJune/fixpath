import { describe, expect, test } from "bun:test";
import { NOVEL, novelCase } from "../src/lib/novel";

describe("novel cases", () => {
  test("there are four", () => {
    expect(NOVEL).toHaveLength(4);
  });

  test("a question matches ignoring case and outer spaces", () => {
    const first = NOVEL[0]!;
    expect(novelCase(`  ${first.question.toUpperCase()}  `)).toBe(first);
    expect(novelCase(first.question)).toBe(first);
  });

  test("a known suggestion is not a novel case", () => {
    expect(novelCase("HP-01 stopped with E-217, oil under the press")).toBeNull();
  });

  test("every case is low confidence and fully filled in", () => {
    for (const c of NOVEL) {
      expect(c.confidence).toBeLessThan(0.4);
      expect(c.record.fixSteps.length).toBeGreaterThan(0);
      expect(c.record.parts.length).toBeGreaterThan(0);
      expect(c.adds.filter((a) => a.type === "Incident")).toHaveLength(1);
    }
  });

  test("the conveyor case has no nearest item", () => {
    expect(NOVEL[3]!.nearest).toEqual([]);
    expect(NOVEL[3]!.record.machine).toBe("");
  });
});
