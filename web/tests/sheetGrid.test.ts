import { describe, expect, test } from "bun:test";
import { columnLetter, rowKey, titleRows, workbookMarks } from "../src/lib/sheetGrid";
import type { Span } from "../src/lib/types";

const sheets = [
  {
    name: "Spares",
    rows: [
      ["Line 3 spares, stock count 15/10/2026", "", "", ""],
      ["", "", "", ""],
      ["Part No.", "Description", "Qty", "Bin"],
      ["BRG-6205-2RS", "Deep groove ball bearing", "6", "A02"],
      ["SEAL-SPDL-70", "Spindle front seal 70 mm", "3", "A05"],
    ],
  },
  {
    name: "Machines",
    rows: [
      ["Asset tag", "Description"],
      ["CNC-01", "Machining centre 1"],
    ],
  },
];

function span(quote: string, fact_id: string, label = fact_id): Span {
  return { quote, start: 0, end: 1, page: null, fact_kind: "node", fact_id, type: "Part", label, extractor: "t", confidence: 1 };
}

describe("column letters", () => {
  test("A to Z, then AA", () => {
    expect([0, 1, 25, 26, 27, 51, 52, 701, 702].map(columnLetter)).toEqual(["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"]);
  });
});

describe("a row as the text of the document has it", () => {
  test("the filled cells joined by a bar", () => {
    expect(rowKey(["SEAL-SPDL-70", "Spindle front seal 70 mm", "", "A05", " "])).toBe("SEAL-SPDL-70 | Spindle front seal 70 mm | A05");
    expect(rowKey(["", ""])).toBe("");
  });
});

describe("title rows", () => {
  test("a row with one filled cell, in the first column, spans the width", () => {
    expect(titleRows(sheets[0]!.rows)).toEqual([0]);
  });
  test("a header row is not a title, and nothing below the header is", () => {
    expect(titleRows(sheets[1]!.rows)).toEqual([]);
    expect(titleRows([["Part", "Qty"], ["Total", ""]])).toEqual([]);
  });
});

describe("marked rows of a workbook", () => {
  const spans = [
    span("BRG-6205-2RS | Deep groove ball bearing | 6 | A02", "BRG-6205-2RS", "Deep groove ball bearing"),
    span("SEAL-SPDL-70 | Spindle front seal 70 mm | 3 | A05", "SEAL-SPDL-70", "Spindle front seal 70 mm"),
    span("SEAL-SPDL-70 | Spindle front seal 70 mm | 3 | A05", "SEAL-SPDL-70|FITS|CNC-01", "SEAL-SPDL-70 fits CNC-01"),
    span("CNC-01 | Machining centre 1", "CNC-01", "Machining centre 1"),
  ];

  test("the cited quote is the strong row, every other fact's row is faint", () => {
    const m = workbookMarks(sheets, spans, { quotes: ["SEAL-SPDL-70 | Spindle front seal 70 mm | 3 | A05"], fact: null });
    expect(m.get("0:4")?.strong).toBe(true);
    expect(m.get("0:4")?.facts.map((f) => f.id)).toEqual(["SEAL-SPDL-70", "SEAL-SPDL-70|FITS|CNC-01"]);
    expect(m.get("0:3")?.strong).toBe(false);
    expect(m.get("1:1")?.strong).toBe(false);
    expect(m.get("0:2")).toBeUndefined();
  });

  test("a quote cut short or longer than the row still names its row", () => {
    expect(workbookMarks(sheets, [], { quotes: ["SEAL-SPDL-70 | Spindle front seal"], fact: null }).get("0:4")?.strong).toBe(true);
    expect(workbookMarks(sheets, [], { quotes: ["BRG-6205-2RS | Deep groove ball bearing | 6 | A02 | extra"], fact: null }).get("0:3")?.strong).toBe(true);
  });

  test("with no quote the rows of the opened fact are strong", () => {
    const m = workbookMarks(sheets, spans, { quotes: [""], fact: "CNC-01" });
    expect(m.get("1:1")?.strong).toBe(true);
    expect(m.get("0:4")?.strong).toBe(true); // an edge that ends at CNC-01 is a fact about it too
  });

  test("a short quote that is only part of a cell is not taken for a row", () => {
    expect(workbookMarks(sheets, [], { quotes: ["A0"], fact: null }).size).toBe(0);
  });
});
