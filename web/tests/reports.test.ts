import { describe, expect, test } from "bun:test";
import { nextStage, reportId, STAGES } from "../src/lib/reports";

describe("report stages", () => {
  test("there are four, in order", () => {
    expect(STAGES.map((s) => s.stage)).toEqual(["captured", "checked", "matched", "merged"]);
  });

  test("nextStage walks them and then ends", () => {
    expect(nextStage("captured")).toBe("checked");
    expect(nextStage("checked")).toBe("matched");
    expect(nextStage("matched")).toBe("merged");
    expect(nextStage("merged")).toBeNull();
  });

  test("ids count up", () => {
    expect(reportId(1)).toBe("NEW-001");
    expect(reportId(12)).toBe("NEW-012");
  });
});
