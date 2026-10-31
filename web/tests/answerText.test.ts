import { describe, expect, test } from "bun:test";
import { alarmFacts, causeRank, pastStops, shortOnStock } from "../src/lib/answerText";
import type { Cause, GraphElements, PartNeed } from "../src/lib/types";

const cause = (over: Partial<Cause>): Cause => ({
  asset_id: "HP-01",
  failure_mode_id: "FM-A",
  name: "main cylinder seal wear",
  likelihood: 0.7,
  support: 6,
  of_total: 6,
  citations: [],
  path: [],
  ...over,
});

describe("pastStops", () => {
  test("says how many past stops of the alarm on the machine had this cause", () => {
    expect(pastStops(cause({}), "E-217", "HP-01")).toBe("6 of 6 past E-217 stops on HP-01");
  });

  test("one stop reads in the singular", () => {
    expect(pastStops(cause({ support: 1, of_total: 1 }), "E-217", "HP-01")).toBe("1 of 1 past E-217 stop on HP-01");
  });

  test("without an alarm or a machine it still reads plainly", () => {
    expect(pastStops(cause({ support: 2, of_total: 5 }), null, "HP-01")).toBe("2 of 5 past stops on HP-01");
    expect(pastStops(cause({ support: 2, of_total: 5 }), "E-217", null)).toBe("2 of 5 past E-217 stops");
    expect(pastStops(cause({ support: 2, of_total: 5 }), null, null)).toBe("2 of 5 past stops");
  });

  test("a cause with no past stops says it is from the manual only", () => {
    expect(pastStops(cause({ support: 0, of_total: 0 }), "E-217", "HP-01")).toBe("from the manual only, no past stops on record");
  });

  test("zero of some stops is said as it is", () => {
    expect(pastStops(cause({ support: 0, of_total: 6 }), "E-217", "HP-01")).toBe("0 of 6 past E-217 stops on HP-01");
  });
});

describe("causeRank", () => {
  const causes = [cause({ failure_mode_id: "FM-A", likelihood: 0.7 }), cause({ failure_mode_id: "FM-B", likelihood: 0.3 })];
  test("the likeliest cause is the most likely one", () => {
    expect(causeRank(causes[0]!, causes)).toBe("Most likely root cause");
  });
  test("another cause is a less likely one", () => {
    expect(causeRank(causes[1]!, causes)).toBe("Less likely root cause");
  });
  test("a cause that ties for the top is the most likely one too", () => {
    const tie = [cause({ failure_mode_id: "FM-A", likelihood: 0.5 }), cause({ failure_mode_id: "FM-B", likelihood: 0.5 })];
    expect(causeRank(tie[1]!, tie)).toBe("Most likely root cause");
  });
});

describe("shortOnStock", () => {
  const part = (over: Partial<PartNeed>): PartNeed => ({
    part_no: "GRP-FNG-12",
    description: "Gripper finger",
    qty: 2,
    qty_on_hand: 0,
    bin: "D-08",
    lead_time_days: 14,
    stock_as_of: "2026-10-15",
    enough: false,
    citations: [],
    path: [],
    ...over,
  });
  test("a part that is short says how many are needed and how many are there", () => {
    expect(shortOnStock(part({}))).toBe("Short: 2 needed, 0 on hand. Lead time 14 days.");
  });
  test("no lead time leaves that sentence out", () => {
    expect(shortOnStock(part({ lead_time_days: null }))).toBe("Short: 2 needed, 0 on hand.");
  });
  test("a part with enough stock says nothing", () => {
    expect(shortOnStock(part({ enough: true, qty_on_hand: 9 }))).toBeNull();
  });
  test("an unknown stock is said as unknown", () => {
    expect(shortOnStock(part({ qty_on_hand: null, enough: false }))).toBe("Stock unknown, 2 needed. Lead time 14 days.");
  });
});

describe("alarmFacts", () => {
  const graph: GraphElements = {
    nodes: [
      { data: { id: "E-217", label: "Main cylinder pressure loss", type: "AlarmCode", family: "problem", glyph: "siren" } },
      { data: { id: "HP-01", label: "Hydraulic press", type: "Asset", family: "where", glyph: "cog" } },
      { data: { id: "INC-1", label: "press losing pressure", type: "Incident", family: "history", glyph: "history" } },
    ],
    edges: [],
  };
  test("reads the message of the alarm and the name of the machine", () => {
    expect(alarmFacts(graph, "E-217", "HP-01")).toEqual({ message: "Main cylinder pressure loss", machineName: "Hydraulic press" });
  });
  test("a node of another type with the same id is not taken", () => {
    expect(alarmFacts(graph, "INC-1", "HP-01").message).toBeNull();
  });
  test("what the graph lacks is null", () => {
    expect(alarmFacts({ nodes: [], edges: [] }, "E-217", "HP-01")).toEqual({ message: null, machineName: null });
    expect(alarmFacts(graph, null, null)).toEqual({ message: null, machineName: null });
  });
});
