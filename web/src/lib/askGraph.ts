import type { SlotKind, AskState } from "./askReducer";
import { keyPart } from "./askReducer";
import { failureName, sentenceCase, sourceLabel } from "./format";
import { layoutLabels, type GLabel } from "./graphLabels";
import { labelOf, relationLabel } from "./ontology";
import { measure as measureText, wrapLines, type Measure } from "./textMetrics";
import type { Citation, Ontology, PathStep } from "./types";

export type GraphType =
  "Asset" | "AlarmCode" | "FailureMode" | "Procedure" | "Part" | "Bin" | "Incident" | "Document";

/** What the graph panel shows: the answer path alone, or the path with every fact around it. */
export type GraphView = "path" | "all";

export interface GNode {
  id: string;
  /** The name: the ID or, for a cause or a page, its words. Never empty, never cut. */
  label: string;
  type: GraphType;
  /** What the node is, in plain words from config/ontology.json: "Root cause", "Spare part". */
  kind: string;
  /** One short line under the name: the alarm's message, the stock; empty when there is none. */
  detail: string;
  /** The name and the detail as they are drawn, broken at spaces to fit the node. */
  nameLines: string[];
  detailLines: string[];
  x: number; // the left edge, in px of the graph plane
  y: number; // the middle of the node
  w: number;
  h: number;
  lit: SlotKind | null; // the magnet of the path row that lights this node
  dashed: boolean; // not the pick: an alternative
  /** Whether the node is on the answer path (or the page that cites its fix), or is one of the
   * other facts that only "Everything" shows. */
  layer: "path" | "extra";
  /** In the second column, beside the path, and not on it. */
  side: boolean;
  /** A cited page that hangs under its fix, to the right of the stroke down the path, because the
   * pane has no room for it beside the column. */
  under?: boolean;
  citations: Citation[];
}

export interface GEdge {
  id: string;
  source: string;
  target: string;
  rel: string;
  /** The relation in plain words: "usually means", "fixed by". Never empty. */
  label: string;
  lits: SlotKind[]; // lit when the magnets of both ends are on the track
  dashed: boolean;
  dotted: boolean;
  /** For an edge between the path and the side column: how far from its source the line turns. */
  turn?: number;
  /** For the line from a fix down to its page: how far right of the fix's middle it leaves. */
  from?: number;
}

export interface Facts {
  message: string | null;
  machineName: string | null;
}

export interface BuildOptions {
  view?: GraphView;
  ontology?: Ontology | null;
  /** What the graph says about the alarm and the machine, when it has said it. */
  facts?: Facts | null;
  measure?: Measure;
  /** The width of the pane the graph is drawn in, in px. With it the path is sized to the pane and
   * its cited page goes beside the fix or under it, whichever fits. */
  paneWidth?: number;
}

// the type sizes of a magnet, in px of the graph plane
export const TYPE_PX = 14; // small capitals above the name
export const NAME_PX = 17;
export const DETAIL_PX = 14;
const TYPE_LH = 16;
const NAME_LH = 20;
const DETAIL_LH = 18;
const PAD_T = 9;
const PAD_B = 10;
/** The width of a magnet that is not text: the padding, the glyph and the gap after it. */
export const OVER = 46;
/** Where the text of a magnet starts, from its left edge. */
export const TEXT_X = 36;
/** The height of a magnet with this many lines of name and of detail; a compact leaf has no row
 * for its type. */
export function magnetHeight(nameLines: number, detailLines: number, withKind = true): number {
  return PAD_T + (withKind ? TYPE_LH : 0) + NAME_LH * Math.max(1, nameLines) + DETAIL_LH * detailLines + PAD_B;
}

const CHAIN_MIN = 232;
/** The width the path is drawn at when the pane has the room for it. */
export const CHAIN_TARGET = 320;
/** The room around the graph in its pane, each side, in px: the same as the panel's. */
export const PANE_PAD = 12;
const UNDER_X = 20; // the left edge of a page under its fix, from the stroke down the middle
const UNDER_LINK = 48; // the line from a fix to the page under it: room for its word
const UNDER_BELOW = 24; // from that page to the magnet below
const CHAIN_WRAP = 300; // a longer name or detail is broken onto a second line
const SIDE_MIN = 150;
const SIDE_WRAP = 226;
const GAP_Y = 46; // between two magnets of the path: room for the word of the edge
const SIDE_GAP = 16; // between two magnets of the side column
const PATH_GAP = 88; // between the path and the side column: the word of a page's edge
const ALL_GAP = 104; // wide enough for "was caused by"

const KEPT = new Set(["INDICATES", "RESOLVED_BY", "REQUIRES", "ROOT_CAUSE"]);

/** The name of a cited page: "BH-250 manual, page 3". */
export function docLabel(c: Citation): string {
  const name = sourceLabel(c.doc_path, null);
  return c.page ? `${name}, page ${c.page}` : name;
}

/** What a magnet needs of its text, in px: the widest line of the type, the name and the detail. */
function textWidth(kind: string, name: string[], detail: string[], m: Measure): number {
  return Math.max(
    m(kind.toUpperCase(), TYPE_PX, 600),
    ...name.map((l) => m(l, NAME_PX, 600)),
    ...detail.map((l) => m(l, DETAIL_PX, 400)),
  );
}

/** The name of a magnet in as few lines as fit `room`. A cited page breaks before its page, so that
 * a page number is never left on a line of its own: "Kessler KX-500 manual," and "page 5". */
function nameLines(label: string, room: number, m: Measure): string[] {
  if (m(label, NAME_PX, 600) <= room) return [label];
  const comma = label.indexOf(", page ");
  if (comma > 0) return [label.slice(0, comma + 1), label.slice(comma + 2)].flatMap((l) => wrapLines(l, room, NAME_PX, 600, m));
  return wrapLines(label, room, NAME_PX, 600, m);
}

interface Spec {
  id: string;
  label: string;
  type: GraphType;
  detail: string;
  lit: SlotKind | null;
  dashed: boolean;
  citations: Citation[];
  layer: "path" | "extra";
  /** the magnet of the path that a side magnet hangs from, and the order of its group */
  anchor?: string;
  group?: number;
  reserve?: boolean; // keep the room of a detail line when there is none
  nameLines?: string[];
  detailLines?: string[];
  w?: number;
  h?: number;
}

/** The graph panel of an answer. The path runs down one column: the machine, its alarm, the root
 * cause, the fix, the key part and its bin, with the manual page that cites the fix beside it. In
 * "Everything" the other causes, their fixes, the past breakdowns of the pick, the other parts and
 * the other cited pages hang in a second column, each block level with the magnet it hangs from.
 * The path is centred on x = 0 and the side column follows it. */
export function buildAskGraph(
  state: AskState,
  options: BuildOptions = {},
): { nodes: GNode[]; edges: GEdge[]; labels: GLabel[]; size: number; gap: number } {
  const view = options.view ?? "path";
  const ontology = options.ontology ?? null;
  const facts = options.facts ?? null;
  const m = options.measure ?? measureText;
  const usable = options.paneWidth !== undefined ? options.paneWidth - 2 * PANE_PAD : null;
  const card = state.card;
  const decisions = state.triage?.decisions;
  const asset = card?.triage.asset_id ?? decisions?.machine ?? null;
  const alarm = card?.triage.alarm_code ?? decisions?.alarm ?? null;
  const slotOf = (kind: SlotKind) => state.slots.find((s) => s.kind === kind);

  const chain: Spec[] = [];
  const side: Spec[] = [];
  const specs = new Map<string, Spec>();
  const put = (list: Spec[], s: Spec) => {
    if (specs.has(s.id)) return;
    specs.set(s.id, s);
    list.push(s);
  };
  type Edge = { src: string; rel: string; dst: string; dashed: boolean; dotted: boolean };
  const edgeList: Edge[] = [];
  const link = (src: string, rel: string, dst: string, dashed = false, dotted = false) => {
    if (edgeList.some((e) => e.src === src && e.rel === rel && e.dst === dst)) return;
    edgeList.push({ src, rel, dst, dashed, dotted });
  };

  const pickedId =
    (card &&
      (card.causes.find((c) => c.failure_mode_id === card.triage.failure_mode_id) ?? card.causes[0])
        ?.failure_mode_id) ||
    slotOf("cause")?.id ||
    decisions?.root_cause ||
    null;
  const causeOf = (id: string) => card?.causes.find((c) => c.failure_mode_id === id);
  const causeName = (id: string) => sentenceCase(causeOf(id)?.name ?? failureName(id));
  const history = (id: string) => {
    const c = causeOf(id);
    if (!c) return "";
    return c.of_total > 0 ? `${c.support} of ${c.of_total} past stops` : "from the manual only";
  };
  const stock = (qty: number, onHand: number | null, enough: boolean) =>
    onHand === null
      ? `${qty} needed, stock unknown`
      : enough
        ? `${qty} needed, ${onHand} in stock`
        : `${qty} needed, only ${onHand} in stock`;

  // --- the path, top to bottom ---------------------------------------------------------
  if (asset) {
    put(chain, {
      id: asset,
      label: asset,
      type: "Asset",
      detail: facts?.machineName ?? "",
      lit: null,
      dashed: false,
      citations: [],
      layer: "path",
      reserve: true,
    });
  }
  if (alarm) {
    put(chain, {
      id: alarm,
      label: alarm,
      type: "AlarmCode",
      detail: facts?.message ?? "",
      lit: "alarm",
      dashed: false,
      citations: slotOf("alarm")?.citations ?? [],
      layer: "path",
      reserve: true,
    });
    if (asset) link(asset, "RAISED", alarm);
  }
  if (pickedId) {
    put(chain, {
      id: pickedId,
      label: causeName(pickedId),
      type: "FailureMode",
      detail: history(pickedId),
      lit: "cause",
      dashed: false,
      citations: causeOf(pickedId)?.citations ?? [],
      layer: "path",
      reserve: true,
    });
    if (alarm) link(alarm, "INDICATES", pickedId);
  }

  const steps: PathStep[] = state.path;
  const fixSlot = slotOf("fix");
  const fixId = card?.fix?.procedure_id ?? (fixSlot?.id || null);
  if (fixId && pickedId) {
    put(chain, {
      id: fixId,
      label: fixId,
      type: "Procedure",
      detail: card?.fix?.title ?? "",
      lit: "fix",
      dashed: false,
      citations: card?.fix?.citations ?? fixSlot?.citations ?? [],
      layer: "path",
      reserve: true,
    });
    link(pickedId, "RESOLVED_BY", fixId);
  }

  const keyId = card ? keyPart(card)?.part_no : slotOf("part")?.id;
  const needs = card?.parts.find((p) => p.part_no === keyId);
  if (keyId && fixId) {
    put(chain, {
      id: keyId,
      label: keyId,
      type: "Part",
      detail: needs ? stock(needs.qty, needs.qty_on_hand, needs.enough) : "",
      lit: "part",
      dashed: false,
      citations: needs?.citations ?? slotOf("part")?.citations ?? [],
      layer: "path",
      reserve: true,
    });
    link(fixId, "REQUIRES", keyId);
  }

  // the bin the key part is fetched from
  const binSlot = slotOf("bin");
  if (keyId && binSlot?.id) {
    put(chain, {
      id: binSlot.id,
      label: binSlot.id,
      type: "Bin",
      detail: binSlot.detail,
      lit: "bin",
      dashed: false,
      citations: binSlot.citations,
      layer: "path",
      reserve: true,
    });
    link(keyId, "STORED_IN", binSlot.id);
  }

  // --- the facts beside the path -------------------------------------------------------
  const pageOf = (c: Citation, anchor: string, layer: "path" | "extra", group: number) => {
    const id = `${c.doc_path}#${c.page ?? 0}`;
    if (specs.has(id)) return; // a page that is there cites one fact: the one that came first
    put(side, {
      id,
      label: docLabel(c),
      type: "Document",
      detail: "",
      lit: null,
      dashed: false,
      citations: [c],
      layer,
      anchor,
      group,
    });
    link(anchor, "CITED_IN", id, false, true);
  };
  if (card && fixId) {
    // the page that cites the fix is on the path; the other pages are of the second layer
    const [first, ...rest] = card.fix?.citations ?? [];
    if (first) pageOf(first, fixId, "path", 2);
    rest.forEach((c) => pageOf(c, fixId, "extra", 2));
  }

  if (card) {
    card.causes
      .filter((c) => c.failure_mode_id !== pickedId)
      .forEach((c) => {
        put(side, {
          id: c.failure_mode_id,
          label: sentenceCase(c.name),
          type: "FailureMode",
          detail: history(c.failure_mode_id),
          lit: null,
          dashed: true,
          citations: c.citations,
          layer: "extra",
          anchor: alarm ?? undefined,
          group: 0,
        });
        if (alarm) link(alarm, "INDICATES", c.failure_mode_id, true);
        const step = steps.find((s) => s.rel === "RESOLVED_BY" && s.src === c.failure_mode_id);
        const other = card.other_fixes.find((f) => f.procedure_id === step?.dst);
        if (step && other) {
          put(side, {
            id: other.procedure_id,
            label: other.procedure_id,
            type: "Procedure",
            detail: other.title,
            lit: null,
            dashed: true,
            citations: other.citations,
            layer: "extra",
            anchor: c.failure_mode_id,
            group: 0,
          });
          link(c.failure_mode_id, "RESOLVED_BY", other.procedure_id, true);
        }
      });
    if (pickedId) {
      card.breakdowns
        .filter((b) => b.root_cause_id === pickedId)
        .slice(0, 5)
        .forEach((b) => {
          put(side, {
            id: b.incident_id,
            label: b.incident_id,
            type: "Incident",
            detail: "",
            lit: null,
            dashed: false,
            citations: b.citations,
            layer: "extra",
            anchor: pickedId,
            group: 1,
          });
          link(b.incident_id, "ROOT_CAUSE", pickedId);
        });
    }
    card.parts
      .filter((p) => p.part_no !== keyId)
      .forEach((p) => {
        put(side, {
          id: p.part_no,
          label: p.part_no,
          type: "Part",
          detail: stock(p.qty, p.qty_on_hand, p.enough),
          lit: null,
          dashed: false,
          citations: p.citations,
          layer: "extra",
          anchor: fixId ?? undefined,
          group: 3,
        });
        if (fixId) link(fixId, "REQUIRES", p.part_no);
      });
    const cause = pickedId ? causeOf(pickedId) : undefined;
    (cause?.citations ?? []).forEach((c) => pageOf(c, pickedId!, "extra", 2));
  }
  // the cause edges that the path names but that were not drawn above
  for (const s of steps) if (KEPT.has(s.rel)) link(s.src, s.rel, s.dst);

  // --- sizes: a magnet is as wide as its text ------------------------------------------
  const shown = (s: Spec) => view === "all" || s.layer === "path";
  const chainShown = chain.filter(shown);
  const sideShown = side.filter(shown);
  const fit = (s: Spec, cap: number) => {
    const kind = labelOf(s.type, ontology);
    const room = cap - OVER;
    s.nameLines = nameLines(s.label, room, m);
    s.detailLines = s.detail ? wrapLines(s.detail, room, DETAIL_PX, 400, m) : [];
    const lines = Math.max(1, s.nameLines.length);
    const detail = s.detailLines.length || (s.reserve ? 1 : 0);
    s.h = PAD_T + TYPE_LH + NAME_LH * lines + DETAIL_LH * detail + PAD_B;
    return OVER + textWidth(kind, s.nameLines, s.detailLines, m);
  };
  // one width for the path and one for the side column, from the widest text in each; a single
  // word wider than the wrap is never cut, and the magnet is as wide as it
  let chainW = Math.max(CHAIN_MIN, ...chainShown.map((s) => Math.ceil(fit(s, CHAIN_WRAP))));
  let sideW = Math.max(SIDE_MIN, ...sideShown.map((s) => Math.ceil(fit(s, SIDE_WRAP))));
  // The path in a pane of a known width: the magnets are as wide as the pane allows, up to
  // CHAIN_TARGET. The page that cites the fix sits beside it when the pane has the room for both,
  // and otherwise under the fix, to the right of the stroke down the middle, where the path stays
  // centred in the pane.
  let under: Spec | null = null;
  if (usable !== null && view === "path") {
    chainW = Math.max(chainW, Math.min(CHAIN_TARGET, Math.floor(usable)));
    const page = sideShown.find((s) => s.type === "Document");
    if (page && usable < chainW + PATH_GAP + sideW) {
      under = page;
      // as wide as the half of the pane to the right of the stroke allows; its name breaks to fit
      const room = Math.max(SIDE_MIN, Math.floor(usable / 2 - UNDER_X));
      sideW = Math.min(room, Math.ceil(fit(page, room)));
    }
  }
  for (const s of chainShown) s.w = chainW;
  for (const s of sideShown) s.w = sideW;

  // --- places --------------------------------------------------------------------------
  const nodes = new Map<string, GNode>();
  const gap = view === "all" ? ALL_GAP : PATH_GAP;
  const make = (s: Spec, x: number, y: number, onSide: boolean, hung = false) => {
    const w = s.w ?? chainW;
    nodes.set(s.id, {
      id: s.id,
      label: s.label,
      type: s.type,
      kind: labelOf(s.type, ontology),
      detail: s.detail,
      nameLines: s.nameLines ?? [s.label],
      detailLines: s.detailLines ?? [],
      x: x - w / 2,
      y,
      w,
      h: s.h ?? 0,
      lit: s.lit,
      dashed: s.dashed,
      layer: s.layer,
      side: onSide,
      ...(hung ? { under: true } : {}),
      citations: s.citations,
    });
  };
  // The facts of a magnet hang beside it in one block, level with it, so that every line between
  // them is short. A block is stacked from the top; the page that is on the path sits level with
  // its fix, and any other block is centred on its magnet. The path gives each block room.
  const root = (s: Spec) => specs.get(s.anchor ?? "")?.anchor ?? s.anchor; // a fix hangs from its cause
  const blocks = new Map<string, { items: { spec: Spec; at: number }[]; up: number; down: number }>();
  for (const s of chainShown) {
    const items = sideShown
      .filter((x) => x !== under && root(x) === s.id)
      .sort(
        (x, y) =>
          (x.group ?? 0) - (y.group ?? 0) || (x.layer === "path" ? 1 : 0) - (y.layer === "path" ? 1 : 0),
      );
    if (!items.length) continue;
    let at = 0;
    const placed = items.map((spec, i) => {
      const h = spec.h ?? 0;
      if (i > 0) at += spec.anchor === items[i - 1]!.id ? GAP_Y : SIDE_GAP; // an edge between two needs room for its word
      const centre = at + h / 2;
      at += h;
      return { spec, centre };
    });
    // The block hangs from the magnet at one level. A page on the path is level with its fix. Else,
    // when the block has two groups of lines, the level is between them: one group fans out
    // above it and the other below it, so that their lines never lie on each other. Else the
    // block is centred.
    const level = placed.find((p) => p.spec.layer === "path");
    let mid = level ? level.centre : at / 2;
    if (!level) {
      const cuts = placed
        .slice(1)
        .map((p, i) => ({ p, before: placed[i]! }))
        .filter(({ p, before }) => (p.spec.group ?? 0) !== (before.spec.group ?? 0) && p.spec.anchor === before.spec.anchor)
        .map(({ p, before }) => (before.centre + (before.spec.h ?? 0) / 2 + p.centre - (p.spec.h ?? 0) / 2) / 2);
      if (cuts.length) mid = cuts.reduce((best, c) => (Math.abs(c - at / 2) < Math.abs(best - at / 2) ? c : best));
    }
    const rel = placed.map((p) => ({ spec: p.spec, at: p.centre - mid }));
    blocks.set(s.id, {
      items: rel,
      up: Math.max(...rel.map((r) => -(r.at - (r.spec.h ?? 0) / 2))),
      down: Math.max(...rel.map((r) => r.at + (r.spec.h ?? 0) / 2)),
    });
  }
  let bottom = -Infinity; // the bottom of the last magnet of the path
  let blockBottom = -Infinity; // the bottom of the last block beside it
  let hung = 0; // the room that the page under a fix takes below it
  chainShown.forEach((s, i) => {
    const h = s.h ?? 0;
    const block = blocks.get(s.id);
    let y = i === 0 ? h / 2 : bottom + GAP_Y + hung + h / 2;
    hung = 0;
    if (block) y = Math.max(y, blockBottom + SIDE_GAP + block.up);
    if (i === 0 && block) y = Math.max(y, block.up);
    make(s, 0, y, false);
    bottom = y + h / 2;
    if (under && under.anchor === s.id) {
      // the page hangs under its fix, right of the stroke; the magnet below moves down to make room
      const ph = under.h ?? 0;
      make(under, UNDER_X + (under.w ?? 0) / 2, bottom + UNDER_LINK + ph / 2, false, true);
      hung = UNDER_LINK + ph + UNDER_BELOW - GAP_Y;
    }
    if (block) blockBottom = y + block.down;
  });
  const sideX = chainW / 2 + gap + sideW / 2;
  for (const block of blocks.values()) {
    const at = nodes.get(chainShown.find((c) => blocks.get(c.id) === block)!.id)!.y;
    for (const item of block.items) make(item.spec, sideX, at + item.at, true);
  }

  // --- edges ---------------------------------------------------------------------------
  const edges = new Map<string, GEdge>();
  for (const e of edgeList) {
    const a = nodes.get(e.src);
    const b = nodes.get(e.dst);
    if (!a || !b) continue;
    const id = `${e.src}|${e.rel}|${e.dst}`;
    let turn: number | undefined;
    if (a.side !== b.side) {
      // a block of facts hangs from one magnet, so its lines share one turn, in the middle of the
      // gap, where the word of each edge is drawn
      turn = Math.round(gap / 2);
    }
    const page = b.under && a.id === e.src ? b : null;
    edges.set(id, {
      id,
      source: e.src,
      target: e.dst,
      rel: e.rel,
      label: relationLabel(e.rel, ontology),
      lits: [],
      dashed: e.dashed,
      dotted: e.dotted,
      turn,
      ...(page ? { from: page.x + page.w / 2 - (a.x + a.w / 2) } : {}),
    });
  }

  // an edge is lit when the magnets of both its ends are lit
  const litKind = (id: string) => nodes.get(id)?.lit ?? null;
  for (const e of edges.values()) {
    const a = litKind(e.source);
    const b = litKind(e.target);
    const aOn = a && slotOf(a)?.id === e.source;
    const bOn = b && slotOf(b)?.id === e.target;
    if (a && b && aOn && bOn) e.lits = [a, b];
  }
  for (const n of nodes.values()) {
    if (n.lit && slotOf(n.lit)?.id !== n.id) n.lit = null;
  }
  const height = Math.max(0, ...[...nodes.values()].map((n) => n.y + n.h / 2));
  const placedNodes = [...nodes.values()];
  const placedEdges = [...edges.values()];
  const labels = layoutLabels(placedEdges, placedNodes, gap, m);
  return { nodes: placedNodes, edges: placedEdges, labels, size: height, gap };
}

/** Where the rows of text of a magnet are centred, in px from the middle of the magnet (down is
 * positive): the type in small capitals, the name, and the detail under it, or null when there is
 * none. The rows are packed from the top, so that a magnet that has no detail yet keeps the others
 * where they will be. */
export function textRows(n: Pick<GNode, "h" | "nameLines" | "detailLines">): {
  kind: number;
  name: number;
  detail: number | null;
} {
  const top = -n.h / 2;
  const names = Math.max(1, n.nameLines.length);
  const nameTop = top + PAD_T + TYPE_LH;
  const detailTop = nameTop + NAME_LH * names;
  return {
    kind: top + PAD_T + TYPE_LH / 2,
    name: nameTop + (NAME_LH * names) / 2,
    detail: n.detailLines.length ? detailTop + (DETAIL_LH * n.detailLines.length) / 2 : null,
  };
}

/** Where the rows of a compact leaf are centred, from the middle of the card: the same as
 * textRows, without the row for the type, so the name starts where the type would have. */
export function leafRows(n: Pick<GNode, "h" | "nameLines" | "detailLines">): {
  kind: null;
  name: number;
  detail: number | null;
} {
  const top = -n.h / 2;
  const names = Math.max(1, n.nameLines.length);
  const nameTop = top + PAD_T;
  const detailTop = nameTop + NAME_LH * names;
  return {
    kind: null,
    name: nameTop + (NAME_LH * names) / 2,
    detail: n.detailLines.length ? detailTop + (DETAIL_LH * n.detailLines.length) / 2 : null,
  };
}
