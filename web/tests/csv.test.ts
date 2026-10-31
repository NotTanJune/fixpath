import { describe, expect, test } from "bun:test";
import { numericColumns, parseCsv, rowByCells } from "../src/lib/csv";

const OLD = "stock count 30.06.2026\nPN;Desc;OnHand;Loc\nBRG-6205-2RS;Deep groove ball;6;A02\nSEALSPDL70;Spindle front seal;3;A05\n";

describe("parsing a csv", () => {
  test("a plain file: header, rows, comma", () => {
    const t = parseCsv("a,b,c\n1,2,3\n4,5,6\n");
    expect(t.delimiter).toBe(",");
    expect(t.header).toEqual(["a", "b", "c"]);
    expect(t.rows.map((r) => r.cells)).toEqual([
      ["1", "2", "3"],
      ["4", "5", "6"],
    ]);
    expect(t.rows.map((r) => r.no)).toEqual([1, 2]);
    expect(t.caption).toEqual([]);
  });

  test("quoted fields keep commas, line breaks and doubled quotes", () => {
    const t = parseCsv('id,note,tech\n1,"press, losing\npressure ""hard""",Rajesh\n2,ok,Lim\n');
    expect(t.rows[0]?.cells).toEqual(["1", 'press, losing\npressure "hard"', "Rajesh"]);
    expect(t.rows[1]?.cells).toEqual(["2", "ok", "Lim"]);
  });

  test("the offsets of a row are its raw text, line breaks inside quotes included", () => {
    const text = 'id,note\n1,"a,\nb"\n2,c\n';
    const t = parseCsv(text);
    expect(text.slice(t.rows[0]!.start, t.rows[0]!.end)).toBe('1,"a,\nb"');
    expect(text.slice(t.rows[1]!.start, t.rows[1]!.end)).toBe("2,c");
  });

  test("CRLF line endings, a byte order mark and a missing final newline", () => {
    const text = "﻿a,b\r\n1,2\r\n3,4";
    const t = parseCsv(text);
    expect(t.header).toEqual(["a", "b"]);
    expect(t.rows.map((r) => r.cells)).toEqual([
      ["1", "2"],
      ["3", "4"],
    ]);
    expect(text.slice(t.rows[1]!.start, t.rows[1]!.end)).toBe("3,4");
  });

  test("blank lines are skipped and an empty file is an empty table", () => {
    expect(parseCsv("a,b\n\n1,2\n\n").rows).toHaveLength(1);
    expect(parseCsv("")).toEqual({ delimiter: ",", caption: [], header: [], rows: [] });
  });

  test("a semicolon file with a title line above the header", () => {
    const t = parseCsv(OLD);
    expect(t.delimiter).toBe(";");
    expect(t.caption).toEqual(["stock count 30.06.2026"]);
    expect(t.header).toEqual(["PN", "Desc", "OnHand", "Loc"]);
    expect(t.rows).toHaveLength(2);
    expect(OLD.slice(t.rows[0]!.start, t.rows[0]!.end)).toBe("BRG-6205-2RS;Deep groove ball;6;A02");
  });

  test("commas inside quotes do not make a semicolon file a comma file", () => {
    const t = parseCsv('a;b\n"x,y,z";2\n"p,q";3\n');
    expect(t.delimiter).toBe(";");
    expect(t.rows[0]?.cells).toEqual(["x,y,z", "2"]);
  });

  test("comment lines before the header are the caption, without their marker", () => {
    const t = parseCsv("# exported 3 Oct\n# by stores\nid,qty\n1,2\n");
    expect(t.caption).toEqual(["exported 3 Oct", "by stores"]);
    expect(t.header).toEqual(["id", "qty"]);
  });

  test("a title line that holds a delimiter is still a caption", () => {
    const t = parseCsv("Spares, line 3\nPN,Qty,Bin\nA,1,B1\n");
    expect(t.caption).toEqual(["Spares, line 3"]);
    expect(t.header).toEqual(["PN", "Qty", "Bin"]);
  });

  test("a tab file", () => {
    expect(parseCsv("a\tb\n1\t2\n").delimiter).toBe("\t");
  });
});

describe("the cited row", () => {
  const t = parseCsv(OLD);
  test("found by its cell content when the quote is not the raw line", () => {
    expect(rowByCells(t, "SEALSPDL70 ; Spindle front seal ; 3 ; A05")?.no).toBe(2);
    expect(rowByCells(t, "Spindle front seal;A05")?.no).toBe(2);
  });
  test("no row when the cells do not match", () => {
    expect(rowByCells(t, "nothing like it;at all")).toBeNull();
    expect(rowByCells(t, "  ")).toBeNull();
  });
  test("one shared cell is not enough to name a row", () => {
    expect(rowByCells(t, "x;y;z;A05")).toBeNull();
  });
});

describe("numeric columns", () => {
  test("a column of numbers is right-aligned, text is not", () => {
    const t = parseCsv("PN,Qty,Cost\nA,3,1.50\nB,12,20\nC,,4\n");
    expect(numericColumns(t)).toEqual([false, true, true]);
  });
});
