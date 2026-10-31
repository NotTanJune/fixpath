import { describe, expect, test } from "bun:test";
import {
  backOf,
  centreCaption,
  centreForFile,
  degreesOf,
  edgeWords,
  factNodeIds,
  fallbackCentre,
  groupPicker,
  mergeGraph,
  navTo,
  type Nav,
} from "../src/lib/explorerFocus";

const edges = [
  { source: "LINE-03", target: "HP-01" },
  { source: "HP-01", target: "HP-01-MAINCYL" },
  { source: "HP-01", target: "E-217" },
  { source: "HP-01", target: "E-200" },
  { source: "E-217", target: "FM-SEAL" },
  { source: "INC-1", target: "HP-01" },
  { source: "INC-1", target: "E-217" },
];
const degree = degreesOf(edges);

const node = (fact_id: string) => ({ fact_kind: "node" as const, fact_id });
const edge = (fact_id: string) => ({ fact_kind: "edge" as const, fact_id });

describe("how connected each node is", () => {
  test("a node counts every edge that ends in it, in either direction", () => {
    expect(degree.get("HP-01")).toBe(5);
    expect(degree.get("E-217")).toBe(3);
    expect(degree.get("LINE-03")).toBe(1);
    expect(degree.get("NOPE")).toBeUndefined();
  });
});

describe("the nodes a file names", () => {
  test("a node fact is its own id, an edge fact names both ends, and each is listed once", () => {
    const spans = [node("E-217"), edge("E-217|INDICATES|FM-SEAL"), edge("INC-1|OCCURRED_ON|HP-01"), node("E-217")];
    expect(factNodeIds(spans)).toEqual(["E-217", "FM-SEAL", "INC-1", "HP-01"]);
  });
  test("an id with a different shape of edge fact is left as it is", () => {
    expect(factNodeIds([edge("A|B")])).toEqual([]);
  });
});

describe("where the explorer centres for a file", () => {
  test("on the most connected node of the file", () => {
    const spans = [node("E-217"), node("E-200"), node("HP-01"), node("FM-SEAL")];
    expect(centreForFile(spans, degree, null)).toBe("HP-01");
  });

  test("a tie goes to the id that sorts first, so that the choice is the same every time", () => {
    expect(centreForFile([node("E-200"), node("FM-SEAL")], degree, null)).toBe("E-200");
  });

  test("an edge fact counts for the nodes at its ends", () => {
    expect(centreForFile([edge("E-217|INDICATES|FM-SEAL")], degree, null)).toBe("E-217");
  });

  test("a node that the graph does not know is never the centre", () => {
    expect(centreForFile([node("GHOST-9"), node("E-200")], degree, null)).toBe("E-200");
    expect(centreForFile([node("GHOST-9")], degree, null)).toBeNull();
  });

  test("a file with no facts has no centre", () => {
    expect(centreForFile([], degree, null)).toBeNull();
  });

  test("the selected row decides: a node fact is its own centre, an edge fact starts at its first end", () => {
    const spans = [node("HP-01"), node("E-200"), edge("E-217|INDICATES|FM-SEAL")];
    expect(centreForFile(spans, degree, 1)).toBe("E-200");
    expect(centreForFile(spans, degree, 2)).toBe("E-217");
  });

  test("a selected edge whose first end is not in the graph starts at the other end", () => {
    expect(centreForFile([edge("GHOST-9|INDICATES|FM-SEAL")], degree, 0)).toBe("FM-SEAL");
  });

  test("a row that does not exist falls back to the most connected node", () => {
    expect(centreForFile([node("E-200"), node("HP-01")], degree, 9)).toBe("HP-01");
  });

  test("with no facts at all the plant's line is the place to start", () => {
    const nodes = [
      { id: "HP-01", type: "Asset" },
      { id: "LINE-03", type: "Line" },
    ];
    expect(fallbackCentre(nodes)).toBe("LINE-03");
    expect(fallbackCentre([{ id: "HP-01", type: "Asset" }])).toBe("HP-01");
    expect(fallbackCentre([])).toBeNull();
  });
});

describe("the words above the tree", () => {
  test("the caption names what is at the centre, by its type and its name", () => {
    expect(centreCaption("Alarm", "E-217")).toBe("Alarm E-217, with its links");
    expect(centreCaption("Root cause", "Main cylinder seal wear")).toBe("Root cause Main cylinder seal wear, with its links");
    expect(centreCaption("Machine", "HP-01")).toBe("Machine HP-01, with its links");
  });

  test("a type word and a name are left as they are", () => {
    expect(centreCaption("Past breakdown", "INC-2026-008")).toBe("Past breakdown INC-2026-008, with its links");
  });
});

describe("the words on a link", () => {
  const word = (rel: string) => (rel === "INDICATES" ? "usually means" : rel === "RESOLVED_BY" ? "fixed by" : rel.toLowerCase());
  test("read from the node the line leaves, the relation says what the graph says", () => {
    expect(edgeWords("INDICATES", true, word)).toBe("usually means");
  });
  test("read the other way it is turned round, so that the sentence is still true", () => {
    expect(edgeWords("INDICATES", false, word)).toBe("shows up as");
    expect(edgeWords("RESOLVED_BY", false, word)).toBe("fixes");
  });
  test("every relation of the graph has words for both directions", () => {
    for (const rel of [
      "HAS_MACHINE",
      "HAS_COMPONENT",
      "RAISED",
      "INDICATES",
      "AFFECTS",
      "RESOLVED_BY",
      "REQUIRES",
      "FITS",
      "OCCURRED_ON",
      "TRIGGERED_BY",
      "ROOT_CAUSE",
      "USED",
    ]) {
      expect(edgeWords(rel, false, word).length).toBeGreaterThan(0);
      expect(edgeWords(rel, false, word)).not.toBe(word(rel));
    }
  });
  test("a relation that is not known reads as a plain link the other way", () => {
    expect(edgeWords("MYSTERY", false, () => "mystery")).toBe("linked to");
  });
});

describe("the picker of nodes", () => {
  const nodes = [
    { id: "E-217", label: "Main cylinder pressure loss", type: "AlarmCode" },
    { id: "E-200", label: "Pump start", type: "AlarmCode" },
    { id: "HP-01", label: "Hydraulic press", type: "Asset" },
    { id: "FM-MAIN-CYLINDER-SEAL-WEAR", label: "main cylinder seal wear", type: "FailureMode" },
    { id: "INC-1", label: "press losing pressure", type: "Incident" },
    { id: "LINE-03", label: "Line 3", type: "Line" },
  ];
  const words = (t: string) => ({ Asset: "Machine", AlarmCode: "Alarm", FailureMode: "Root cause", Incident: "Past breakdown", Line: "Production line" })[t] ?? t;

  test("every node, grouped by the type, in the order of the plant, with the plural for the heading", () => {
    const groups = groupPicker(nodes, "", words);
    expect(groups.map((g) => g.heading)).toEqual(["Production lines", "Machines", "Alarms", "Root causes", "Past breakdowns"]);
    expect(groups[2]!.items.map((i) => i.id)).toEqual(["E-200", "E-217"]);
  });

  test("a query matches the ID, the name or the type word, whatever its case", () => {
    expect(groupPicker(nodes, "e-217", words).flatMap((g) => g.items.map((i) => i.id))).toEqual(["E-217"]);
    expect(groupPicker(nodes, "PRESS", words).flatMap((g) => g.items.map((i) => i.id)).sort()).toEqual(["E-217", "HP-01", "INC-1"]);
    expect(groupPicker(nodes, "root cause", words).flatMap((g) => g.items.map((i) => i.id))).toEqual(["FM-MAIN-CYLINDER-SEAL-WEAR"]);
  });

  test("a group with no match is left out, and nothing matching gives no group", () => {
    expect(groupPicker(nodes, "pump", words).map((g) => g.heading)).toEqual(["Alarms"]);
    expect(groupPicker(nodes, "zzz", words)).toEqual([]);
  });

  test("an item says its name in words: a cause by its name, the rest by their ID with the name", () => {
    const all = groupPicker(nodes, "", words).flatMap((g) => g.items);
    expect(all.find((i) => i.id === "FM-MAIN-CYLINDER-SEAL-WEAR")!.text).toBe("Main cylinder seal wear");
    expect(all.find((i) => i.id === "E-217")!.text).toBe("E-217 Main cylinder pressure loss");
  });
});

describe("going back", () => {
  const start: Nav = { centre: "HP-01", back: [] };
  test("a new centre keeps the one it left, so that Back can return", () => {
    const next = navTo(start, "E-217");
    expect(next).toEqual({ centre: "E-217", back: ["HP-01"] });
    expect(backOf(next)).toEqual({ centre: "HP-01", back: [] });
  });
  test("the same centre again changes nothing", () => {
    expect(navTo(start, "HP-01")).toBe(start);
  });
  test("Back with nowhere to go changes nothing", () => {
    expect(backOf(start)).toBe(start);
  });
  test("the trail is kept short", () => {
    let nav = start;
    for (let i = 0; i < 40; i++) nav = navTo(nav, `N-${i}`);
    expect(nav.back.length).toBeLessThanOrEqual(20);
    expect(nav.back[nav.back.length - 1]).toBe("N-38");
  });
});

describe("a graph with the nodes a run added", () => {
  const base = {
    nodes: [
      { id: "HP-01", label: "Hydraulic press", type: "Asset" },
      { id: "E-217", label: "Pressure loss", type: "AlarmCode" },
    ],
    edges: [{ id: "HP-01|RAISED|E-217", source: "HP-01", target: "E-217", rel: "RAISED" }],
  };
  const inc = { id: "INC-1", label: "stopped", type: "Incident" };
  const link = (to: string) => ({ id: `INC-1|OCCURRED_ON|${to}`, source: "INC-1", target: to, rel: "OCCURRED_ON" });

  test("the new nodes and the links between known nodes are added after the old ones", () => {
    const out = mergeGraph(base, { nodes: [inc], edges: [link("HP-01")] });
    expect(out.nodes.map((n) => n.id)).toEqual(["HP-01", "E-217", "INC-1"]);
    expect(out.edges.map((e) => e.id)).toEqual(["HP-01|RAISED|E-217", "INC-1|OCCURRED_ON|HP-01"]);
  });

  test("what the graph already has is not added twice, so a graph that was fetched again wins", () => {
    const out = mergeGraph(
      { nodes: [...base.nodes, { ...inc, label: "the stored one" }], edges: [...base.edges, link("HP-01")] },
      { nodes: [inc], edges: [link("HP-01")] },
    );
    expect(out.nodes.filter((n) => n.id === "INC-1")).toEqual([{ ...inc, label: "the stored one" }]);
    expect(out.edges).toHaveLength(2);
  });

  test("a link to a node nobody has is left out", () => {
    const out = mergeGraph(base, { nodes: [inc], edges: [link("GONE")] });
    expect(out.edges.map((e) => e.id)).toEqual(["HP-01|RAISED|E-217"]);
  });

  test("with nothing added it is the graph itself", () => {
    expect(mergeGraph(base, { nodes: [], edges: [] })).toBe(base);
    expect(mergeGraph(base, null)).toBe(base);
  });
});
