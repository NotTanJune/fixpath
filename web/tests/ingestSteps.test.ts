import { describe, expect, test } from "bun:test";
import { queueLine, refusalText } from "../src/lib/ingestSteps";

describe("the line of files waiting", () => {
  test("it says how many are ahead", () => {
    expect(queueLine(2)).toBe("Waiting for your turn. 2 files ahead.");
  });

  test("one file ahead is singular and none ahead is plain", () => {
    expect(queueLine(1)).toBe("Waiting for your turn. 1 file ahead.");
    expect(queueLine(0)).toBe("Waiting for your turn.");
  });
});

describe("the reason a drop shows", () => {
  test("the replay-mode refusal of the server is said without the word keys as a setting", () => {
    expect(
      refusalText(
        "Ingest needs keys. This server is in replay mode, which has no OpenAI key, so a new file cannot be extracted; add keys to ingest files.",
      ),
    ).toBe("This server runs without keys, so it cannot read new files.");
  });

  test("any other reason is the server's own words", () => {
    expect(refusalText("Too many files are waiting; try again in a moment.")).toBe(
      "Too many files are waiting; try again in a moment.",
    );
  });
});
