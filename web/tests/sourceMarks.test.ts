import { describe, expect, test } from "bun:test";
import { factNames, lineMarks, markRanges, splitLines } from "../src/lib/sourceMarks";
import type { Span } from "../src/lib/types";

function span(start: number, end: number, fact_id: string, label: string): Span {
  return {
    quote: "",
    start,
    end,
    page: null,
    fact_kind: "node",
    fact_id,
    type: "X",
    label,
    extractor: "t",
    confidence: 1,
  };
}

const TEXT = "alpha one\nbeta  two\ngamma three\ndelta four\n";

describe("marks of a source", () => {
  test("a cited quote is the strong mark, found across different whitespace", () => {
    const r = markRanges({ text: TEXT, spans: [] }, { quotes: ["beta two"], fact: null });
    expect(r).toEqual([{ start: 10, end: 19, strong: true, facts: [] }]);
  });

  test("every other fact is a faint mark, and spans on one range share it", () => {
    const spans = [
      span(0, 9, "A-1", "Alpha"),
      span(0, 9, "A-1|REL|B-1", "A-1 relates to B-1"),
      span(20, 31, "C-1", "Gamma"),
    ];
    const r = markRanges({ text: TEXT, spans }, { quotes: ["beta two"], fact: null });
    expect(r.map((m) => [m.start, m.end, m.strong])).toEqual([
      [0, 9, false],
      [10, 19, true],
      [20, 31, false],
    ]);
    expect(r[0]?.facts.map((f) => f.id)).toEqual(["A-1", "A-1|REL|B-1"]);
  });

  test("a fact inside the cited quote joins the strong mark and draws nothing of its own", () => {
    const spans = [span(15, 19, "B-1", "Beta")];
    const r = markRanges({ text: TEXT, spans }, { quotes: ["beta two"], fact: null });
    expect(r).toHaveLength(1);
    expect(r[0]?.strong).toBe(true);
    expect(r[0]?.facts.map((f) => f.id)).toEqual(["B-1"]);
  });

  test("with no quote cited, the spans of the opened fact are strong", () => {
    const spans = [span(0, 9, "A-1", "Alpha"), span(20, 31, "C-1|REL|D-1", "C-1 relates to D-1")];
    const r = markRanges({ text: TEXT, spans }, { quotes: [""], fact: "D-1" });
    expect(r.map((m) => [m.start, m.strong])).toEqual([
      [0, false],
      [20, true],
    ]);
  });

  test("a quote that is not in the text falls back to the spans of the opened fact", () => {
    const spans = [span(0, 9, "A-1", "Alpha")];
    const r = markRanges({ text: TEXT, spans }, { quotes: ["not there"], fact: "A-1" });
    expect(r).toEqual([{ start: 0, end: 9, strong: true, facts: [{ id: "A-1", label: "Alpha", at: 0 }] }]);
  });

  test("faint marks that overlap are merged into the first, so nothing is drawn twice", () => {
    const spans = [span(0, 12, "A-1", "Alpha"), span(6, 15, "B-1", "Beta")];
    const r = markRanges({ text: TEXT, spans }, { quotes: [], fact: null });
    expect(r).toHaveLength(1);
    expect(r[0]).toMatchObject({ start: 0, end: 12, strong: false });
    expect(r[0]?.facts.map((f) => f.id)).toEqual(["A-1", "B-1"]);
  });

  test("a failure-mode id in a label is read as its name", () => {
    const r = markRanges(
      { text: TEXT, spans: [span(0, 9, "X", "INC-1 was caused by FM-MAIN-CYLINDER-SEAL-WEAR")] },
      { quotes: [], fact: null },
    );
    expect(r[0]?.facts[0]?.label).toBe("INC-1 was caused by main cylinder seal wear");
  });
});

describe("lines", () => {
  test("numbered from 1, with the offsets of each, and no phantom last line", () => {
    const lines = splitLines(TEXT);
    expect(lines.map((l) => [l.no, l.start, l.end, l.text])).toEqual([
      [1, 0, 9, "alpha one"],
      [2, 10, 19, "beta  two"],
      [3, 20, 31, "gamma three"],
      [4, 32, 42, "delta four"],
    ]);
  });

  test("a CRLF file keeps its offsets and shows no carriage return", () => {
    const lines = splitLines("a\r\nbb\r\n");
    expect(lines.map((l) => [l.start, l.end, l.text])).toEqual([
      [0, 1, "a"],
      [3, 5, "bb"],
    ]);
  });

  test("a file with no final newline keeps its last line", () => {
    expect(splitLines("a\nb").map((l) => l.text)).toEqual(["a", "b"]);
    expect(splitLines("")).toEqual([]);
  });

  test("a mark that spans two lines marks both, and the strong one wins", () => {
    const lines = splitLines(TEXT);
    const marks = lineMarks(lines, [
      { start: 5, end: 15, strong: true, facts: [{ id: "A", label: "a" }] },
      { start: 20, end: 25, strong: false, facts: [{ id: "C", label: "c" }] },
    ]);
    expect(marks.map((m) => m && m.strong)).toEqual([true, true, false, null]);
    expect(marks[2]?.facts.map((f) => f.id)).toEqual(["C"]);
  });
});

describe("naming the facts of a mark", () => {
  const f = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `F${i}`, label: `Fact ${i}` }));
  test("up to four by name, then a count", () => {
    expect(factNames(f(2))).toEqual(["Fact 0", "Fact 1"]);
    expect(factNames(f(6))).toEqual(["Fact 0", "Fact 1", "Fact 2", "Fact 3", "and 2 more"]);
    expect(factNames([])).toEqual([]);
  });
});

describe("the span a mark stands for", () => {
  test("each fact of a mark says which span of the file it came from, so that a click finds its row", () => {
    const spans = [span(0, 9, "A-1", "Alpha"), span(0, 9, "A-1|REL|B-1", "A-1 relates to B-1"), span(20, 31, "C-1", "Gamma")];
    const r = markRanges({ text: TEXT, spans }, { quotes: [], fact: null });
    expect(r[0]?.facts.map((f) => f.at)).toEqual([0, 1]);
    expect(r[1]?.facts.map((f) => f.at)).toEqual([2]);
  });

  test("a range given by the reader is the strong mark, whatever else the text says", () => {
    const spans = [span(0, 9, "A-1", "Alpha"), span(20, 31, "C-1", "Gamma")];
    const r = markRanges({ text: TEXT, spans }, { quotes: ["alpha one"], fact: "A-1", range: { start: 20, end: 31 } });
    expect(r.filter((m) => m.strong).map((m) => [m.start, m.end])).toEqual([[20, 31]]);
    expect(r.filter((m) => !m.strong).map((m) => [m.start, m.end])).toEqual([[0, 9]]);
    expect(r.find((m) => m.strong)?.facts.map((f) => f.id)).toEqual(["C-1"]);
  });

  test("with no range the opened fact still stands in for a quote that is not found", () => {
    const spans = [span(0, 9, "A-1", "Alpha"), span(20, 31, "C-1", "Gamma")];
    const r = markRanges({ text: TEXT, spans }, { quotes: [], fact: "C-1" });
    expect(r.filter((m) => m.strong).map((m) => m.start)).toEqual([20]);
  });
});
