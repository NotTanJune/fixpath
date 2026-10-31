// The plan of the explorer's tree, apart from the component so that a test can read it: the node at
// the top, one branch for each kind of link around it, and under each branch its nodes in a column.
// The centre is a card that says what it is: its type, its ID and its name. A leaf says its ID and
// its name only, for its pill says the type. A branch is a lighter pill that says the kind of link
// and how many there are; it is not a fact of the graph.

import { magnetHeight, NAME_PX, OVER, DETAIL_PX } from "./askGraph";
import { EXPLORER_ORDER } from "./explorerLayout";
import { edgeWords } from "./explorerFocus";
import { sentenceCase } from "./format";
import { measure as measureText, wrapLines, type Measure } from "./textMetrics";

export interface ExNode {
  id: string;
  label: string;
  type: string;
}

export interface ExEdge {
  id: string;
  source: string;
  target: string;
  rel: string;
}

export interface Magnet {
  id: string;
  type: string;
  kind: string; // the type in words
  name: string[]; // the lines of the name
  detail: string[]; // the lines under it
  w: number;
  h: number;
  x: number; // the left edge
  y: number; // the middle
  branch: string | null; // the key of its branch; null for the centre
  centre: boolean;
}

/** "+13 more", where the reader opens a branch that was cut short. */
export interface MoreChip {
  id: string; // more:<branch key>
  branch: string;
  count: number;
  label: string;
  w: number;
  h: number;
  x: number;
  y: number;
}

/** The small outlined label of a branch: its icon, its words and how many it has. */
export interface Pill {
  id: string; // pill:<branch key>
  branch: string;
  type: string; // the type of its nodes, for the icon
  past: boolean; // past incidents, which have the clock
  words: string;
  count: number;
  countText: string;
  w: number;
  h: number;
  x: number;
  y: number;
}

/** A point on the rail that the drops hang from. x and y are its middle. */
export interface Junction {
  id: string;
  x: number;
  y: number;
}

/** A stroke of the tree, between two things of the plan: the centre, a junction, a pill or a node. */
export interface Wire {
  id: string;
  kind: "trunk" | "rail" | "drop" | "spine";
  from: string;
  to: string;
  px: number;
}

export interface ExLine {
  id: string; // the id of the edge in the graph
  from: string;
  to: string;
  text: string; // the relation in words, read from `from`
}

export interface ExplorerPlan {
  magnets: Magnet[];
  more: MoreChip[];
  pills: Pill[];
  junctions: Junction[];
  wires: Wire[];
  /** The edge behind each node drawn under the centre. */
  lines: ExLine[];
  /** Real edges between two drawn nodes of different branches, drawn faint. */
  cross: ExLine[];
}

const EMPTY: ExplorerPlan = { magnets: [], more: [], pills: [], junctions: [], wires: [], lines: [], cross: [] };

export const MAGNET_W = 204; // the centre card
// A leaf is narrower: its text starts 36 px in, after the glyph, and leaves 8 px at the right. The
// longest real ID (CNC-01-COOLANT, 135 px in Barlow 600 at 17 px) fits on one line of the 136 px
// that are left, and a label wraps to two lines.
export const LEAF_W = 180;
export const LEAF_OVER = 44;
// Between two columns. Three leaves and two gaps are 600 px, the most that the 541 px pane shows
// at the zoom floor (517 / 0.86 = 601 px); a cross-line shows 2 dashes in it.
export const COL_GAP = 30;
const TRUNK = 40; // from the centre card down to the rail
const DROP = 36; // from the rail down to a pill
const PILL_H = 48;
const PILL_MIN_W = 112;
const PILL_OVER = 52; // the padding, the icon and the gap after it
const PILL_PX = 14;
const PILL_GAP = 30; // from a pill down to its first node
const ROW_GAP = 12;
const CHIP_H = 30;
const CAP_ONE = 4; // nodes of a branch shown before "+N more"
const MAX_NAME_LINES = 2;
const MAX_DETAIL_LINES = 2;
const RAIL_PX = 2;

/** How thick the drop to a branch is: 2 px for one item, up to 6 px for twenty or more. */
export function dropPx(count: number): number {
  return 2 + 4 * Math.min(1, Math.log2(Math.max(1, count)) / Math.log2(20));
}

const BRANCH_OVERRIDE: Record<string, string> = { "out:RAISED": "raised alarm" };

/** The words of a branch. A link out reads as the ontology says it, a link in from the other end;
 * a branch of past breakdowns is "past incidents", whichever link it is. */
export function branchWords(rel: string, out: boolean, type: string, forward: (rel: string) => string): string {
  if (type === "Incident") return "past incidents";
  const override = BRANCH_OVERRIDE[`${out ? "out" : "in"}:${rel}`];
  if (override) return override;
  const words = out ? forward(rel) : edgeWords(rel, false, forward);
  return words.replace(/^is /, "");
}

const COUNT_WORD: Record<string, [string, string]> = {
  Line: ["line", "lines"],
  Asset: ["machine", "machines"],
  Component: ["component", "components"],
  AlarmCode: ["alarm", "alarms"],
  FailureMode: ["root cause", "root causes"],
  Procedure: ["fix", "fixes"],
  Part: ["part", "parts"],
  Incident: ["incident", "incidents"],
  Document: ["source", "sources"],
};

/** What a branch counts: "1 line", "17 incidents". */
export function countWords(count: number, type: string, wordOf: (type: string) => string): string {
  const [one, many] = COUNT_WORD[type] ?? [wordOf(type).toLowerCase(), `${wordOf(type).toLowerCase()}s`];
  return `${count} ${count === 1 ? one : many}`;
}

const rank = (type: string) => {
  const at = EXPLORER_ORDER.indexOf(type);
  return at === -1 ? EXPLORER_ORDER.length : at;
};
/** The order of a column: newest breakdown first, else by ID. */
const order = (a: ExNode, b: ExNode) =>
  rank(a.type) - rank(b.type) || (a.type === "Incident" ? b.id.localeCompare(a.id) : a.id.localeCompare(b.id));

/** At most `max` lines; the last is cut, and ends in "...", when there was more. */
function clamp(lines: string[], max: number, room: number, px: number, weight: number, m: Measure): string[] {
  if (lines.length <= max) return lines;
  const kept = lines.slice(0, max);
  let last = kept[max - 1]!;
  while (last.length > 1 && m(`${last}...`, px, weight) > room) last = last.slice(0, -1).trimEnd();
  kept[max - 1] = `${last}...`;
  return kept;
}

interface Group {
  key: string;
  rel: string;
  out: boolean;
  type: string;
  words: string;
  items: { node: ExNode; edge: ExEdge }[];
}

export function explorerPlan(input: {
  nodes: ExNode[];
  edges: ExEdge[];
  centre: string;
  wordOf: (type: string) => string;
  forward: (rel: string) => string;
  expanded: Set<string>; // the key of each branch the reader has opened
  measure?: Measure;
}): ExplorerPlan {
  const { nodes, edges, centre, wordOf, forward, expanded } = input;
  const m = input.measure ?? measureText;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const middle = byId.get(centre);
  if (!middle) return { ...EMPTY };

  // one branch for each kind of link: the relation, the direction and the type it reaches. A node
  // is in the branch of the first edge between it and the centre.
  const groups = new Map<string, Group>();
  const placed = new Set<string>();
  for (const e of edges) {
    if (e.source === e.target) continue;
    const out = e.source === centre;
    if (!out && e.target !== centre) continue;
    const node = byId.get(out ? e.target : e.source);
    if (!node || placed.has(node.id)) continue;
    placed.add(node.id);
    const key = `${out ? "o" : "i"}:${e.rel}:${node.type}`;
    const group = groups.get(key) ?? {
      key,
      rel: e.rel,
      out,
      type: node.type,
      words: branchWords(e.rel, out, node.type, forward),
      items: [],
    };
    group.items.push({ node, edge: e });
    groups.set(key, group);
  }
  // the lightest branch on the left, the heaviest on the right
  const branches = [...groups.values()].sort(
    (a, b) => a.items.length - b.items.length || a.words.localeCompare(b.words) || a.key.localeCompare(b.key),
  );

  const magnetOf = (n: ExNode, branch: string | null, x: number): Omit<Magnet, "y"> => {
    const leaf = branch !== null;
    const w = leaf ? LEAF_W : MAGNET_W;
    const room = w - (leaf ? LEAF_OVER : OVER);
    const cause = n.type === "FailureMode";
    const name = cause ? sentenceCase(n.label || n.id) : n.id;
    const detail = !cause && n.label && n.label !== n.id ? n.label : "";
    const nameLines = clamp(wrapLines(name, room, NAME_PX, 600, m), MAX_NAME_LINES, room, NAME_PX, 600, m);
    const detailLines = detail ? clamp(wrapLines(detail, room, DETAIL_PX, 400, m), MAX_DETAIL_LINES, room, DETAIL_PX, 400, m) : [];
    return {
      id: n.id,
      type: n.type,
      kind: leaf ? "" : wordOf(n.type),
      name: nameLines,
      detail: detailLines,
      w,
      h: magnetHeight(nameLines.length, detailLines.length, !leaf),
      x,
      branch,
      centre: branch === null,
    };
  };

  const colX = (i: number) => i * (LEAF_W + COL_GAP);
  const colMid = (i: number) => colX(i) + LEAF_W / 2;
  const mid = branches.length ? (colMid(0) + colMid(branches.length - 1)) / 2 : MAGNET_W / 2;

  const first = magnetOf(middle, null, mid - MAGNET_W / 2);
  const top: Magnet = { ...first, y: first.h / 2 };
  const magnets: Magnet[] = [top];
  const chips: MoreChip[] = [];
  const pills: Pill[] = [];
  const wires: Wire[] = [];
  const lines: ExLine[] = [];
  const railY = first.h + TRUNK;
  const pillY = railY + DROP + PILL_H / 2;
  const leavesTop = railY + DROP + PILL_H + PILL_GAP;

  branches.forEach((b, i) => {
    const count = b.items.length;
    const countText = countWords(count, b.type, wordOf);
    const textW = Math.max(m(b.words, PILL_PX, 600), m(countText, PILL_PX, 400));
    const w = Math.min(LEAF_W, Math.max(PILL_MIN_W, textW + PILL_OVER));
    const pill: Pill = {
      id: `pill:${b.key}`,
      branch: b.key,
      type: b.type,
      past: b.type === "Incident",
      words: b.words,
      count,
      countText,
      w,
      h: PILL_H,
      x: colX(i) + (LEAF_W - w) / 2,
      y: pillY,
    };
    pills.push(pill);

    // the nodes of the branch: a few, or all of them when the reader opened it
    const sorted = [...b.items].sort((p, q) => order(p.node, q.node));
    // one node that would be left out is drawn: "+1 more" says no more than the node would
    const limit = count - CAP_ONE === 1 ? CAP_ONE + 1 : CAP_ONE;
    const kept = expanded.has(b.key) ? sorted : sorted.slice(0, limit);
    let y = leavesTop;
    let lastId = pill.id;
    for (const item of kept) {
      const base = magnetOf(item.node, b.key, colX(i));
      magnets.push({ ...base, y: y + base.h / 2 });
      y += base.h + ROW_GAP;
      lastId = item.node.id;
      lines.push({
        id: item.edge.id,
        from: centre,
        to: item.node.id,
        text: edgeWords(item.edge.rel, item.edge.source === centre, forward),
      });
    }
    const left = count - kept.length;
    if (left > 0) {
      chips.push({
        id: `more:${b.key}`,
        branch: b.key,
        count: left,
        label: `+${left} more`,
        w: LEAF_W,
        h: CHIP_H,
        x: colX(i),
        y: y + CHIP_H / 2,
      });
      lastId = `more:${b.key}`;
    }
    wires.push({ id: `spine:${b.key}`, kind: "spine", from: pill.id, to: lastId, px: RAIL_PX });
  });

  // the rail: the trunk comes down at the middle of the centre, a drop leaves it at every pill
  const junctions: Junction[] = [];
  if (branches.length) {
    const xs = [...new Set([mid, ...pills.map((p) => Math.round((p.x + p.w / 2) * 100) / 100)])].sort((a, b) => a - b);
    const idOf = (x: number) => `j:${x}`;
    for (const x of xs) junctions.push({ id: idOf(x), x, y: railY });
    wires.push({ id: "trunk", kind: "trunk", from: centre, to: idOf(mid), px: RAIL_PX });
    for (let i = 1; i < xs.length; i++) {
      wires.push({ id: `rail:${i}`, kind: "rail", from: idOf(xs[i - 1]!), to: idOf(xs[i]!), px: RAIL_PX });
    }
    for (const p of pills) {
      const x = Math.round((p.x + p.w / 2) * 100) / 100;
      wires.push({ id: `drop:${p.branch}`, kind: "drop", from: idOf(x), to: p.id, px: dropPx(p.count) });
    }
  }

  // a real edge between two drawn nodes of different branches, once for each pair
  const branchOf = new Map(magnets.filter((n) => n.branch !== null).map((n) => [n.id, n.branch]));
  const cross: ExLine[] = [];
  const pairs = new Set<string>();
  for (const e of edges) {
    const a = branchOf.get(e.source);
    const b = branchOf.get(e.target);
    if (a === undefined || b === undefined || a === b) continue;
    const pair = [e.source, e.target].sort().join("|");
    if (pairs.has(pair)) continue;
    pairs.add(pair);
    cross.push({ id: e.id, from: e.source, to: e.target, text: forward(e.rel) });
  }
  return { magnets, more: chips, pills, junctions, wires, lines, cross };
}

/** The columns of the tree, left to right: where each starts and ends, and the count of its branch,
 * for the first view to start at the edge of one. */
export function columnsOf(plan: ExplorerPlan): { x1: number; x2: number; weight: number }[] {
  return plan.pills.map((p) => ({ x1: p.x + p.w / 2 - LEAF_W / 2, x2: p.x + p.w / 2 + LEAF_W / 2, weight: p.count }));
}

/** What the plan draws, for the fact map to follow: the centre, and the ids of the nodes and of
 * the edges (the edge ids of the graph) behind the lines of the tree and the cross-lines. A node left
 * in a "+N more" chip is not in it, and neither is a pill, which is not a fact. */
export function shownOf(plan: ExplorerPlan): { centre: string; ids: string[] } {
  return {
    centre: plan.magnets.find((m) => m.centre)?.id ?? "",
    ids: [...plan.magnets.map((m) => m.id), ...plan.lines.map((l) => l.id), ...plan.cross.map((l) => l.id)],
  };
}
