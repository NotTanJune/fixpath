import { describe, expect, test } from "bun:test";
import { citedFactLine, documentName } from "../src/lib/sourceName";
import { sheetBox } from "../src/lib/sheetFrame";

describe("document names in plain words", () => {
  test("manuals keep the model and say manual or guide once", () => {
    expect(documentName("manuals/BH-250 service manual.pdf")).toBe("BH-250 service manual");
    expect(documentName("manuals/Kessler_KX-500_Operator_Maintenance_Manual_rev3.pdf")).toBe(
      "Kessler KX-500 operator maintenance manual, rev 3",
    );
    expect(documentName("manuals/Orbis_OR6_cell_maintenance_guide.pdf")).toBe(
      "Orbis OR6 cell maintenance guide",
    );
    expect(documentName("manuals/VT-LD20_manual_EN.pdf")).toBe("VT-LD20 manual");
  });

  test("handovers in the three file name styles", () => {
    expect(documentName("handovers/Handover_20260822_B.txt")).toBe("Shift handover 22 Aug, B shift");
    expect(documentName("handovers/handover_2026-09-28_C.txt")).toBe(
      "Shift handover 28 Sep, C shift",
    );
    expect(documentName("handovers/HO 04-23 B shift.txt")).toBe("Shift handover 23 Apr, B shift");
    expect(documentName("handovers/HO 05-05 night shift.txt")).toBe(
      "Shift handover 5 May, night shift",
    );
    expect(documentName("handovers/shift notes 28 Jun B.txt")).toBe(
      "Shift handover 28 Jun, B shift",
    );
    expect(documentName("handovers/shift notes 12 Sep night.txt")).toBe(
      "Shift handover 12 Sep, night shift",
    );
    expect(documentName("handovers/handover_2026-05-22_night.txt")).toBe(
      "Shift handover 22 May, night shift",
    );
  });

  test("logs name the machine and what the log holds", () => {
    expect(documentName("logs/CNC01_alarms_apr-sep.log")).toBe("CNC-01 alarm log, Apr to Sep");
    expect(documentName("logs/HP01.log")).toBe("HP-01 log");
    expect(documentName("logs/cnc-02 alarm export.log")).toBe("CNC-02 alarm export");
    expect(documentName("logs/leaktest_VT-LD20.log")).toBe("Leak test VT-LD20 log");
    expect(documentName("logs/robot_cell_events.log")).toBe("Robot cell event log");
  });

  test("the exports, the stock lists and the loose notes", () => {
    expect(documentName("maintenance/maint_log_export_2026.csv")).toBe(
      "Maintenance log export 2026",
    );
    expect(documentName("inventory/spares_2026-10.xlsx")).toBe("Spares workbook, Oct 2026");
    expect(documentName("inventory/spares_old.csv")).toBe("Old spares list");
    expect(documentName("maintenance/note_from_arun_CNC2_spindle.txt")).toBe(
      "Note from Arun, CNC-02 spindle",
    );
  });

  test("an unknown file falls back to its cleaned name", () => {
    expect(documentName("misc/some_odd-file.txt")).toBe("Some odd file");
  });
});

describe("the cited fact in one line", () => {
  const spans = [
    { fact_id: "E-217", label: "Main cylinder pressure loss" },
    { fact_id: "INC-2026-001|OCCURRED_ON|HP-01", label: "INC-2026-001 happened on HP-01" },
  ];

  test("an id with a fact in the file is named by that fact", () => {
    expect(citedFactLine("E-217", spans, "")).toBe("E-217 Main cylinder pressure loss");
  });

  test("an edge fact's own label is not repeated after the id", () => {
    expect(citedFactLine("INC-2026-001", spans, "")).toBe("INC-2026-001 happened on HP-01");
  });

  test("with no fact in the file the quote is the line, on one line", () => {
    expect(citedFactLine("Lockout", [], "Before you work\n  on the hydraulic system")).toBe(
      "Lockout: Before you work on the hydraulic system",
    );
  });

  test("a long quote stays whole; the page cuts it with an ellipsis", () => {
    const line = citedFactLine("X-1", [], "word ".repeat(60));
    expect(line).toBe(`X-1: ${"word ".repeat(59)}word`);
  });

  test("the label alone when there is nothing else", () => {
    expect(citedFactLine("HP-01", [], "")).toBe("HP-01");
  });
});

describe("where the sheet sits", () => {
  const viewport = { width: 1440, height: 900 };
  test("it covers the graph pane edge to edge", () => {
    const side = { left: 820, top: 48, right: 1440, bottom: 900 };
    expect(sheetBox(side, { left: 280, top: 48, right: 1440, bottom: 900 }, viewport, 48)).toEqual({
      left: 820,
      top: 48,
      width: 620,
      height: 852,
    });
  });
  test("with no graph pane it covers the content area", () => {
    expect(sheetBox(null, { left: 280, top: 48, right: 1440, bottom: 900 }, viewport, 48)).toEqual({
      left: 280,
      top: 48,
      width: 1160,
      height: 852,
    });
  });
  test("a pane too narrow to read in is not used", () => {
    const side = { left: 1400, top: 48, right: 1440, bottom: 900 };
    const content = { left: 280, top: 48, right: 1440, bottom: 900 };
    expect(sheetBox(side, content, viewport, 48).left).toBe(280);
  });
  test("with nothing measured it covers the viewport under the bar", () => {
    expect(sheetBox(null, null, viewport, 48)).toEqual({ left: 0, top: 48, width: 1440, height: 852 });
  });
});
