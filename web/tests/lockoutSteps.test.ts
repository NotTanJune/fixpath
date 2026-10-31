import { describe, expect, test } from "bun:test";
import { bandSteps, lockoutScope, lockoutSteps, needsBand } from "../src/lib/lockoutSteps";
import { readFileSync } from "node:fs";

const HP =
  "Before you work on the hydraulic system, stop the pump, switch off the main isolator and fit your personal lock and tag. The system keeps stored hydraulic energy in the accumulator after the pump stops, so bleed the accumulator until its gauge reads 0 bar before you open any line. Lower the ram onto blocks and verify zero energy before you start. Apply lockout and tagout before starting. Verify zero energy.";

describe("lockoutSteps", () => {
  test("splits the safety text into its sentences, in order, and ends on the closing step", () => {
    const steps = lockoutSteps(HP);
    expect(steps).toEqual([
      "Before you work on the hydraulic system, stop the pump, switch off the main isolator and fit your personal lock and tag.",
      "The system keeps stored hydraulic energy in the accumulator after the pump stops, so bleed the accumulator until its gauge reads 0 bar before you open any line.",
      "Lower the ram onto blocks and verify zero energy before you start.",
      "Apply lockout and tagout before starting. Verify zero energy.",
    ]);
  });

  test("does not rewrite the text: the steps joined by a space are the sentences of the text", () => {
    expect(lockoutSteps(HP).join(" ")).toBe(HP);
  });

  test("drops an exact repeat and keeps the first one in place", () => {
    const text = "Switch off the isolator. Fit your lock. Switch off the isolator. Check the gauge. Apply lockout and tagout before starting. Verify zero energy.";
    expect(lockoutSteps(text)).toEqual([
      "Switch off the isolator.",
      "Fit your lock.",
      "Check the gauge.",
      "Apply lockout and tagout before starting. Verify zero energy.",
    ]);
  });

  test("a repeat of a closing sentence earlier in the text is dropped, so the closing is last and once", () => {
    const text = "Verify zero energy. Fit your lock. Apply lockout and tagout before starting. Verify zero energy.";
    expect(lockoutSteps(text)).toEqual([
      "Fit your lock.",
      "Apply lockout and tagout before starting. Verify zero energy.",
    ]);
  });

  test("a text that is only the closing is one step", () => {
    expect(lockoutSteps("Apply lockout and tagout before starting. Verify zero energy.")).toEqual([
      "Apply lockout and tagout before starting. Verify zero energy.",
    ]);
  });

  test("a text without the closing is split and not given one", () => {
    expect(lockoutSteps("Stop the pump. Fit your lock.")).toEqual(["Stop the pump.", "Fit your lock."]);
  });

  test("extra white space and line breaks do not make a repeat look different", () => {
    expect(lockoutSteps("Fit your lock.\n  Fit   your lock. Test it.")).toEqual(["Fit your lock.", "Test it."]);
  });

  test("an empty text has no steps", () => {
    expect(lockoutSteps("")).toEqual([]);
    expect(lockoutSteps("   ")).toEqual([]);
  });

  test("a sentence without a full stop at the end is kept", () => {
    expect(lockoutSteps("Stop the pump. Fit your lock")).toEqual(["Stop the pump.", "Fit your lock"]);
  });
});

describe("needsBand", () => {
  const card = (required: boolean, fix: boolean | null, others: boolean[] = []) => ({
    safety: { required },
    fix: fix === null ? null : { requires_loto: fix },
    other_fixes: others.map((requires_loto) => ({ requires_loto })),
  });
  test("is owed when the safety block says so", () => {
    expect(needsBand(card(true, false))).toBe(true);
  });
  test("is owed when a fix that is shown needs lockout, whatever the safety block says", () => {
    expect(needsBand(card(false, true))).toBe(true);
    expect(needsBand(card(false, false, [false, true]))).toBe(true);
  });
  test("is not owed when nothing needs it", () => {
    expect(needsBand(card(false, false, [false]))).toBe(false);
    expect(needsBand(card(false, null))).toBe(false);
  });
});

describe("bandSteps", () => {
  test("a band that is owed is never empty", () => {
    expect(bandSteps({ safety: { text: "" } })).toEqual(["Apply lockout and tagout before starting. Verify zero energy."]);
  });
  test("is the steps of the text otherwise", () => {
    expect(bandSteps({ safety: { text: "Stop the pump." } })).toEqual(["Stop the pump."]);
  });
});

describe("lockoutScope", () => {
  const answer = (n: number) => {
    const lines = readFileSync(new URL(`../../data/recordings/api/ask_${n}.jsonl`, import.meta.url), "utf8").split("\n").filter(Boolean);
    return JSON.parse(lines.map((l) => JSON.parse(l)).filter((e) => e.event === "answer").map((e) => JSON.stringify(e.data))[0]!);
  };

  test("the generic block of an unknown line says which machines it is for", () => {
    const card = answer(5); // "Line 4 robot is down": no such line
    expect(card.safety.procedure_ids).toEqual([]);
    expect(lockoutScope(card.safety)).toBe("Before work on any Line 3 machine");
    expect(bandSteps(card)).toEqual(["Apply lockout and tagout before starting. Verify zero energy."]); // the block is never hidden
  });

  test("the block of a real answer is the machine's own and has no such line", () => {
    for (const n of [1, 2, 3, 4]) {
      const { safety } = answer(n);
      if (safety.required) expect(safety.procedure_ids.length).toBeGreaterThan(0);
      expect(lockoutScope(safety)).toBeNull();
    }
  });

  test("a block with a manual page behind it is not generic, whatever its ids", () => {
    expect(lockoutScope({ procedure_ids: [], citations: [{ doc_path: "manuals/x.pdf" }] })).toBeNull();
  });
});
