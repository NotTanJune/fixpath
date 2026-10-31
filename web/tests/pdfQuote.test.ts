import { describe, expect, test } from "bun:test";
import { matchQuote, pageOfOffset, pdfMarks, segmentRects } from "../src/lib/pdfQuote";
import type { Span } from "../src/lib/types";

const items = (...s: string[]) => s.map((str) => ({ str }));

describe("a quote found among the text items of a page", () => {
  test("inside one item", () => {
    expect(matchQuote(items("Plant ref: PROC-HP-04", "Time: 180 min"), "PROC-HP-04")).toEqual([
      { item: 0, from: 11, to: 21 },
    ]);
  });

  test("across items, where the quote has a line break and the items none", () => {
    const found = matchQuote(
      items("Before you work on the", " hydraulic system,", " stop the pump"),
      "work on the\nhydraulic system, stop",
    );
    expect(found).toEqual([
      { item: 0, from: 11, to: 22 },
      { item: 1, from: 1, to: 18 },
      { item: 2, from: 1, to: 5 },
    ]);
  });

  test("whitespace differs everywhere: doubled, tabs, spaces split into items of their own", () => {
    const found = matchQuote(items("Lockout", " ", "required:", "  yes"), "Lockout   required:\tyes");
    expect(found?.map((s) => s.item)).toEqual([0, 2, 3]);
    expect(found?.[2]).toEqual({ item: 3, from: 2, to: 5 });
  });

  test("case is kept first and ignored only when the exact text is not there", () => {
    expect(matchQuote(items("WARNING: Apply lockout"), "warning: apply lockout")).toEqual([
      { item: 0, from: 0, to: 22 },
    ]);
  });

  test("none when it is not on the page, or the quote is empty", () => {
    expect(matchQuote(items("alpha", "beta"), "gamma")).toBeNull();
    expect(matchQuote(items("alpha"), "   ")).toBeNull();
    expect(matchQuote([], "alpha")).toBeNull();
  });

  test("the first of several matches", () => {
    expect(matchQuote(items("a b", "a b"), "a b")).toEqual([{ item: 0, from: 0, to: 3 }]);
  });
});

describe("the rectangle of a stretch of an item", () => {
  test("a share of the item's box, by characters", () => {
    expect(segmentRects([{ item: 0, from: 5, to: 10 }], [{ str: "0123456789", x: 100, y: 20, w: 200, h: 12 }])).toEqual([
      { x: 200, y: 20, w: 100, h: 12 },
    ]);
  });
  test("an item with no text has no rectangle", () => {
    expect(segmentRects([{ item: 0, from: 0, to: 0 }], [{ str: "", x: 0, y: 0, w: 10, h: 5 }])).toEqual([]);
  });
});

describe("pages", () => {
  const pages = [
    { page: 1, text: "", start: 0 },
    { page: 2, text: "", start: 100 },
    { page: 3, text: "", start: 250 },
  ];
  test("the page of an offset in the text of the document", () => {
    expect([0, 99, 100, 249, 250, 900].map((o) => pageOfOffset(pages, o))).toEqual([1, 1, 2, 2, 3, 3]);
    expect(pageOfOffset([], 5)).toBeNull();
  });
});

function span(page: number | null, quote: string, fact_id: string, label = fact_id): Span {
  return { quote, start: 0, end: 1, page, fact_kind: "node", fact_id, type: "X", label, extractor: "t", confidence: 1 };
}

describe("the marks to find on each page", () => {
  const text = "page one text\n\nWARNING: Apply lockout and tagout\nmore";
  const doc = {
    text,
    spans: [
      span(1, "page one", "A-1", "Alpha"),
      span(2, "Lockout required: yes", "PROC-1", "Procedure one"),
      span(2, "Lockout required: yes", "PROC-1|REL|X", "PROC-1 relates to X"),
      span(null, "no page", "Z-1"),
    ],
    pages: [
      { page: 1, text: "page one text", start: 0 },
      { page: 2, text: "WARNING: Apply lockout and tagout\nmore", start: 15 },
    ],
  };

  test("the cited quote is strong on the cited page; every other fact is faint on its page", () => {
    const marks = pdfMarks(doc, { quotes: ["WARNING: Apply lockout and tagout"], fact: null, page: 2 });
    expect(marks.filter((m) => m.strong).map((m) => [m.page, m.quote])).toEqual([[2, "WARNING: Apply lockout and tagout"]]);
    const faint = marks.filter((m) => !m.strong);
    expect(faint.map((m) => m.page).sort()).toEqual([1, 2]);
    expect(faint.find((m) => m.page === 2)?.facts.map((f) => f.id)).toEqual(["PROC-1", "PROC-1|REL|X"]);
  });

  test("a citation with no page takes the page of its quote in the text", () => {
    const marks = pdfMarks(doc, { quotes: ["WARNING: Apply lockout and tagout"], fact: null, page: null });
    expect(marks.find((m) => m.strong)?.page).toBe(2);
  });

  test("a faint fact with the cited quote joins the strong mark", () => {
    const marks = pdfMarks(doc, { quotes: ["Lockout  required: yes"], fact: null, page: 2 });
    const strong = marks.filter((m) => m.strong);
    expect(strong).toHaveLength(1);
    expect(strong[0]?.facts.map((f) => f.id)).toEqual(["PROC-1", "PROC-1|REL|X"]);
    expect(marks.filter((m) => m.page === 2 && !m.strong)).toHaveLength(0);
  });

  test("with no quote the spans of the opened fact are strong", () => {
    const marks = pdfMarks(doc, { quotes: [""], fact: "A-1", page: null });
    expect(marks.find((m) => m.page === 1)?.strong).toBe(true);
    expect(marks.find((m) => m.page === 2)?.strong).toBe(false);
  });

  test("spans with no page are left out", () => {
    expect(pdfMarks(doc, { quotes: [], fact: null, page: null }).some((m) => m.quote === "no page")).toBe(false);
  });
});
