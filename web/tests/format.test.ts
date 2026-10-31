import { describe, expect, test } from "bun:test";
import {
  claimMark,
  composeAsk,
  docIdOfChunk,
  errorText,
  edgeParts,
  factSentence,
  failureName,
  plainLabel,
  shortDate,
  sourceLabel,
} from "../src/lib/format";

describe("format helpers", () => {
  test("adds a pick to the question as a new sentence", () => {
    expect(composeAsk("Line 4 robot is down", "robot workcell (RW-01)")).toBe(
      "Line 4 robot is down. Robot workcell (RW-01).",
    );
    expect(composeAsk("HP-01 stopped. ", "main cylinder seal wear")).toBe(
      "HP-01 stopped. Main cylinder seal wear.",
    );
  });

  test("keeps the new question inside 500 characters", () => {
    const out = composeAsk("x".repeat(600), "pick");
    expect(out.length).toBeLessThanOrEqual(500);
    expect(out.endsWith(". Pick.")).toBe(true);
  });

  test("turns a failure mode id into words", () => {
    expect(failureName("FM-MAIN-CYLINDER-SEAL-WEAR")).toBe("Main cylinder seal wear");
  });

  test("finds the document of a chunk id", () => {
    expect(docIdOfChunk("DOC-5926a330-3-2")).toBe("DOC-5926a330");
    expect(docIdOfChunk(null)).toBeNull();
  });

  test("writes a short date", () => {
    expect(shortDate("2026-09-28", 2026)).toBe("28 Sep");
    expect(shortDate("2025-09-28", 2026)).toBe("28 Sep 2025");
  });

  test("names a cited page briefly", () => {
    expect(sourceLabel("manuals/BH-250 service manual.pdf", 3)).toBe("BH-250 manual p.3");
    expect(sourceLabel("manuals/Orbis_OR6_cell_maintenance_guide.pdf", 2)).toBe(
      "Orbis OR6 manual p.2",
    );
    expect(sourceLabel("inventory/spares_2026-10.xlsx", null)).toBe("spares 2026-10");
  });

  test("reads an edge fact as a sentence, never as A|REL|B", () => {
    expect(factSentence("E-217|INDICATES|FM-MAIN-CYLINDER-SEAL-WEAR")).toBe(
      "E-217 points to Main cylinder seal wear",
    );
    expect(factSentence("INC-2026-040|OCCURRED_ON|RW-01")).toBe("INC-2026-040 happened on RW-01");
    expect(factSentence("HP-01")).toBe("HP-01");
    expect(factSentence("INC-2026-040|OCCURRED_ON|RW-01")).not.toContain("|");
    expect(edgeParts("A|REL|B")).toEqual({ from: "A", rel: "REL", to: "B" });
    expect(edgeParts("HP-01")).toBeNull();
  });

  test("puts failure mode names in a label in place of their ids", () => {
    expect(plainLabel("E-217 usually means FM-MAIN-CYLINDER-SEAL-WEAR")).toBe(
      "E-217 usually means main cylinder seal wear",
    );
    expect(plainLabel("INC-2026-040 used GRP-FNG-12")).toBe("INC-2026-040 used GRP-FNG-12");
  });

  test("the cache-miss error does not tell a live user to add keys", () => {
    const miss =
      "This question is not in the replay cache; ask one of the suggested questions or add keys";
    expect(errorText(miss, true)).toBe(miss);
    expect(errorText(miss, false)).not.toMatch(/keys|replay/i);
    expect(errorText("Something went wrong while answering. Try again.", false)).toBe(
      "Something went wrong while answering. Try again.",
    );
  });
});

describe("claimMark", () => {
  const claim = { id: "c", text: "t", citation_ids: [], reason: null };
  test("the list decides the label, so a kept partly claim reads Kept", () => {
    const partly = { ...claim, verdict: "partly" as const, p: 0.42, support: 0.89 };
    expect(claimMark(partly, true)).toEqual({ label: "Kept", score: 0.89 });
    expect(claimMark(partly, false)).toEqual({ label: "Unverified", score: 0.89 });
  });
  test("an older claim with no support mass shows its p", () => {
    const old = { ...claim, verdict: "supported" as const, p: 0.9 };
    expect(claimMark(old, true)).toEqual({ label: "Kept", score: 0.9 });
  });
});

