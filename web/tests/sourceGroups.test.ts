import { describe, expect, test } from "bun:test";
import {
  categoryOf,
  groupKeyOf,
  groupSources,
  initialOpen,
  openKeys,
  toggleGroup,
  type OpenState,
} from "../src/lib/sourceGroups";

const doc = (id: string, doc_type: string, path: string) => ({ id, doc_type, path, bytes: 1 });
const sources = [
  doc("M1", "manual", "manuals/a.pdf"),
  doc("M2", "manual", "manuals/b.pdf"),
  doc("L1", "log", "logs/a.log"),
  doc("H1", "handover", "handovers/HO 04-12 A shift.txt"),
  doc("H2", "handover", "handovers/HO 04-13 A shift.txt"),
  doc("H3", "handover", "handovers/HO 04-14 B shift.txt"),
  doc("N1", "maintenance_note", "maintenance/note_from_arun.txt"),
  doc("N2", "maintenance_note", "maintenance/note_from_rajesh.txt"),
  doc("X1", "maintenance_note", "maintenance/maint_log_export_2026.csv"),
  doc("S1", "inventory", "inventory/spares_2026-10.xlsx"),
  doc("S2", "inventory", "inventory/spares_old.csv"),
];

describe("source groups", () => {
  test("six groups, in plain words, in the order of the pipeline, with their counts", () => {
    expect(groupSources(sources).map((g) => [g.label, g.items.length])).toEqual([
      ["Machine manuals", 2],
      ["Machine logs", 1],
      ["Shift handovers", 3],
      ["Maintenance notes", 2],
      ["Maintenance export", 1],
      ["Stock lists", 2],
    ]);
  });

  test("the maintenance export is told from the maintenance notes by the file, not by the type", () => {
    expect(groupKeyOf(sources[8]!)).toBe("export");
    expect(groupKeyOf(sources[6]!)).toBe("notes");
  });

  test("a group with no files is left out, and a file with no type is in Other files", () => {
    const some = [doc("A", "manual", "m.pdf"), doc("B", "", "x.bin")];
    expect(groupSources(some).map((g) => [g.key, g.label])).toEqual([
      ["manuals", "Machine manuals"],
      ["other", "Other files"],
    ]);
    expect(groupSources([])).toEqual([]);
  });

  test("a type that is not known has a group of its own, labelled from the type", () => {
    const some = [
      doc("P1", "procedure", "procedures/p1.txt"),
      doc("A", "manual", "m.pdf"),
      doc("P2", "procedure", "procedures/p2.txt"),
      doc("W", "work_order", "w.txt"),
      doc("Q", "inquiry", "q.txt"),
    ];
    expect(groupSources(some).map((g) => [g.label, g.items.length])).toEqual([
      ["Machine manuals", 1],
      ["Procedures", 2],
      ["Work orders", 1],
      ["Inquiries", 1],
    ]);
    expect(groupKeyOf(some[0]!)).toBe("type-procedure");
  });

  test("the category of a file is one name, from the type", () => {
    expect(categoryOf(doc("H", "handover", "handovers/a.txt"))).toBe("Shift handover");
    expect(categoryOf(doc("M", "manual", "manuals/a.pdf"))).toBe("Machine manual");
    expect(categoryOf(doc("L", "log", "logs/a.log"))).toBe("Machine log");
    expect(categoryOf(doc("N", "maintenance_note", "maintenance/a.txt"))).toBe("Maintenance note");
    expect(categoryOf(doc("X", "maintenance_note", "maintenance/a.csv"))).toBe("Maintenance export");
    expect(categoryOf(doc("S", "inventory", "inventory/a.xlsx"))).toBe("Stock list");
    expect(categoryOf(doc("P", "procedure", "procedures/a.txt"))).toBe("Procedure");
    expect(categoryOf(doc("W", "work_order", "w.txt"))).toBe("Work order");
    expect(categoryOf(doc("U", "", "u.txt"))).toBe("Other file");
  });

  test("Newly added is the first group, newest first, and the files stay in their categories too", () => {
    const added = (id: string, type: string, path: string, at: string) => ({ ...doc(id, type, path), added: at });
    const all = [
      doc("H1", "handover", "handovers/old.txt"),
      added("H2", "handover", "handovers/new1.txt", "2026-10-29T10:00:00Z"),
      added("P1", "procedure", "procedures/new2.txt", "2026-10-29T11:00:00Z"),
    ];
    const groups = groupSources(all);
    expect(groups.map((g) => [g.key, g.label, g.items.map((s) => s.id)])).toEqual([
      ["added", "Newly added", ["P1", "H2"]],
      ["handovers", "Shift handovers", ["H1", "H2"]],
      ["type-procedure", "Procedures", ["P1"]],
    ]);
  });

  test("there is no Newly added group when no file has a time", () => {
    expect(groupSources(sources).some((g) => g.key === "added")).toBe(false);
  });

  test("a file keeps the order the server gave it, inside its group", () => {
    expect(groupSources(sources)[2]!.items.map((s) => s.id)).toEqual(["H1", "H2", "H3"]);
  });
});

describe("which groups are open", () => {
  const groups = groupSources(sources);

  test("a file that was newly added opens both of its groups", () => {
    const all = [
      { ...doc("N", "handover", "handovers/n.txt"), added: "2026-10-29T10:00:00Z" },
      doc("H", "handover", "handovers/h.txt"),
    ];
    expect(initialOpen(groupSources(all), "N")).toEqual(["added", "handovers"]);
    expect(initialOpen(groupSources(all), "H")).toEqual(["handovers"]);
  });

  test("only the group of the selected file is open at first", () => {
    expect(initialOpen(groups, "H2")).toEqual(["handovers"]);
    expect(initialOpen(groups, "S2")).toEqual(["stock"]);
  });

  test("nothing is open when no file is selected or the file is not in the list", () => {
    expect(initialOpen(groups, undefined)).toEqual([]);
    expect(initialOpen(groups, "nope")).toEqual([]);
  });

  test("a group the reader opens stays open beside the current one", () => {
    let state: OpenState = { at: "H2", keys: initialOpen(groups, "H2") };
    state = toggleGroup(state, groups, "H2", "manuals");
    expect(openKeys(state, groups, "H2").sort()).toEqual(["handovers", "manuals"]);
  });

  test("the reader can shut the current group too", () => {
    let state: OpenState = { at: "H2", keys: initialOpen(groups, "H2") };
    state = toggleGroup(state, groups, "H2", "handovers");
    expect(openKeys(state, groups, "H2")).toEqual([]);
  });

  test("picking a file in another group moves the open state to that group alone", () => {
    let state: OpenState = { at: "H2", keys: initialOpen(groups, "H2") };
    state = toggleGroup(state, groups, "H2", "manuals");
    // the reader picks M1: the selection changed, so what was opened for H2 no longer holds
    expect(openKeys(state, groups, "M1")).toEqual(["manuals"]);
  });

  test("a toggle for a selection that came since starts from that selection's own group", () => {
    const stale: OpenState = { at: "H2", keys: ["handovers", "manuals"] };
    const state = toggleGroup(stale, groups, "S1", "logs");
    expect(openKeys(state, groups, "S1").sort()).toEqual(["logs", "stock"]);
  });
});
