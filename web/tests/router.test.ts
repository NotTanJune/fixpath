import { describe, expect, test } from "bun:test";
import { hrefFor, parseHash, tabsFor, VIEWS } from "../src/lib/router";

describe("hash router", () => {
  test("picks the view from the hash", () => {
    expect(parseHash("#/ask").view).toBe("ask");
    expect(parseHash("#/ingestion").view).toBe("ingestion");
  });

  test("the views are Ask, Ingestion and New issues", () => {
    expect(VIEWS).toEqual([
      { view: "ask", label: "Ask" },
      { view: "ingestion", label: "Ingestion" },
      { view: "issues", label: "New issues" },
    ]);
  });

  test("the top bar shows New issues in replay mode only", () => {
    expect(tabsFor(true).map((v) => v.view)).toEqual(["ask", "ingestion", "issues"]);
    expect(tabsFor(false).map((v) => v.view)).toEqual(["ask", "ingestion"]);
  });

  test("the issues hash opens New issues", () => {
    expect(parseHash("#/issues")).toEqual({ view: "issues" });
  });

  test("an old Pipeline link opens the same file in Ingestion", () => {
    expect(parseHash("#/pipeline")).toEqual({ view: "ingestion" });
    expect(parseHash("#/pipeline/DOC-55c94b21")).toEqual({
      view: "ingestion",
      arg: "DOC-55c94b21",
    });
    expect(parseHash("#/pipeline/").view).toBe("ingestion");
  });

  test("an old Evals link opens Ask", () => {
    expect(parseHash("#/evals")).toEqual({ view: "ask" });
    expect(parseHash("#/evals/").view).toBe("ask");
    expect(parseHash("#/evals/whatever")).toEqual({ view: "ask" });
  });

  test("an empty or unknown hash is the Ask view", () => {
    expect(parseHash("")).toEqual({ view: "ask" });
    expect(parseHash("#").view).toBe("ask");
    expect(parseHash("#/").view).toBe("ask");
    expect(parseHash("#/nowhere").view).toBe("ask");
  });

  test("keeps the part after the view as the argument", () => {
    expect(parseHash("#/ingestion/DOC-55c94b21")).toEqual({
      view: "ingestion",
      arg: "DOC-55c94b21",
    });
    expect(parseHash("#/ask").arg).toBeUndefined();
  });

  test("a view with a trailing slash still matches", () => {
    expect(parseHash("#/ingestion/").view).toBe("ingestion");
  });

  test("builds the href of a view, with an optional argument", () => {
    expect(hrefFor("ask")).toBe("#/ask");
    expect(hrefFor("ingestion")).toBe("#/ingestion");
    expect(hrefFor("ingestion", "DOC-1")).toBe("#/ingestion/DOC-1");
  });
});
