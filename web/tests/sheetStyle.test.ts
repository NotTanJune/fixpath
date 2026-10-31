import { describe, expect, test } from "bun:test";
import {
  borderCss,
  cellRef,
  columnIndex,
  effectiveBorders,
  formatValue,
  freezeCounts,
  mergeMap,
  parseRange,
  sizePx,
} from "../src/lib/sheetStyle";

describe("column letters", () => {
  test("a letter names a column from 0", () => {
    expect(columnIndex("A")).toBe(0);
    expect(columnIndex("H")).toBe(7);
    expect(columnIndex("Z")).toBe(25);
    expect(columnIndex("AA")).toBe(26);
    expect(columnIndex("ab")).toBe(27);
  });
  test("a row and a column make a cell name", () => {
    expect(cellRef(0, 0)).toBe("A1");
    expect(cellRef(3, 7)).toBe("H4");
    expect(cellRef(0, 26)).toBe("AA1");
  });
  test("anything that is not letters is no column", () => {
    expect(columnIndex("")).toBe(-1);
    expect(columnIndex("A1")).toBe(-1);
  });
});

describe("number formats", () => {
  test("#,##0 groups thousands and shows no decimals", () => {
    expect(formatValue("6", "#,##0")).toBe("6");
    expect(formatValue("1234567", "#,##0")).toBe("1,234,567");
    expect(formatValue("38.5", "#,##0")).toBe("39");
  });
  test("#,##0.00 shows two decimals", () => {
    expect(formatValue("18", "#,##0.00")).toBe("18.00");
    expect(formatValue("38.5", "#,##0.00")).toBe("38.50");
    expect(formatValue("12345.678", "#,##0.00")).toBe("12,345.68");
  });
  test("0 and 0.0 formats show no grouping", () => {
    expect(formatValue("1234.5", "0")).toBe("1235");
    expect(formatValue("1234.5", "0.00")).toBe("1234.50");
  });
  test("text, empty values and values with units are kept as written", () => {
    expect(formatValue("10 pcs", "#,##0")).toBe("10 pcs");
    expect(formatValue("3 wks", "#,##0.00")).toBe("3 wks");
    expect(formatValue("check with stores", "#,##0")).toBe("check with stores");
    expect(formatValue("", "#,##0")).toBe("");
  });
  test("no format, or one that is not known, leaves the value alone", () => {
    expect(formatValue("38.5")).toBe("38.5");
    expect(formatValue("38.5", "General")).toBe("38.5");
    expect(formatValue("38.5", "dd/mm/yyyy")).toBe("38.5");
  });
});

describe("merged ranges", () => {
  test("a range is rows and columns from 0, both ends included", () => {
    expect(parseRange("A1:H1")).toEqual({ r0: 0, c0: 0, r1: 0, c1: 7 });
    expect(parseRange("B2:C4")).toEqual({ r0: 1, c0: 1, r1: 3, c1: 2 });
  });
  test("a single cell is a range of one", () => {
    expect(parseRange("C3")).toEqual({ r0: 2, c0: 2, r1: 2, c1: 2 });
  });
  test("a range that is not one is null", () => {
    expect(parseRange("")).toBeNull();
    expect(parseRange("1:2")).toBeNull();
    expect(parseRange("A1:")).toBeNull();
  });
  test("the first cell of a merge spans it and the others are covered", () => {
    const { anchors, covered } = mergeMap(["A1:H1", "B3:C4", "nonsense"]);
    expect(anchors.get("0:0")).toEqual({ rows: 1, cols: 8 });
    expect(anchors.get("2:1")).toEqual({ rows: 2, cols: 2 });
    expect(covered.has("0:1")).toBe(true);
    expect(covered.has("0:7")).toBe(true);
    expect(covered.has("3:2")).toBe(true);
    expect(covered.has("0:0")).toBe(false);
    expect(covered.has("2:1")).toBe(false);
  });
});

describe("the frozen pane", () => {
  test("the cell names the rows above it and the columns left of it", () => {
    expect(freezeCounts("A4")).toEqual({ rows: 3, cols: 0 });
    expect(freezeCounts("A2")).toEqual({ rows: 1, cols: 0 });
    expect(freezeCounts("C2")).toEqual({ rows: 1, cols: 2 });
  });
  test("A1, nothing and nonsense freeze nothing", () => {
    expect(freezeCounts("A1")).toEqual({ rows: 0, cols: 0 });
    expect(freezeCounts(null)).toEqual({ rows: 0, cols: 0 });
    expect(freezeCounts("zz")).toEqual({ rows: 0, cols: 0 });
  });
});

describe("borders and sizes", () => {
  test("a border side is a css border", () => {
    expect(borderCss({ style: "thin", color: "#c9d0d9" })).toBe("1px solid #c9d0d9");
    expect(borderCss({ style: "medium", color: "#0b6b5d" })).toBe("2px solid #0b6b5d");
    expect(borderCss({ style: "thick", color: "#000000" })).toBe("3px solid #000000");
    expect(borderCss({ style: "dotted", color: "#888888" })).toBe("1px dotted #888888");
    expect(borderCss(undefined)).toBeUndefined();
  });
  test("a cell draws its own right and bottom sides, so that a line between two cells is drawn once", () => {
    const thin = { style: "thin", color: "#ccc" };
    const medium = { style: "medium", color: "#0b6b5d" };
    const cells = {
      A1: { borders: { left: thin, right: thin, top: thin, bottom: medium } },
      B1: { borders: { left: thin, right: thin, top: thin, bottom: medium } },
      A2: { borders: { left: thin, right: thin, top: thin, bottom: thin } },
    };
    const a1 = effectiveBorders(cells, 0, 0);
    expect(a1.bottom).toEqual(medium); // its own side wins over the top of the cell below
    expect(a1.right).toEqual(thin);
    expect(a1.top).toEqual(thin); // first row: no cell above draws it
    expect(a1.left).toEqual(thin); // first column
    const a2 = effectiveBorders(cells, 1, 0);
    expect(a2.top).toBeUndefined(); // A1 draws it
    expect(a2.bottom).toEqual(thin);
  });
  test("a cell without a style draws the side its neighbour draws at the edge", () => {
    const thin = { style: "thin", color: "#ccc" };
    const cells = { C2: { borders: { left: thin, top: thin } } };
    const b2 = effectiveBorders(cells, 1, 1);
    expect(b2.right).toEqual(thin); // C2's left side
    const c1 = effectiveBorders(cells, 0, 2);
    expect(c1.bottom).toEqual(thin); // C2's top side
    expect(effectiveBorders(cells, 1, 0).right).toBeUndefined();
  });
  test("a font size in points is px at the screen's 96 per inch, from the 11 pt default", () => {
    expect(sizePx(undefined)).toBe(14);
    expect(sizePx(11)).toBe(14);
    expect(sizePx(14)).toBeCloseTo(17.8, 1);
  });
});
