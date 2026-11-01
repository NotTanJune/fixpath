import { describe, expect, test } from "bun:test";
import { newBreakdown } from "../src/lib/writeBack";

const input = { machine: "HP-01", cause: "worn seal", part: "SEAL-KIT-50", note: "Replaced on shift B." };

describe("newBreakdown", () => {
  test("builds the id and the fields", () => {
    expect(newBreakdown(input, 1)).toEqual({
      id: "INC-TECH-001",
      label: "INC-TECH-001 (technician, not reviewed)",
      machine: "HP-01",
      cause: "worn seal",
      part: "SEAL-KIT-50",
      note: "Replaced on shift B.",
      source: "Technician report",
    });
  });

  test("the ids count up", () => {
    expect(newBreakdown(input, 2).id).toBe("INC-TECH-002");
    expect(newBreakdown(input, 12).id).toBe("INC-TECH-012");
  });
});
