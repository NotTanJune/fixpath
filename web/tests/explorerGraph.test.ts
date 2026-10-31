import { describe, expect, test } from "bun:test";
import { leafRows, textRows } from "../src/lib/askGraph";
import { EXPLORER_FLOOR, startPan } from "../src/lib/graphFit";
import {
  branchWords,
  columnsOf,
  COL_GAP,
  countWords,
  dropPx,
  explorerPlan,
  LEAF_OVER,
  LEAF_W,
  MAGNET_W,
  shownOf,
  type ExEdge,
  type ExNode,
  type ExplorerPlan,
} from "../src/lib/explorerGraph";

const measure = (text: string, px: number) => Math.ceil(text.length * px * 0.5);
const word = (type: string) =>
  ({
    Asset: "Machine",
    AlarmCode: "Alarm",
    FailureMode: "Root cause",
    Incident: "Past breakdown",
    Line: "Production line",
    Component: "Component",
    Part: "Spare part",
    Procedure: "Fix",
  })[type] ?? type;
const forward = (rel: string) =>
  ({
    HAS_MACHINE: "has machine",
    HAS_COMPONENT: "is built from",
    RAISED: "raised",
    INDICATES: "usually means",
    OCCURRED_ON: "happened on",
    TRIGGERED_BY: "started with",
    RESOLVED_BY: "fixed by",
    REQUIRES: "needs",
    ROOT_CAUSE: "was caused by",
    USED: "used",
  })[rel] ?? rel.toLowerCase();

/** A machine with a line, parts, alarms and breakdowns, the shape of the press in the sample plant. */
function machine(id: string, o: { comps: number; alarms: number; incidents: number; line?: boolean }) {
  const ns: ExNode[] = [{ id, label: "Hydraulic press", type: "Asset" }];
  const es: ExEdge[] = [];
  if (o.line !== false) {
    ns.push({ id: "LINE-03", label: "Line 3", type: "Line" });
    es.push({ id: "l", source: "LINE-03", target: id, rel: "HAS_MACHINE" });
  }
  for (let i = 0; i < o.comps; i++) {
    ns.push({ id: `${id}-C${i}`, label: `Component number ${i} with a long description`, type: "Component" });
    es.push({ id: `c${i}`, source: id, target: `${id}-C${i}`, rel: "HAS_COMPONENT" });
  }
  for (let i = 0; i < o.alarms; i++) {
    ns.push({ id: `E-${200 + i}`, label: `Alarm ${i} pressure loss in the main cylinder`, type: "AlarmCode" });
    es.push({ id: `a${i}`, source: id, target: `E-${200 + i}`, rel: "RAISED" });
  }
  for (let i = 0; i < o.incidents; i++) {
    const n = String(i + 1).padStart(3, "0");
    ns.push({
      id: `INC-2026-${n}`,
      label: "press losing pressure mid-stroke, oil on the floor and a second line of words to wrap over",
      type: "Incident",
    });
    es.push({ id: `o${i}`, source: `INC-2026-${n}`, target: id, rel: "OCCURRED_ON" });
    // each breakdown started with an alarm
    if (o.alarms) es.push({ id: `t${i}`, source: `INC-2026-${n}`, target: `E-${200 + (i % o.alarms)}`, rel: "TRIGGERED_BY" });
  }
  return { nodes: ns, edges: es };
}

const hp = machine("HP-01", { comps: 3, alarms: 6, incidents: 17 });
const cnc = machine("CNC-01", { comps: 5, alarms: 9, incidents: 24 });
// a node that is two steps from the machine: it is never drawn
hp.nodes.push({ id: "FM-SEAL", label: "main cylinder seal wear", type: "FailureMode" });
hp.edges.push({ id: "i", source: "E-201", target: "FM-SEAL", rel: "INDICATES" });

const plan = (
  d: { nodes: ExNode[]; edges: ExEdge[] } = hp,
  centre = "HP-01",
  expanded: string[] = [],
): ExplorerPlan => explorerPlan({ ...d, centre, wordOf: word, forward, expanded: new Set(expanded), measure });

const INC_KEY = "i:OCCURRED_ON:Incident";

describe("the thickness of a drop", () => {
  test("one item is 2 px, twenty or more is 6 px, and it grows with the count between", () => {
    expect(dropPx(1)).toBe(2);
    expect(dropPx(20)).toBeCloseTo(6, 10);
    expect(dropPx(200)).toBe(6);
    expect(dropPx(3)).toBeGreaterThan(dropPx(2));
    expect(dropPx(6)).toBeGreaterThan(dropPx(3));
    expect(dropPx(17)).toBeGreaterThan(dropPx(6));
    expect(dropPx(17)).toBeLessThan(6);
    expect(dropPx(2)).toBeCloseTo(2 + 4 / Math.log2(20), 10);
  });
});

describe("the words of a branch", () => {
  test("a link out reads as the ontology says it, without the leading 'is'", () => {
    expect(branchWords("HAS_COMPONENT", true, "Component", forward)).toBe("built from");
    expect(branchWords("INDICATES", true, "FailureMode", forward)).toBe("usually means");
  });
  test("a machine's alarms read as 'raised alarm'", () => {
    expect(branchWords("RAISED", true, "AlarmCode", forward)).toBe("raised alarm");
  });
  test("a link in reads from the other end", () => {
    expect(branchWords("HAS_MACHINE", false, "Line", forward)).toBe("belongs to");
    expect(branchWords("RAISED", false, "Asset", forward)).toBe("was raised by");
    expect(branchWords("HAS_COMPONENT", false, "Asset", forward)).toBe("part of");
  });
  test("every branch of breakdowns reads 'past incidents'", () => {
    for (const rel of ["OCCURRED_ON", "TRIGGERED_BY", "ROOT_CAUSE", "USED"]) {
      expect(branchWords(rel, false, "Incident", forward)).toBe("past incidents");
    }
  });
});

describe("the count of a branch", () => {
  test("says the number and what it counts, one or many", () => {
    expect(countWords(1, "Line", word)).toBe("1 line");
    expect(countWords(3, "Component", word)).toBe("3 components");
    expect(countWords(6, "AlarmCode", word)).toBe("6 alarms");
    expect(countWords(17, "Incident", word)).toBe("17 incidents");
    expect(countWords(1, "Procedure", word)).toBe("1 fix");
    expect(countWords(2, "Procedure", word)).toBe("2 fixes");
    expect(countWords(2, "FailureMode", word)).toBe("2 root causes");
  });
  test("a type it has no word for is said by the word of the ontology", () => {
    expect(countWords(2, "Bin", word)).toBe("2 bins");
    expect(countWords(1, "Bin", word)).toBe("1 bin");
  });
});

describe("the branches of a machine", () => {
  test("one for each kind of link, the lightest on the left and the heaviest on the right", () => {
    const p = plan();
    expect(p.pills.map((x) => x.words)).toEqual(["belongs to", "built from", "raised alarm", "past incidents"]);
    expect(p.pills.map((x) => x.countText)).toEqual(["1 line", "3 components", "6 alarms", "17 incidents"]);
    expect(p.pills.map((x) => x.count)).toEqual([1, 3, 6, 17]);
    const xs = p.pills.map((x) => x.x + x.w / 2);
    expect([...xs].sort((a, b) => a - b)).toEqual(xs);
  });

  test("a tie in the count is ordered by the words", () => {
    const d = machine("HP-01", { comps: 2, alarms: 2, incidents: 0 });
    const p = plan(d);
    expect(p.pills.map((x) => x.words)).toEqual(["belongs to", "built from", "raised alarm"]);
    expect(p.pills.map((x) => x.count)).toEqual([1, 2, 2]);
  });

  test("the past incidents have the clock and no other branch does", () => {
    const p = plan();
    expect(p.pills.filter((x) => x.past).map((x) => x.words)).toEqual(["past incidents"]);
    expect(p.pills.map((x) => x.type)).toEqual(["Line", "Component", "AlarmCode", "Incident"]);
  });

  test("a pill is a lighter, smaller thing than a card, and is not a fact", () => {
    const p = plan();
    const card = p.magnets.find((m) => !m.centre)!;
    for (const pill of p.pills) {
      expect(pill.h).toBeLessThan(card.h);
      expect(pill.w).toBeLessThanOrEqual(MAGNET_W);
    }
    expect(p.pills.map((x) => x.id)).toEqual(p.pills.map((x) => `pill:${x.branch}`));
  });

  test("another centre gets its own branches by the same rule", () => {
    const a = plan(hp, "E-201");
    expect(a.pills.map((x) => x.words).sort()).toEqual(["past incidents", "usually means", "was raised by"]);
    expect(a.pills.find((x) => x.words === "was raised by")).toMatchObject({ countText: "1 machine", type: "Asset" });
    const inc = plan(hp, "INC-2026-001");
    expect(inc.pills.map((x) => x.words).sort()).toEqual(["happened on", "started with"]);
  });

  test("a node with no links has no branches and stands alone", () => {
    const p = plan({ nodes: [{ id: "X-1", label: "", type: "Asset" }], edges: [] }, "X-1");
    expect(p.magnets).toHaveLength(1);
    expect(p.pills).toHaveLength(0);
    expect(p.wires).toHaveLength(0);
  });
});

describe("the tree", () => {
  test("the centre card is at the top, centred over its branches", () => {
    const p = plan();
    const c = p.magnets.find((m) => m.centre)!;
    expect(c.id).toBe("HP-01");
    expect(c.y - c.h / 2).toBe(0);
    const first = p.pills[0]!;
    const last = p.pills[p.pills.length - 1]!;
    const middle = (first.x + first.w / 2 + last.x + last.w / 2) / 2;
    expect(c.x + c.w / 2).toBeCloseTo(middle, 6);
    for (const x of p.pills) expect(x.y - x.h / 2).toBeGreaterThan(c.y + c.h / 2);
  });

  test("the leaves are stacked in one column under their pill, newest breakdown first", () => {
    const p = plan();
    const pill = p.pills.find((x) => x.words === "past incidents")!;
    const leaves = p.magnets.filter((m) => m.branch === pill.branch);
    expect(leaves.map((m) => m.id)).toEqual(["INC-2026-017", "INC-2026-016", "INC-2026-015", "INC-2026-014"]);
    for (const m of leaves) expect(m.x + m.w / 2).toBeCloseTo(pill.x + pill.w / 2, 6);
    for (let i = 1; i < leaves.length; i++) expect(leaves[i]!.y).toBeGreaterThan(leaves[i - 1]!.y);
    expect(leaves[0]!.y - leaves[0]!.h / 2).toBeGreaterThan(pill.y + pill.h / 2);
  });

  test("a branch of more than four shows four and a '+N more' chip, which opens the rest", () => {
    const p = plan();
    // the six alarms are cut as well: "+2 more"
    expect(p.more.map((c) => [c.branch, c.count])).toEqual([["o:RAISED:AlarmCode", 2], [INC_KEY, 13]]);
    const chip = p.more.find((c) => c.branch === INC_KEY)!;
    expect(chip).toMatchObject({ id: `more:${INC_KEY}`, count: 13, label: "+13 more" });
    const lastLeaf = p.magnets.filter((m) => m.branch === INC_KEY).at(-1)!;
    expect(chip.y).toBeGreaterThan(lastLeaf.y + lastLeaf.h / 2);
    const open = plan(hp, "HP-01", [INC_KEY]);
    expect(open.magnets.filter((m) => m.branch === INC_KEY)).toHaveLength(17);
    expect(open.more.map((c) => c.branch)).toEqual(["o:RAISED:AlarmCode"]);
  });

  test("one leaf that would be left out is drawn, because '+1 more' says no more than the leaf", () => {
    const five = plan(machine("HP-01", { comps: 3, alarms: 6, incidents: 5 }));
    expect(five.magnets.filter((m) => m.type === "Incident")).toHaveLength(5);
    expect(five.more.some((c) => c.branch === INC_KEY)).toBe(false);
    const six = plan(machine("HP-01", { comps: 3, alarms: 6, incidents: 6 }));
    expect(six.magnets.filter((m) => m.type === "Incident")).toHaveLength(4);
    expect(six.more.find((c) => c.branch === INC_KEY)!.count).toBe(2);
  });

  test("only what is one link from the centre is drawn", () => {
    const p = plan();
    expect(p.magnets.some((m) => m.id === "FM-SEAL")).toBe(false);
  });

  test("a leaf is linked to one branch only, by the first edge between it and the centre", () => {
    const d = machine("HP-01", { comps: 1, alarms: 1, incidents: 0 });
    d.edges.push({ id: "dup", source: "HP-01", target: "E-200", rel: "OTHER" });
    const p = plan(d);
    expect(p.magnets.filter((m) => m.id === "E-200")).toHaveLength(1);
    expect(p.lines.filter((l) => l.to === "E-200")).toHaveLength(1);
  });
});

describe("the lines of the tree", () => {
  test("a trunk from the centre to a rail, a drop to each pill, and a spine from each pill to its leaves", () => {
    const p = plan();
    const kinds = (k: string) => p.wires.filter((w) => w.kind === k);
    expect(kinds("trunk")).toHaveLength(1);
    expect(kinds("trunk")[0]!.from).toBe("HP-01");
    expect(kinds("drop")).toHaveLength(p.pills.length);
    expect(kinds("spine")).toHaveLength(p.pills.length);
    for (const pill of p.pills) {
      expect(kinds("drop").find((w) => w.to === pill.id)).toBeDefined();
      expect(kinds("spine").find((w) => w.from === pill.id)).toBeDefined();
    }
  });

  test("a drop is as thick as its count says, and the rest are thin", () => {
    const p = plan();
    for (const pill of p.pills) {
      expect(p.wires.find((w) => w.kind === "drop" && w.to === pill.id)!.px).toBeCloseTo(dropPx(pill.count), 10);
    }
    const drops = p.pills.map((x) => p.wires.find((w) => w.kind === "drop" && w.to === x.id)!.px);
    expect([...drops].sort((a, b) => a - b)).toEqual(drops);
    for (const w of p.wires.filter((x) => x.kind !== "drop")) expect(w.px).toBe(2);
  });

  test("the rail runs through the trunk and the foot of every drop, and every wire joins nodes that exist", () => {
    const p = plan();
    const ids = new Set([...p.magnets.map((m) => m.id), ...p.more.map((m) => m.id), ...p.pills.map((x) => x.id), ...p.junctions.map((j) => j.id)]);
    for (const w of p.wires) {
      expect(ids.has(w.from)).toBe(true);
      expect(ids.has(w.to)).toBe(true);
    }
    const rail = p.junctions.map((j) => j.y);
    expect(new Set(rail).size).toBe(1);
    const centre = p.magnets.find((m) => m.centre)!;
    expect(p.junctions.some((j) => Math.abs(j.x - (centre.x + centre.w / 2)) < 1e-6)).toBe(true);
    for (const pill of p.pills) expect(p.junctions.some((j) => Math.abs(j.x - (pill.x + pill.w / 2)) < 1e-6)).toBe(true);
    // the rail is one line from the leftmost junction to the rightmost
    const xs = p.junctions.map((j) => j.x).sort((a, b) => a - b);
    expect(p.wires.filter((w) => w.kind === "rail")).toHaveLength(xs.length - 1);
  });

  test("a single branch hangs straight below the centre with no rail", () => {
    const p = plan(machine("HP-01", { comps: 0, alarms: 0, incidents: 0 }));
    expect(p.pills).toHaveLength(1);
    expect(p.wires.filter((w) => w.kind === "rail")).toHaveLength(0);
    expect(p.junctions).toHaveLength(1);
  });

  test("one line for each leaf drawn, from the centre, with its words read from there", () => {
    const p = plan();
    expect(p.lines).toHaveLength(p.magnets.length - 1);
    expect(p.lines.every((l) => l.from === "HP-01")).toBe(true);
    expect(p.lines.find((l) => l.to === "E-201")).toMatchObject({ id: "a1", text: "raised" });
    expect(p.lines.find((l) => l.to === "INC-2026-017")).toMatchObject({ text: "had" });
    expect(p.lines.find((l) => l.to === "LINE-03")).toMatchObject({ text: "belongs to" });
  });
});

describe("the cross-lines", () => {
  test("a real edge between two drawn leaves of different branches is a cross-line", () => {
    const p = plan();
    // INC-2026-016 started with E-203; the four newest breakdowns and the first four alarms are drawn
    expect(p.cross.length).toBeGreaterThan(0);
    const t15 = p.cross.find((c) => c.id === "t15");
    expect(t15).toBeDefined();
    expect(t15).toMatchObject({ from: "INC-2026-016", to: "E-203" });
    // INC-2026-017 started with E-204, which is in "+2 more": no cross-line
    expect(p.cross.some((c) => c.id === "t16")).toBe(false);
  });

  test("they run only between drawn leaves, of different branches", () => {
    for (const d of [hp, cnc]) {
      const p = plan(d, d.nodes[0]!.id);
      const branch = new Map(p.magnets.map((m) => [m.id, m.branch]));
      expect(p.cross.length).toBeGreaterThan(0);
      for (const c of p.cross) {
        expect(branch.has(c.from)).toBe(true);
        expect(branch.has(c.to)).toBe(true);
        expect(branch.get(c.from)).not.toBe(branch.get(c.to));
        expect(branch.get(c.from)).not.toBeNull();
        expect(branch.get(c.to)).not.toBeNull();
      }
    }
  });

  test("a leaf left in a '+N more' chip has no cross-line, until its branch is opened", () => {
    const shut = plan();
    expect(shut.cross.some((c) => c.id === "t0")).toBe(false);
    const open = plan(hp, "HP-01", [INC_KEY]);
    expect(open.cross.some((c) => c.id === "t0")).toBe(true);
    expect(open.cross.length).toBeGreaterThan(shut.cross.length);
  });

  test("an edge inside one branch, or to the centre, is not a cross-line", () => {
    const d = machine("HP-01", { comps: 0, alarms: 3, incidents: 0, line: false });
    d.edges.push({ id: "aa", source: "E-200", target: "E-201", rel: "SIBLING" });
    const p = plan(d);
    expect(p.cross).toHaveLength(0);
    expect(p.lines).toHaveLength(3);
  });

  test("two edges between the same two leaves are one cross-line", () => {
    const d = machine("HP-01", { comps: 0, alarms: 2, incidents: 1, line: false });
    d.edges.push({ id: "again", source: "INC-2026-001", target: "E-200", rel: "ROOT_CAUSE" });
    const p = plan(d);
    expect(p.cross.filter((c) => c.to === "E-200")).toHaveLength(1);
  });
});

describe("room for every box", () => {
  const boxes = (p: ExplorerPlan) => [
    ...p.magnets.map((m) => ({ id: m.id, x1: m.x, x2: m.x + m.w, y1: m.y - m.h / 2, y2: m.y + m.h / 2 })),
    ...p.more.map((m) => ({ id: m.id, x1: m.x, x2: m.x + m.w, y1: m.y - m.h / 2, y2: m.y + m.h / 2 })),
    ...p.pills.map((m) => ({ id: m.id, x1: m.x, x2: m.x + m.w, y1: m.y - m.h / 2, y2: m.y + m.h / 2 })),
  ];
  const meet = (a: { x1: number; x2: number; y1: number; y2: number }, b: typeof a) =>
    a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;

  const six = (() => {
    const ns: ExNode[] = [{ id: "FM-X", label: "worn seal", type: "FailureMode" }];
    const es: ExEdge[] = [];
    const add = (type: string, id: string, rel: string, out: boolean) => {
      ns.push({ id, label: `${type} ${id} with some words of description`, type });
      es.push(out ? { id: `e-${id}`, source: "FM-X", target: id, rel } : { id: `e-${id}`, source: id, target: "FM-X", rel });
    };
    for (let i = 0; i < 2; i++) add("Component", `K-${i}`, "AFFECTS", true);
    add("Procedure", "PROC-AA-01", "RESOLVED_BY", true);
    for (let i = 0; i < 3; i++) add("AlarmCode", `E-30${i}`, "INDICATES", false);
    for (let i = 0; i < 9; i++) add("Incident", `INC-2026-00${i}`, "ROOT_CAUSE", false);
    add("Part", "SEAL-1", "OTHER", true);
    add("Document", "DOC-aaaaaaaa", "CITED_IN", true);
    return { nodes: ns, edges: es };
  })();

  const cases: [string, ExplorerPlan][] = [
    ["HP-01", plan(hp)],
    ["HP-01 with every incident", plan(hp, "HP-01", [INC_KEY])],
    ["CNC-01", plan(cnc, "CNC-01")],
    ["CNC-01 with every incident", plan(cnc, "CNC-01", ["i:OCCURRED_ON:Incident", "o:RAISED:AlarmCode", "o:HAS_COMPONENT:Component"])],
    ["a root cause with six branches", plan(six, "FM-X")],
  ];

  for (const [name, p] of cases) {
    test(`no two boxes meet: ${name}`, () => {
      const all = boxes(p);
      for (let i = 0; i < all.length; i++) {
        for (let j = i + 1; j < all.length; j++) expect(meet(all[i]!, all[j]!)).toBe(false);
      }
    });
  }

  test("the six-branch centre has six columns side by side", () => {
    const p = cases[4]![1];
    expect(p.pills).toHaveLength(6);
    expect(new Set(p.pills.map((x) => Math.round(x.x + x.w / 2))).size).toBe(6);
    // the columns do not overlap in x either
    const cols = p.pills.map((x) => x.x + x.w / 2).sort((a, b) => a - b);
    for (let i = 1; i < cols.length; i++) expect(cols[i]! - cols[i - 1]!).toBeGreaterThanOrEqual(LEAF_W);
  });
});

describe("what a magnet says", () => {
  const magnet = (id: string, centre = "HP-01") => plan(hp, centre, [INC_KEY]).magnets.find((m) => m.id === id)!;
  test("a leaf says its ID and its name under it, and no type: its pill says that", () => {
    expect(magnet("E-201")).toMatchObject({ kind: "", name: ["E-201"] });
    expect(magnet("E-201").detail.join(" ").startsWith("Alarm 1 pressure loss")).toBe(true);
  });
  test("the centre keeps its type in words", () => {
    expect(plan().magnets.find((m) => m.centre)).toMatchObject({ kind: "Machine", name: ["HP-01"] });
  });
  test("a root cause is its name, as the answer graph says it, with no ID", () => {
    const m = plan(hp, "E-201").magnets.find((x) => x.id === "FM-SEAL")!;
    expect(m).toMatchObject({ kind: "", detail: [] });
    expect(m.name.join(" ")).toBe("Main cylinder seal wear");
  });
  test("a long description is held to two lines", () => {
    const m = magnet("INC-2026-001");
    expect(m.detail.length).toBeLessThanOrEqual(2);
    expect(m.detail[m.detail.length - 1]!.endsWith("...")).toBe(true);
  });
  test("a magnet is as high as its lines need", () => {
    expect(magnet("INC-2026-001").h).toBeGreaterThan(magnet("LINE-03").h);
  });
  test("the centre is the one magnet marked as it, and has no branch", () => {
    const p = plan();
    expect(p.magnets.filter((m) => m.centre).map((m) => [m.id, m.branch])).toEqual([["HP-01", null]]);
  });
});

describe("a centre that is not there", () => {
  test("gives an empty plan", () => {
    expect(plan(hp, "NOPE")).toEqual({ magnets: [], more: [], pills: [], junctions: [], wires: [], lines: [], cross: [] });
  });
});

describe("what the pane shows", () => {
  test("the centre, then the ids of the nodes drawn, of the lines of the tree and of the cross-lines", () => {
    const p = plan();
    const shown = shownOf(p);
    expect(shown.centre).toBe("HP-01");
    expect(shown.ids).toEqual([...p.magnets.map((m) => m.id), ...p.lines.map((l) => l.id), ...p.cross.map((l) => l.id)]);
    expect(shown.ids).toContain("HP-01");
    expect(shown.ids).toContain("a2");
    expect(shown.ids).toContain("t15");
  });
  test("a pill, a chip and a node cut to a chip are not shown, and neither is its line", () => {
    const shown = shownOf(plan());
    expect(shown.ids.some((id) => id.startsWith("pill:") || id.startsWith("more:") || id.startsWith("j:"))).toBe(false);
    const drawn = plan().magnets.filter((m) => m.type === "Incident").length;
    expect(drawn).toBe(4);
    expect(shown.ids.filter((id) => id.startsWith("INC-"))).toHaveLength(drawn);
    expect(shown.ids.filter((id) => /^o\d+$/.test(id))).toHaveLength(drawn);
  });
  test("a plan with no centre shows nothing", () => {
    expect(shownOf(plan(hp, "NOPE"))).toEqual({ centre: "", ids: [] });
  });
});

describe("compact leaf cards", () => {
  // widths in Chrome, in Barlow 600 at 17 px, of the longest IDs of the sample plant
  const REAL: Record<string, number> = {
    "HP-01-HYDPUMP": 125,
    "CNC-01-TOOLCHG": 133,
    "CNC-01-COOLANT": 135,
    "INC-2026-001": 103,
  };
  const real = (text: string, px: number, weight: number) => (px === 17 && weight === 600 && REAL[text] !== undefined ? REAL[text]! : measure(text, px));
  const longIds = (() => {
    const ns: ExNode[] = [{ id: "CNC-01", label: "Machining centre", type: "Asset" }];
    const es: ExEdge[] = [];
    for (const id of ["HP-01-HYDPUMP", "CNC-01-TOOLCHG", "CNC-01-COOLANT"]) {
      ns.push({ id, label: "A part of the machine with some words of description", type: "Component" });
      es.push({ id: `e-${id}`, source: "CNC-01", target: id, rel: "HAS_COMPONENT" });
    }
    ns.push({ id: "INC-2026-001", label: "press losing pressure mid-stroke", type: "Incident" });
    es.push({ id: "e-inc", source: "INC-2026-001", target: "CNC-01", rel: "OCCURRED_ON" });
    return { nodes: ns, edges: es };
  })();
  const leaves = explorerPlan({ ...longIds, centre: "CNC-01", wordOf: word, forward, expanded: new Set(), measure: real }).magnets.filter((m) => !m.centre);

  test("a leaf is narrower than the centre card, and the centre card is as wide as it was", () => {
    expect(LEAF_W).toBeLessThan(204);
    expect(MAGNET_W).toBe(204);
    const p = plan();
    for (const m of p.magnets) expect(m.w).toBe(m.centre ? MAGNET_W : LEAF_W);
    for (const c of p.more) expect(c.w).toBe(LEAF_W);
  });

  test("the longest real IDs still fit on one line of a leaf", () => {
    expect(leaves.map((m) => m.id).sort()).toEqual(["CNC-01-COOLANT", "CNC-01-TOOLCHG", "HP-01-HYDPUMP", "INC-2026-001"]);
    for (const m of leaves) {
      expect(m.name).toEqual([m.id]);
      expect(REAL[m.id]!).toBeLessThanOrEqual(LEAF_W - LEAF_OVER);
    }
  });

  test("a label wraps to at most two lines of the room that is left", () => {
    for (const m of leaves) expect(m.detail.length).toBeLessThanOrEqual(2);
    for (const m of plan(hp, "HP-01", [INC_KEY]).magnets.filter((x) => !x.centre)) {
      expect(m.name.length).toBeLessThanOrEqual(2);
      expect(m.detail.length).toBeLessThanOrEqual(2);
    }
  });

  test("a leaf is as high as its name and label need, with no row for a type", () => {
    for (const m of leaves) {
      const lines = m.name.length + m.detail.length;
      expect(lines).toBeGreaterThan(0);
      expect(m.h).toBe(9 + 20 * m.name.length + 18 * m.detail.length + 10);
    }
    const centre = plan().magnets.find((m) => m.centre)!;
    expect(centre.h).toBe(9 + 16 + 20 + 18 + 10);
  });

  test("the rows of a leaf start at its name; the centre's start at its type", () => {
    const m = leaves.find((x) => x.detail.length === 2)!;
    const rows = leafRows({ h: m.h, nameLines: m.name, detailLines: m.detail });
    expect(rows.kind).toBeNull();
    expect(rows.name).toBe(-m.h / 2 + 9 + 10);
    expect(rows.detail).toBe(-m.h / 2 + 9 + 20 + 18);
    const c = plan().magnets.find((x) => x.centre)!;
    expect(textRows({ h: c.h, nameLines: c.name, detailLines: c.detail }).kind).toBe(-c.h / 2 + 9 + 8);
  });

  test("the columns are a leaf and a gap apart, and the gap is the one measured", () => {
    expect(COL_GAP).toBe(30);
    const xs = plan().pills.map((x) => x.x + x.w / 2);
    for (let i = 1; i < xs.length; i++) expect(xs[i]! - xs[i - 1]!).toBe(LEAF_W + COL_GAP);
  });

  test("at the explorer's zoom floor in a 541 px pane, three whole columns of the HP-01 tree fit", () => {
    const p = plan();
    const left = p.magnets.filter((m) => !m.centre).map((m) => m.x);
    const first = Math.min(...left);
    const view = (541 - 2 * 12) / EXPLORER_FLOOR;
    const edges = [...new Set(left)].sort((a, b) => a - b).map((x) => x + LEAF_W);
    expect(edges.filter((e) => e - first <= view).length).toBeGreaterThanOrEqual(3);
  });
});

describe("the columns of the first view", () => {
  test("one for each branch, a leaf wide, with the count of the branch as its weight", () => {
    const p = plan();
    const cols = columnsOf(p);
    expect(cols.map((c) => c.weight)).toEqual([1, 3, 6, 17]);
    for (const c of cols) expect(c.x2 - c.x1).toBe(LEAF_W);
    expect(cols.map((c) => c.x1)).toEqual(p.pills.map((x) => x.x + x.w / 2 - LEAF_W / 2));
    for (const leaf of p.magnets.filter((m) => !m.centre)) expect(cols.some((c) => c.x1 === leaf.x)).toBe(true);
  });

  test("the first view of the HP-01 tree at 541 px has the centre card, three whole columns and the heaviest branch", () => {
    const p = plan();
    const cols = columnsOf(p);
    const c = p.magnets.find((m) => m.centre)!;
    const box = { x1: Math.min(c.x, cols[0]!.x1), y1: 0, x2: Math.max(c.x + c.w, cols.at(-1)!.x2) };
    const at = startPan({ w: 541 }, box, EXPLORER_FLOOR, c.x + c.w / 2, 12, { columns: cols, centre: { x1: c.x, x2: c.x + c.w } });
    const v1 = (12 - at.x) / EXPLORER_FLOOR;
    const v2 = (541 - 12 - at.x) / EXPLORER_FLOOR;
    const whole = cols.filter((x) => x.x1 >= v1 - 1e-9 && x.x2 <= v2 + 1e-9);
    expect(whole.length).toBeGreaterThanOrEqual(3);
    expect(cols.some((x) => Math.abs(x.x1 - v1) < 1e-9)).toBe(true);
    expect(whole.map((x) => x.weight)).toContain(17);
    expect(c.x).toBeGreaterThanOrEqual(v1);
    expect(c.x + c.w).toBeLessThanOrEqual(v2);
  });
});

