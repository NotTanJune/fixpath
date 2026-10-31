import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { askReducer, initialAsk } from "../src/lib/askReducer";
import { buildAskGraph, OVER, DETAIL_PX, NAME_PX, TYPE_PX, textRows, type GraphView } from "../src/lib/askGraph";
import { EDGE_PX, edgeSegments } from "../src/lib/graphLabels";
import { viewOf } from "../src/lib/graphView";
import { estimate, wrapLines } from "../src/lib/textMetrics";
import type { Ontology } from "../src/lib/types";

const ontology = JSON.parse(
  readFileSync(new URL("../../data/recordings/api/ontology.json", import.meta.url), "utf8"),
) as Ontology;

function finalState(n: number, upTo?: (e: any) => boolean) {
  const path = new URL(`../../data/recordings/api/ask_${n}.jsonl`, import.meta.url);
  let state = askReducer(initialAsk, { type: "start", question: "q" });
  for (const line of readFileSync(path, "utf8").split("\n").filter(Boolean)) {
    const e = JSON.parse(line);
    state = askReducer(state, { type: "event", name: e.event, data: e.data });
    if (upTo?.(e)) break;
  }
  return state;
}

const build = (n: number, view: GraphView = "path", facts: any = null) =>
  buildAskGraph(finalState(n), { view, ontology, facts, measure: estimate });

const FACTS = { message: "Main cylinder pressure loss", machineName: "Hydraulic press" };
const centre = (n: { x: number; w: number }) => n.x + n.w / 2;

describe("the answer path view", () => {
  test("holds exactly the path and the page that cites the fix", () => {
    const { nodes } = build(1, "path");
    expect(nodes.map((n) => n.id).sort()).toEqual(
      [
        "HP-01",
        "E-217",
        "FM-MAIN-CYLINDER-SEAL-WEAR",
        "PROC-HP-04",
        "SEAL-KIT-50",
        "B-12",
        "manuals/BH-250 service manual.pdf#5",
      ].sort(),
    );
    expect(nodes.every((n) => n.layer === "path")).toBe(true);
    const page = nodes.find((n) => n.type === "Document");
    expect(page?.label).toBe("BH-250 manual, page 5");
  });

  test("runs down one column, in the order of the answer", () => {
    for (const n of [1, 2, 3, 4]) {
      const { nodes } = build(n, "path");
      const path = nodes.filter((x) => x.lit);
      expect(path.map((x) => x.lit)).toEqual(["alarm", "cause", "fix", "part", "bin"]);
      const column = nodes.filter((x) => !x.side);
      expect(column.map((x) => x.type)).toEqual(["Asset", "AlarmCode", "FailureMode", "Procedure", "Part", "Bin"]);
      expect(new Set(column.map((x) => Math.round(centre(x)))).size).toBe(1);
      const ys = column.map((x) => x.y);
      expect(ys).toEqual([...ys].sort((a, b) => a - b));
      for (let i = 1; i < column.length; i++) {
        // room between two magnets for the word of the edge between them
        expect(column[i]!.y - column[i]!.h / 2 - (column[i - 1]!.y + column[i - 1]!.h / 2)).toBeGreaterThanOrEqual(40);
      }
    }
  });

  test("the page that cites the fix sits beside it, level with it", () => {
    const { nodes } = build(1, "path");
    const fix = nodes.find((n) => n.id === "PROC-HP-04")!;
    const page = nodes.find((n) => n.type === "Document")!;
    expect(page.side).toBe(true);
    expect(page.y).toBe(fix.y);
    expect(page.x).toBeGreaterThan(fix.x + fix.w);
  });

  test("the lit edges are the path, in order", () => {
    const { nodes, edges } = build(1, "path");
    expect(nodes.filter((n) => n.lit).map((n) => [n.lit, n.id])).toEqual([
      ["alarm", "E-217"],
      ["cause", "FM-MAIN-CYLINDER-SEAL-WEAR"],
      ["fix", "PROC-HP-04"],
      ["part", "SEAL-KIT-50"],
      ["bin", "B-12"],
    ]);
    expect(edges.filter((e) => e.lits.length).map((e) => e.rel)).toEqual([
      "INDICATES",
      "RESOLVED_BY",
      "REQUIRES",
      "STORED_IN",
    ]);
  });
});

describe("the Everything view", () => {
  test("adds the other causes, fixes, breakdowns, parts and pages, and keeps the path", () => {
    const path = build(1, "path").nodes;
    const all = build(1, "all").nodes;
    const ids = all.map((n) => n.id);
    for (const n of path) expect(ids).toContain(n.id);
    for (const id of [
      "FM-RELIEF-VALVE-MISADJUSTMENT",
      "PROC-HP-06",
      "OIL-HLP46-20L",
      "INC-2026-060",
      "INC-2026-034",
      "INC-2026-031",
    ]) {
      expect(ids).toContain(id);
    }
    expect(all.filter((n) => n.type === "Document").length).toBeGreaterThan(1);
    const extra = all.filter((n) => n.layer === "extra").map((n) => n.id);
    expect(extra).toContain("PROC-HP-06");
    expect(extra.every((id) => !path.some((p) => p.id === id))).toBe(true);
    expect(all.find((n) => n.id === "FM-RELIEF-VALVE-MISADJUSTMENT")?.dashed).toBe(true);
    expect(all.find((n) => n.id === "PROC-HP-06")?.dashed).toBe(true);
  });

  test("keeps the path in its column and its order, and spaces it only as far as its blocks need", () => {
    const path = build(1, "path").nodes.filter((x) => !x.side);
    const all = build(1, "all").nodes.filter((x) => !x.side);
    expect(all.map((n) => n.id)).toEqual(path.map((n) => n.id));
    for (const n of path) {
      const same = all.find((x) => x.id === n.id)!;
      expect(Math.round(centre(same))).toBe(Math.round(centre(n)));
    }
    const span = (nodes: typeof path) => nodes[nodes.length - 1]!.y - nodes[0]!.y;
    expect(span(all)).toBeGreaterThanOrEqual(span(path));
  });

  test("the page that cites the fix stays level with the fix in both views", () => {
    for (const n of [1, 2, 3, 4]) {
      for (const view of ["path", "all"] as const) {
        const { nodes } = build(n, view);
        const fix = nodes.find((x) => x.type === "Procedure" && !x.side)!;
        const page = nodes.find((x) => x.type === "Document" && x.layer === "path")!;
        expect(page.y).toBe(fix.y);
      }
    }
  });

  test("every side magnet sits within reach of the magnet it hangs from", () => {
    // its block is level with that magnet, so no line runs more than a block's height
    for (const n of [1, 2, 3, 4]) {
      const { nodes, edges } = build(n, "all");
      const at = new Map(nodes.map((x) => [x.id, x]));
      for (const e of edges) {
        const a = at.get(e.source)!;
        const b = at.get(e.target)!;
        if (a.side === b.side) continue;
        expect(Math.abs(a.y - b.y)).toBeLessThan(220);
      }
    }
  });

  test("the lines of a block turn together, in the middle of the gap", () => {
    const { edges, gap } = build(1, "all");
    const turns = edges.filter((e) => e.turn !== undefined).map((e) => e.turn);
    expect(turns.length).toBeGreaterThan(3);
    expect(new Set(turns)).toEqual(new Set([Math.round(gap / 2)]));
  });

  test("no side magnet overlaps another", () => {
    for (const n of [1, 2, 3, 4]) {
      const side = build(n, "all").nodes.filter((x) => x.side);
      for (const a of side)
        for (const b of side) {
          if (a.id >= b.id) continue;
          const apart = a.y + a.h / 2 <= b.y - b.h / 2 || b.y + b.h / 2 <= a.y - a.h / 2;
          expect(apart).toBe(true);
        }
    }
  });
});

describe("every node says what it is, every edge what it means", () => {
  test("a node has its type in plain words, a name and no cut text", () => {
    const kinds = new Map<string, string>([
      ["Asset", "Machine"],
      ["AlarmCode", "Alarm"],
      ["FailureMode", "Root cause"],
      ["Procedure", "Fix"],
      ["Part", "Spare part"],
      ["Bin", "Bin"],
      ["Incident", "Past breakdown"],
      ["Document", "Source"],
    ]);
    for (const n of [1, 2, 3, 4]) {
      for (const view of ["path", "all"] as const) {
        for (const node of build(n, view, FACTS).nodes) {
          expect(node.kind).toBe(kinds.get(node.type) as string);
          expect(node.label.trim().length).toBeGreaterThan(0);
          expect(node.nameLines.join(" ")).toBe(node.label);
          for (const line of [node.label, node.detail, ...node.nameLines, ...node.detailLines]) {
            expect(line).not.toMatch(/…|\.\.\./);
          }
        }
      }
    }
  });

  test("says the useful short line: the alarm's message, the history, the stock, the bin", () => {
    const at = (id: string) => build(1, "all", FACTS).nodes.find((n) => n.id === id)!;
    expect(at("HP-01").detail).toBe("Hydraulic press");
    expect(at("E-217").detail).toBe("Main cylinder pressure loss");
    expect(at("FM-MAIN-CYLINDER-SEAL-WEAR").detail).toBe("6 of 6 past stops");
    expect(at("PROC-HP-04").detail).toBe("Replace main cylinder seal kit");
    expect(at("SEAL-KIT-50").detail).toBe("1 needed, 3 in stock");
    expect(at("B-12").detail).toBe("3 on hand");
    expect(at("FM-RELIEF-VALVE-MISADJUSTMENT").detail).toBe("0 of 6 past stops");
  });

  test("a cited page reads as the manual and its page, never as p.3", () => {
    for (const n of [1, 2, 3, 4]) {
      for (const view of ["path", "all"] as const) {
        const docs = build(n, view).nodes.filter((x) => x.type === "Document");
        expect(docs.length).toBeGreaterThan(0);
        for (const d of docs) expect(d.label).toMatch(/^\S.* manual, page \d+$/);
        expect(new Set(docs.map((d) => d.label)).size).toBe(docs.length);
      }
    }
    expect(build(2, "path").nodes.find((x) => x.type === "Document")?.label).toBe("Kessler KX-500 manual, page 5");
  });

  test("every edge has its relation in plain words", () => {
    const words: Record<string, string> = {
      RAISED: "raised",
      INDICATES: "usually means",
      RESOLVED_BY: "fixed by",
      REQUIRES: "needs",
      STORED_IN: "kept in",
      CITED_IN: "cited in",
      ROOT_CAUSE: "was caused by",
    };
    for (const n of [1, 2, 3, 4]) {
      for (const view of ["path", "all"] as const) {
        const { edges } = build(n, view);
        expect(edges.length).toBeGreaterThan(0);
        for (const e of edges) {
          expect(e.label.trim().length).toBeGreaterThan(0);
          expect(e.label).toBe(words[e.rel] as string);
          expect(e.label).not.toMatch(/…|\.\.\.|_/);
        }
      }
    }
  });

  test("the words come from config/ontology.json, and the UI-made ones are named too", () => {
    expect(ontology.relations?.["RAISED"]?.label).toBe("raised");
    const labels = buildAskGraph(finalState(1), { view: "all", ontology: null, measure: estimate }).edges.map((e) => e.label);
    expect(labels).toContain("kept in");
    expect(labels).toContain("cited in");
    expect(labels.every((l) => l.length > 0)).toBe(true);
  });
});

describe("a magnet is as wide as its text", () => {
  test("holds the widest of its type, name and detail with room for the glyph", () => {
    for (const n of [1, 2, 3, 4]) {
      for (const view of ["path", "all"] as const) {
        for (const node of build(n, view, FACTS).nodes) {
          const widest = Math.max(
            estimate(node.kind.toUpperCase(), TYPE_PX, 600),
            ...node.nameLines.map((l) => estimate(l, NAME_PX, 600)),
            ...node.detailLines.map((l) => estimate(l, DETAIL_PX, 400)),
          );
          expect(node.w).toBeGreaterThanOrEqual(widest + OVER);
        }
      }
    }
  });

  test("a long name wraps at a space and grows the magnet in height, not in width", () => {
    const lines = wrapLines("Hydraulic main cylinder seal wear and scoring", 120, NAME_PX, 600, estimate);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.join(" ")).toBe("Hydraulic main cylinder seal wear and scoring");
  });

  test("a word wider than the room is kept whole", () => {
    expect(wrapLines("ABRASIVE-CONTAMINATION-DAMAGE seal", 60, NAME_PX, 600, estimate)).toEqual([
      "ABRASIVE-CONTAMINATION-DAMAGE",
      "seal",
    ]);
  });
});

describe("a node never moves when later facts arrive", () => {
  test("its centre and height stay, whatever the facts have said so far", () => {
    const early = buildAskGraph(
      finalState(1, (e) => e.event === "tool_call" && e.data.name === "parts_for"),
      { view: "path", ontology, measure: estimate },
    );
    const late = build(1, "path", FACTS);
    for (const n of early.nodes) {
      const same = late.nodes.find((m) => m.id === n.id);
      expect(same).toBeDefined();
      expect([same!.y, same!.h]).toEqual([n.y, n.h]);
      expect(Math.round(centre(same!))).toBe(Math.round(centre(n)));
    }
  });
});

describe("the empty and the guarded", () => {
  test("a clarify answer has an empty graph", () => {
    expect(build(5).nodes).toEqual([]);
  });

  test("no node lies between the two ends of a lit edge in the same column", () => {
    for (const n of [1, 2, 3, 4]) {
      for (const view of ["path", "all"] as const) {
        const { nodes, edges } = build(n, view);
        const byId = new Map(nodes.map((x) => [x.id, x]));
        const lit = edges.filter((e) => e.lits.length);
        expect(lit.length).toBeGreaterThan(0);
        for (const e of lit) {
          const a = byId.get(e.source)!;
          const b = byId.get(e.target)!;
          if (Math.abs(centre(a) - centre(b)) > 1) continue;
          const [lo, hi] = [Math.min(a.y, b.y), Math.max(a.y, b.y)];
          const between = nodes.filter((x) => Math.abs(centre(x) - centre(a)) <= 1 && x.y > lo && x.y < hi);
          expect(between.map((x) => x.id)).toEqual([]);
        }
      }
    }
  });

  test("other parts leave the column of the key part", () => {
    const { nodes } = build(1, "all");
    const key = nodes.find((n) => n.id === "SEAL-KIT-50")!;
    const oil = nodes.find((n) => n.id === "OIL-HLP46-20L")!;
    expect(oil.side).toBe(true);
    expect(Math.round(centre(oil))).not.toBe(Math.round(centre(key)));
  });
});

describe("the view belongs to one answer", () => {
  test("every answer opens on the answer path", () => {
    expect(viewOf(null, 1)).toBe("path");
    expect(viewOf({ run: 1, view: "all" }, 2)).toBe("path");
  });

  test("Everything holds for the answer it was chosen on, while the run goes on", () => {
    const held = { run: 3, view: "all" as GraphView };
    expect(viewOf(held, 3)).toBe("all");
    expect(viewOf(held, 3)).toBe("all"); // facts arriving do not change the run
  });

  test("a new question opens on the path, and so does a saved answer that is reopened", () => {
    const answered = finalState(1);
    const chosen = { run: answered.run, view: "all" as GraphView };
    expect(viewOf(chosen, answered.run)).toBe("all");
    const asked = askReducer(answered, { type: "start", question: "next" });
    expect(asked.run).toBe(answered.run + 1);
    expect(viewOf(chosen, asked.run)).toBe("path");
    const reopened = askReducer(asked, { type: "restore", question: "q", events: [], at: "2026-10-29T09:00:00Z" });
    expect(reopened.run).toBe(asked.run + 1);
    expect(viewOf({ run: asked.run, view: "all" }, reopened.run)).toBe("path");
  });

  test("nothing is kept in storage: a stored Everything from an older visit is not read", () => {
    const calls: string[] = [];
    const g = globalThis as { localStorage?: unknown };
    const before = g.localStorage;
    g.localStorage = { getItem: (k: string) => (calls.push(k), "all"), setItem: (k: string) => void calls.push(k) };
    try {
      expect(viewOf(null, 1)).toBe("path");
    } finally {
      g.localStorage = before;
    }
    expect(calls).toEqual([]);
  });
});

describe("the rows of text in a magnet", () => {
  test("type, name and detail stack inside the magnet and never overlap", () => {
    for (const n of [1, 2, 3, 4]) {
      for (const view of ["path", "all"] as const) {
        for (const node of build(n, view, FACTS).nodes) {
          const rows = textRows(node);
          const half = node.h / 2;
          const kind = [rows.kind - 8, rows.kind + 8];
          const name = [rows.name - 10 * node.nameLines.length, rows.name + 10 * node.nameLines.length];
          expect(kind[0]).toBeGreaterThanOrEqual(-half);
          expect(kind[1]).toBeLessThanOrEqual(name[0]!);
          if (node.detailLines.length) {
            const detail = [rows.detail! - 9 * node.detailLines.length, rows.detail! + 9 * node.detailLines.length];
            expect(name[1]).toBeLessThanOrEqual(detail[0]!);
            expect(detail[1]).toBeLessThanOrEqual(half);
          } else {
            expect(rows.detail).toBeNull();
            expect(name[1]).toBeLessThanOrEqual(half);
          }
        }
      }
    }
  });
});

describe("the words on the lines", () => {
  type Box = { x: number; y: number; w: number; h: number }; // x, y: the centre
  const corners = (b: Box) => ({ l: b.x - b.w / 2, r: b.x + b.w / 2, t: b.y - b.h / 2, b: b.y + b.h / 2 });
  /** The gap between a box and a horizontal or vertical segment: 0 when they touch or cross. */
  const clearance = (b: Box, s: { x1: number; y1: number; x2: number; y2: number }) => {
    const c = corners(b);
    const sx = [Math.min(s.x1, s.x2), Math.max(s.x1, s.x2)] as const;
    const sy = [Math.min(s.y1, s.y2), Math.max(s.y1, s.y2)] as const;
    const dx = Math.max(0, sx[0] - c.r, c.l - sx[1]);
    const dy = Math.max(0, sy[0] - c.b, c.t - sy[1]);
    return Math.hypot(dx, dy);
  };
  const cases = [1, 2, 3, 4].flatMap((n) => (["path", "all"] as const).map((view) => [n, view] as const));

  test("every edge is named by exactly one label, and a fan-out has one label for the group", () => {
    for (const [n, view] of cases) {
      const { edges, labels } = build(n, view);
      const named = labels.flatMap((l) => l.edges).sort();
      expect(named).toEqual(edges.map((e) => e.id).sort());
      for (const l of labels) {
        const words = new Set(l.edges.map((id) => edges.find((e) => e.id === id)!.label));
        expect([...words]).toEqual([l.text]);
      }
    }
    const all = build(1, "all");
    expect(all.labels.filter((l) => l.text === "was caused by")).toHaveLength(1);
    expect(all.labels.find((l) => l.text === "was caused by")!.edges).toHaveLength(3);
    const cited = all.labels.filter((l) => l.text === "cited in");
    expect(cited.length).toBeLessThanOrEqual(2); // the cause's pages and the fix's pages
    expect(cited.reduce((sum, l) => sum + l.edges.length, 0)).toBe(all.edges.filter((e) => e.rel === "CITED_IN").length);
  });

  test("no two labels overlap", () => {
    for (const [n, view] of cases) {
      const { labels } = build(n, view);
      for (const a of labels)
        for (const b of labels) {
          if (a.id >= b.id) continue;
          const p = corners(a);
          const q = corners(b);
          const apart = p.r <= q.l || q.r <= p.l || p.b <= q.t || q.b <= p.t;
          expect(apart).toBe(true);
        }
    }
  });

  test("a label keeps 12 px from every line that is not its own", () => {
    for (const [n, view] of cases) {
      const { nodes, edges, labels, gap } = build(n, view);
      const byId = new Map(edges.map((e) => [e.id, e]));
      for (const l of labels) {
        const own = new Set(l.edges);
        for (const e of edges) {
          if (own.has(e.id)) continue;
          for (const s of edgeSegments(byId.get(e.id)!, nodes, gap)) {
            expect(clearance(l, s)).toBeGreaterThanOrEqual(12);
          }
        }
      }
    }
  });

  test("a label sits on a line of its own group and clear of every magnet", () => {
    for (const [n, view] of cases) {
      const { nodes, edges, labels, gap } = build(n, view);
      for (const l of labels) {
        const lines = l.edges.flatMap((id) => edgeSegments(edges.find((e) => e.id === id)!, nodes, gap));
        expect(Math.min(...lines.map((s) => clearance({ x: l.x, y: l.y, w: 0, h: 0 }, s)))).toBe(0);
        for (const node of nodes) {
          const box = { x: node.x + node.w / 2, y: node.y, w: node.w, h: node.h };
          const a = corners(l);
          const b = corners(box);
          expect(a.r <= b.l || b.r <= a.l || a.b <= b.t || b.b <= a.t).toBe(true);
        }
      }
    }
  });

  test("a label is wide enough for its words", () => {
    for (const [n, view] of cases) {
      for (const l of build(n, view).labels) {
        expect(l.w).toBeGreaterThanOrEqual(estimate(l.text, EDGE_PX, 600));
        expect(l.h).toBeGreaterThanOrEqual(EDGE_PX);
      }
    }
  });
});

describe("the answer path in a pane of a known width", () => {
  const at = (n: number, paneWidth: number, view: GraphView = "path") =>
    buildAskGraph(finalState(n), { view, ontology, facts: null, measure: estimate, paneWidth });
  /** The graph is drawn centred in its pane (the frame is its box, or the box mirrored about the
   * path when nothing is beside it): it is inside the pane when that box is no wider than it. */
  const inside = (nodes: { x: number; w: number; side: boolean }[], paneWidth: number) => {
    const usable = paneWidth - 24;
    const left = Math.min(...nodes.map((n) => n.x));
    const right = Math.max(...nodes.map((n) => n.x + n.w));
    const beside = nodes.some((n) => n.side);
    const width = beside ? right - left : 2 * Math.max(-left, right);
    expect(width).toBeLessThanOrEqual(usable + 0.5);
  };

  test("at the pane of a laptop the column is centred, up to 320 px wide, and the page sits under its fix", () => {
    for (const n of [1, 2, 3, 4]) {
      const { nodes, edges } = at(n, 570);
      const column = nodes.filter((x) => !x.side && !x.under);
      expect(new Set(column.map((x) => Math.round(centre(x)))).size).toBe(1);
      expect(Math.round(centre(column[0]!))).toBe(0);
      expect(column[0]!.w).toBeGreaterThanOrEqual(300);
      expect(column[0]!.w).toBeLessThanOrEqual(320);
      const fix = nodes.find((x) => x.type === "Procedure")!;
      const page = nodes.find((x) => x.type === "Document")!;
      const part = nodes.find((x) => x.type === "Part")!;
      expect(page.under).toBe(true);
      expect(page.side).toBe(false);
      expect(page.y - page.h / 2).toBeGreaterThan(fix.y + fix.h / 2); // directly under it
      expect(part.y - part.h / 2).toBeGreaterThan(page.y + page.h / 2); // and above the part
      expect(page.x).toBeGreaterThanOrEqual(12); // clear of the doubled stroke down the middle
      inside(nodes, 570);
      const cite = edges.find((e) => e.rel === "CITED_IN")!;
      expect(cite.source).toBe(fix.id);
      expect(cite.from).toBeCloseTo(page.x + page.w / 2, 5);
    }
  });

  test("with room the page sits beside the fix, level with it, and the whole is inside the pane", () => {
    for (const n of [1, 2, 3, 4]) {
      const { nodes } = at(n, 1000);
      const fix = nodes.find((x) => x.type === "Procedure")!;
      const page = nodes.find((x) => x.type === "Document")!;
      expect(page.under).toBeFalsy();
      expect(page.side).toBe(true);
      expect(page.y).toBe(fix.y);
      inside(nodes, 1000);
    }
  });

  test("at every width of the pane that the separator allows, everything is inside it and nothing overlaps", () => {
    for (const n of [1, 2, 3, 4]) {
      for (let width = 360; width <= 1000; width += 10) {
        const { nodes } = at(n, width);
        inside(nodes, width);
        for (const a of nodes)
          for (const b of nodes) {
            if (a.id >= b.id) continue;
            const apart =
              a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h / 2 <= b.y - b.h / 2 || b.y + b.h / 2 <= a.y - a.h / 2;
            expect(apart).toBe(true);
          }
        const column = nodes.filter((x) => !x.side && !x.under);
        expect(new Set(column.map((x) => Math.round(centre(x)))).size).toBe(1);
        expect(column.map((x) => x.lit).filter(Boolean)).toEqual(["alarm", "cause", "fix", "part", "bin"]);
      }
    }
  });

  test("the words keep their room at every width, with the page under its fix too", () => {
    for (const n of [1, 2, 3, 4]) {
      for (const width of [360, 460, 570, 700, 1000]) {
        const { nodes, edges, labels, gap } = at(n, width);
        for (const l of labels) {
          const own = new Set(l.edges);
          for (const e of edges) {
            if (own.has(e.id)) continue;
            for (const s of edgeSegments(e, nodes, gap)) {
              const dx = Math.max(0, Math.min(s.x1, s.x2) - (l.x + l.w / 2), l.x - l.w / 2 - Math.max(s.x1, s.x2));
              const dy = Math.max(0, Math.min(s.y1, s.y2) - (l.y + l.h / 2), l.y - l.h / 2 - Math.max(s.y1, s.y2));
              expect(Math.hypot(dx, dy)).toBeGreaterThanOrEqual(12);
            }
          }
        }
        expect(labels.flatMap((l) => l.edges).sort()).toEqual(edges.map((e) => e.id).sort());
      }
    }
  });

  test("without a measured pane the layout is the one it was", () => {
    const { nodes } = build(1, "path");
    const page = nodes.find((x) => x.type === "Document")!;
    expect(page.under).toBeFalsy();
    expect(page.side).toBe(true);
  });
});
