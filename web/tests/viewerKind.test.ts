import { describe, expect, test } from "bun:test";
import { viewerKind } from "../src/lib/viewerKind";

describe("which viewer a source gets", () => {
  test("by the kind the API gives", () => {
    expect(viewerKind({ path: "manuals/BH-250 service manual.pdf", kind: "pdf", doc_type: "manual" })).toBe("pdf");
    expect(viewerKind({ path: "inventory/spares_2026-10.xlsx", kind: "workbook", doc_type: "inventory" })).toBe("workbook");
  });

  test("a csv is a table whatever its type", () => {
    expect(viewerKind({ path: "maintenance/maint_log_export_2026.csv", kind: "text", doc_type: "maintenance_note" })).toBe("csv");
    expect(viewerKind({ path: "inventory/spares_old.csv", kind: "text", doc_type: "inventory" })).toBe("csv");
  });

  test("a log is numbered lines, a note or handover is a note", () => {
    expect(viewerKind({ path: "logs/HP01.log", kind: "text", doc_type: "log" })).toBe("log");
    expect(viewerKind({ path: "handovers/HO 04-12 A shift.txt", kind: "text", doc_type: "handover" })).toBe("note");
    expect(viewerKind({ path: "maintenance/note_from_arun_CNC2_spindle.txt", kind: "text", doc_type: "maintenance_note" })).toBe("note");
  });

  test("the file extension decides when the kind is missing", () => {
    expect(viewerKind({ path: "x/y.PDF" })).toBe("pdf");
    expect(viewerKind({ path: "x/y.xlsx" })).toBe("workbook");
    expect(viewerKind({ path: "x/y.csv" })).toBe("csv");
    expect(viewerKind({ path: "x/y.log" })).toBe("log");
    expect(viewerKind({ path: "x/y" })).toBe("note");
  });
});
