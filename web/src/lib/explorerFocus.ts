// What the explorer of the Pipeline view centres on, and the words of its controls, apart from the
// component so that a test can read them.

import { sentenceCase } from "./format";
import { EXPLORER_ORDER } from "./explorerLayout";

/** How many edges end in each node, in either direction. */
export function degreesOf(edges: { source: string; target: string }[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const e of edges) {
    out.set(e.source, (out.get(e.source) ?? 0) + 1);
    out.set(e.target, (out.get(e.target) ?? 0) + 1);
  }
  return out;
}

interface FactLike {
  fact_kind: "node" | "edge";
  fact_id: string;
}

/** The ends of an edge fact id "A|REL|B", or null when it is not that shape. */
function ends(factId: string): [string, string] | null {
  const [from, rel, to, ...rest] = factId.split("|");
  return from && rel && to && !rest.length ? [from, to] : null;
}

/** The nodes that the facts of a file name, each once, in the order the file gives them: the node
 * of a node fact, and both ends of an edge fact. */
export function factNodeIds(spans: FactLike[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  const add = (id: string) => {
    if (!seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  };
  for (const s of spans) {
    if (s.fact_kind === "node") add(s.fact_id);
    else for (const id of ends(s.fact_id) ?? []) add(id);
  }
  return out;
}

/** The node the explorer centres on for an open file. With a row selected, that fact's node (an
 * edge fact starts from its first end, the one its sentence is about); otherwise the most connected
 * node the file names, the lower ID on a tie. Null when the file names no node the graph holds. */
export function centreForFile(spans: FactLike[], degree: Map<string, number>, selected: number | null): string | null {
  const picked = selected === null ? undefined : spans[selected];
  if (picked) {
    const names = picked.fact_kind === "node" ? [picked.fact_id] : (ends(picked.fact_id) ?? []);
    const known = names.find((id) => degree.has(id));
    if (known) return known;
  }
  const candidates = factNodeIds(spans).filter((id) => degree.has(id));
  if (!candidates.length) return null;
  return candidates.reduce((best, id) => {
    const d = degree.get(id)!;
    const b = degree.get(best)!;
    return d > b || (d === b && id < best) ? id : best;
  });
}

/** Where to start when a file names nothing: the plant's line, else any node. */
export function fallbackCentre(nodes: { id: string; type: string }[]): string | null {
  return (nodes.find((n) => n.type === "Line") ?? nodes[0])?.id ?? null;
}

/** The line above the plane: what is at the centre, by its type word and its name. */
export function centreCaption(typeWord: string, name: string): string {
  return `${typeWord} ${name}, with its links`;
}

// The relations read from the other end: a line that leaves the node it is not "from" says what that
// node is to the one it leaves.
const REVERSE: Record<string, string> = {
  HAS_MACHINE: "belongs to",
  HAS_COMPONENT: "is part of",
  RAISED: "was raised by",
  INDICATES: "shows up as",
  AFFECTS: "is damaged by",
  RESOLVED_BY: "fixes",
  REQUIRES: "is needed by",
  FITS: "takes",
  OCCURRED_ON: "had",
  TRIGGERED_BY: "started",
  ROOT_CAUSE: "caused",
  USED: "was used in",
  STORED_IN: "holds",
  CITED_IN: "cites",
};

/** The words on a line, read from the node it leaves. `leavesSource` is whether that node is the
 * source of the edge in the graph; `forward` gives the words for the edge as it is stored. */
export function edgeWords(rel: string, leavesSource: boolean, forward: (rel: string) => string): string {
  return leavesSource ? forward(rel) : (REVERSE[rel] ?? "linked to");
}

const PLURAL: Record<string, string> = {
  Line: "Production lines",
  Asset: "Machines",
  Component: "Components",
  AlarmCode: "Alarms",
  FailureMode: "Root causes",
  Procedure: "Fixes",
  Part: "Spare parts",
  Incident: "Past breakdowns",
  Document: "Sources",
};

/** The plural of a node type's word: "Past breakdowns", "Root causes". */
export function pluralWords(type: string, word: string): string {
  return PLURAL[type] ?? `${word}s`;
}

export interface PickerItem {
  id: string;
  type: string;
  /** What the option says: an ID and its name, or, for a root cause, its name. */
  text: string;
}

export interface PickerGroup {
  type: string;
  heading: string;
  items: PickerItem[];
}

/** The nodes for the picker, by type in the order of the plant, filtered by a query that matches
 * the ID, the name or the type word. A group with no match is left out. */
export function groupPicker(
  nodes: { id: string; label: string; type: string }[],
  query: string,
  wordOf: (type: string) => string,
): PickerGroup[] {
  const q = query.trim().toLowerCase();
  const matches = (n: { id: string; label: string; type: string }) =>
    !q || n.id.toLowerCase().includes(q) || n.label.toLowerCase().includes(q) || wordOf(n.type).toLowerCase().includes(q);
  const types = [
    ...EXPLORER_ORDER,
    ...[...new Set(nodes.map((n) => n.type))].filter((t) => !EXPLORER_ORDER.includes(t)),
  ];
  return types.flatMap((type) => {
    const items = nodes
      .filter((n) => n.type === type && matches(n))
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((n) => ({
        id: n.id,
        type,
        text: type === "FailureMode" ? sentenceCase(n.label) : n.label && n.label !== n.id ? `${n.id} ${n.label}` : n.id,
      }));
    return items.length ? [{ type, heading: pluralWords(type, wordOf(type)), items }] : [];
  });
}

/** Where the explorer is, and the centres it has left, newest last. */
export interface Nav {
  centre: string;
  back: string[];
}

const TRAIL = 20;

/** A new centre, keeping the one it leaves on the trail. */
export function navTo(nav: Nav, centre: string): Nav {
  if (centre === nav.centre) return nav;
  return { centre, back: [...nav.back, nav.centre].slice(-TRAIL) };
}

/** One step back along the trail. */
export function backOf(nav: Nav): Nav {
  const previous = nav.back[nav.back.length - 1];
  return previous === undefined ? nav : { centre: previous, back: nav.back.slice(0, -1) };
}

interface PlainNode {
  id: string;
  label: string;
  type: string;
}
interface PlainEdge {
  id: string;
  source: string;
  target: string;
  rel: string;
}

/** The graph with the nodes and links a run added to it. What the graph already holds is kept as it
 * is, so a graph fetched again after the load takes the place of what was added; a link whose end
 * is in neither is left out. Without anything to add it is the graph itself. */
export function mergeGraph<G extends { nodes: PlainNode[]; edges: PlainEdge[] }>(
  graph: G,
  extra: { nodes: PlainNode[]; edges: PlainEdge[] } | null,
): G {
  if (!extra || (!extra.nodes.length && !extra.edges.length)) return graph;
  const have = new Set(graph.nodes.map((n) => n.id));
  const nodes = [...graph.nodes, ...extra.nodes.filter((n) => !have.has(n.id))];
  const known = new Set(nodes.map((n) => n.id));
  const kept = new Set(graph.edges.map((e) => e.id));
  const edges = [
    ...graph.edges,
    ...extra.edges.filter((e) => !kept.has(e.id) && known.has(e.source) && known.has(e.target)),
  ];
  return { ...graph, nodes, edges };
}
