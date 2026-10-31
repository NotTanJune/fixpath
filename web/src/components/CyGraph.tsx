import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import { useEffect, useRef } from "react";
import { clampPan, type Column, EXPLORER_FLOOR, explorerZoom, framedFit, needsFit, snapZoom, startPan } from "../lib/graphFit";
import { glyphUri } from "../lib/ontology";
import { PACE, paced } from "../lib/pace";
import type { Tone } from "../lib/ontology";
import { TONES } from "../lib/ontology";

/** A magnet that says what it is: its type in small capitals, its name, and one short line under
 * it. Cytoscape draws a label in one font, so each row is a label of its own, left aligned in
 * the text column of the magnet, and the rows of one magnet move and fade together. */
export interface CyRich {
  kind: string;
  name: string[];
  detail: string[];
  textX: number; // where the text starts, from the left edge of the magnet
  rows: { kind: number | null; name: number; detail: number | null }; // row centres, from the magnet's middle; no type row for a compact leaf
}

export interface CyNode {
  id: string;
  label: string;
  rich?: CyRich;
  tone: Tone;
  glyph: string;
  w: number;
  h?: number; // the height of the magnet; the explorer's are fixed
  x?: number; // the left edge, for the preset layout
  y?: number;
  dashed?: boolean;
  lit?: boolean;
  focus?: boolean;
  dense?: boolean; // the small magnets of the explorer
  mark?: boolean; // the explorer: found in the file that is open, drawn with a dotted outline
  pill?: boolean; // the explorer: a branch label, an outline with no fill; the label may have two lines
  dot?: boolean; // the explorer: a point on the rail that the drops hang from, not drawn
  pulse?: "new" | "match"; // the explorer, while a file is added: a halo once as the node is drawn, blue for a node the file added, yellow for one it matched
}

/** The words of a line of the answer's graph, on a box of the ground colour, placed by
 * lib/graphLabels.ts: x and y are the centre. A label is not part of any line, so that a
 * fan-out of lines has one. */
export interface CyLabel {
  id: string;
  text: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface CyEdge {
  id: string;
  source: string;
  target: string;
  text?: string; // the relation in plain words, drawn on the edge when edge labels are on
  dashed?: boolean;
  dotted?: boolean;
  lit?: boolean;
  labelAt?: number; // the explorer: put the words this many px from the target, on the last stretch of the line
  turn?: number; // the explorer: px from the source where the line turns down
  quiet?: boolean; // the explorer: drawn only while a node at one of its ends is pointed at
  px?: number; // the explorer's tree: the stroke, in px
  cross?: number; // the explorer's tree: a faint dashed curve between two nodes, bent this many px (signed)
  // leave sideways first, so that the line never runs behind a neighbour: "bus" turns half
  // way across the gap, "branch" turns early so that it stays clear of a bus
  route?: "bus" | "branch";
  from?: number; // the answer's graph: leave the source this many px right of its middle, at its bottom edge (fromY px below the middle)
  fromY?: number;
}

const INK = "#1D2023";
const BLUE = "#1F4FD1";
const MARK = "#FABD2F"; // the highlighter yellow of the page (--mark-solid)
const GROUND = "#F9F5D7";
const MUTED = "#5A6168";
const NODE_H = 56;
const EDGE_PX = 14; // the words on an edge
const PAD = 12; // the room around the graph in its panel, in px

/** A box in graph coordinates that the answer's graph is fitted to, whether or not all of its
 * nodes are drawn yet, so that the picture does not move as the reveal adds to it. */
export interface Frame {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

/** Fit the graph to its panel; in the explorer keep the text readable and start at the top
 * left when the whole graph would not fit at that size. With a frame the fit is to the frame: the
 * width of the panel decides the size, and the height does too until the text would be drawn
 * smaller than FRAMED_FLOOR (lib/graphFit.ts), when the graph keeps that size and its panel scrolls. */
function fitAll(g: Core, interactive: boolean, frame?: Frame | null, floor = EXPLORER_FLOOR, anchor?: string, columns?: Column[]): void {
  if (frame) {
    const container = g.container();
    const plane = container?.parentElement;
    if (!container || !plane) return;
    const w = plane.clientWidth;
    const h = plane.clientHeight;
    if (w <= 0 || h <= 0) return;
    const bw = Math.max(1, frame.x2 - frame.x1);
    const bh = Math.max(1, frame.y2 - frame.y1);
    const { zoom, height: need } = framedFit({ w, h }, { w: bw, h: bh }, PAD, g.minZoom(), g.maxZoom());
    if (container.style.height !== `${need}px`) container.style.height = `${need}px`;
    g.resize();
    g.zoom(zoom);
    g.pan({
      x: (w - bw * zoom) / 2 - frame.x1 * zoom,
      y: (need - bh * zoom) / 2 - frame.y1 * zoom,
    });
    return;
  }
  const real = g.elements().not(".ghost");
  if (!real.length) return;
  if (!interactive) {
    g.fit(real, PAD);
    return;
  }
  // the explorer: the width of the graph decides the size, no smaller than `floor` and no larger
  // than life, and the graph starts at the top of its panel, with the anchor node (the centre of
  // the tree) in the middle when the graph is wider than the panel; the rest is panned to
  const box = real.boundingBox();
  const width = g.container()?.clientWidth ?? 0;
  const focus = anchor ? g.getElementById(anchor) : null;
  const there = focus && !focus.empty() ? focus : null;
  const where = there && columns?.length ? { columns, centre: { x1: there.position("x") - there.width() / 2, x2: there.position("x") + there.width() / 2 } } : undefined;
  // a first view that would end inside a column is drawn a little smaller, so that the column is whole
  const fitted = explorerZoom(width, box.w, PAD, floor);
  const zoom = where ? snapZoom(width, box.w, fitted, where.columns, where.centre, PAD) : fitted;
  g.zoom(zoom);
  g.pan(startPan({ w: width }, box, zoom, there ? there.position("x") : undefined, PAD, where));
}

const STYLE: cytoscape.StylesheetJson = [
  {
    selector: "node",
    style: {
      shape: "rectangle",
      width: "data(w)",
      height: "data(h)",
      "background-color": "data(fill)",
      "border-width": 1,
      "border-color": INK,
      label: "data(label)",
      color: "data(text)",
      "font-family": '"Barlow", system-ui, sans-serif',
      "font-size": "data(fs)",
      "font-weight": 600,
      "text-valign": "center",
      "text-halign": "center",
      "text-margin-x": "data(tm)" as unknown as number,
      "text-wrap": "ellipsis",
      "line-height": 1.05,
      "text-max-width": "data(tw)",
      "background-image": "data(icon)",
      "background-width": "data(iw)",
      "background-height": "data(iw)",
      "background-position-x": "data(ix)" as unknown as number,
      "background-position-y": "data(iy)" as unknown as number,
      "background-fit": "none",
      "background-repeat": "no-repeat",
    },
  },
  {
    // one row of the text of a magnet: a one px node whose label starts at its right edge
    selector: "node.tag",
    style: {
      width: 1,
      height: 1,
      "background-opacity": 0,
      "border-width": 0,
      "background-image": "none",
      events: "no",
      label: "data(label)",
      "font-size": "data(fs)",
      "font-weight": "data(fw)" as unknown as number,
      "text-halign": "right",
      "text-valign": "center",
      "text-justification": "left",
      "text-wrap": "wrap",
      "text-max-width": "600px",
      "text-margin-x": 0,
      "line-height": 1.18,
    },
  },
  { selector: "node.tag.kind", style: { "text-transform": "uppercase" } },
  {
    // a branch of the tree: a thin outline and small words, lighter than a card, with no fill
    selector: "node.pill",
    style: {
      shape: "round-rectangle",
      "background-opacity": 0,
      "border-width": 1,
      "border-color": MUTED,
      "font-weight": 600,
      "text-wrap": "wrap",
      "text-justification": "left",
      "line-height": 1.2,
      events: "no",
    },
  },
  {
    // a point on the rail: nothing to see
    selector: "node.dot",
    style: { width: 1, height: 1, "background-opacity": 0, "border-width": 0, "background-image": "none", label: "", events: "no" },
  },
  {
    selector: "node.dashed",
    style: { "border-style": "dashed", "border-width": 2 },
  },
  { selector: "node.lit", style: { "border-color": BLUE, "border-width": 3 } },
  { selector: "node.focus", style: { "border-color": BLUE, "border-width": 3 } },
  { selector: "node.link", style: { "overlay-opacity": 0 } },
  {
    // found in the file that is open: a dotted outline in marker blue, clear of the body
    selector: "node.infile",
    style: { "outline-width": 2, "outline-style": "dotted", "outline-color": BLUE, "outline-offset": 3, "outline-opacity": 1 },
  },
  { selector: "node.wrap", style: { "text-wrap": "wrap" } },
  {
    selector: "edge",
    style: {
      width: 2,
      "line-color": INK,
      "curve-style": "taxi",
      "taxi-direction": "auto",
      "target-arrow-shape": "none",
      "overlay-opacity": 0,
    },
  },
  {
    selector: "edge.turn",
    style: { "taxi-direction": "horizontal", "taxi-turn": "data(turn)" as unknown as number },
  },
  {
    // the words of a line of the answer's graph: a box of the ground colour, so that no line runs
    // through them, above every line
    selector: "node.elabel",
    style: {
      width: "data(w)",
      height: "data(h)",
      "background-color": GROUND,
      "background-image": "none",
      "border-width": 0,
      label: "data(label)",
      "font-size": EDGE_PX,
      "font-weight": 600,
      color: INK,
      "text-valign": "center",
      "text-halign": "center",
      "text-margin-x": 0,
      "text-wrap": "none",
      events: "no",
      "z-index": 20,
    },
  },
  {
    // the explorer: the words sit on the last stretch of the line, just before the node it reaches
    selector: "edge.toward",
    style: {
      "target-label": "data(text)",
      "target-text-offset": "data(off)" as unknown as number,
      "font-family": '"Barlow", system-ui, sans-serif',
      "font-size": EDGE_PX,
      "font-weight": 600,
      color: INK,
      "text-rotation": "none",
      "text-background-color": GROUND,
      "text-background-opacity": 1,
      "text-background-padding": "3px",
      "text-background-shape": "rectangle",
    },
  },
  {
    // the trunk, the rail, the drops and the spines of the tree
    selector: "edge.wire",
    style: { "curve-style": "straight", width: "data(px)", "line-color": INK, "line-cap": "butt", events: "no" },
  },
  {
    // a real edge between two nodes of different branches: thin, muted and under the cards
    selector: "edge.cross",
    style: {
      "curve-style": "unbundled-bezier",
      width: 1.5,
      "line-color": MUTED,
      "line-style": "dashed",
      "line-dash-pattern": [6, 5],
      opacity: 0.6,
      events: "no",
      "z-index": 0,
    },
  },
  // the line from a fix down to its page leaves right of the fix's middle and drops straight
  { selector: "edge.hang", style: { "curve-style": "straight" } },
  { selector: "edge.quiet", style: { display: "none" } },
  { selector: "edge.peek", style: { display: "element", width: 1.5, "z-index": 5 } },
  { selector: "edge.bus", style: { "taxi-direction": "horizontal", "taxi-turn": "50%" } },
  { selector: "edge.branch", style: { "taxi-direction": "horizontal", "taxi-turn": "30%" } },
  { selector: "edge.dashed", style: { "line-style": "dashed", "line-dash-pattern": [6, 5] } },
  { selector: "edge.dotted", style: { "line-style": "dotted", "line-color": "#5A6168", width: 2 } },
  {
    selector: "edge.ghost",
    style: { "curve-style": "straight", opacity: 1, events: "no", "z-index": 10 },
  },
  { selector: "edge.outer", style: { width: 9, "line-color": BLUE } },
  { selector: "edge.inner", style: { width: 4, "line-color": GROUND, "z-index": 11 } },
];

/** Draw the first `at` px of a straight edge: one dash of that length and a gap longer than
 * the edge. (A gradient with transparent stops cannot do this: Cytoscape paints a gradient
 * stop with only its red, green and blue, so a transparent stop is opaque black.) */
function reveal(edge: cytoscape.EdgeSingular, at: number, whole: number): void {
  edge.style({ "line-style": "dashed", "line-dash-pattern": [Math.max(at, 0.01), whole + 8] });
}

/** A halo that swells around a node and fades, once: the sign that a file added it (blue) or matched it
 * (yellow). It is an animation of the node's outline, which sits outside the body so that the colours
 * of the node are not mixed with it, and it is removed when it ends. */
function halo(node: cytoscape.NodeSingular, kind: "new" | "match", ms: number): void {
  if (ms <= 0) return;
  const clear = () => {
    if (!node.removed()) node.removeStyle("outline-style outline-color outline-width outline-offset outline-opacity");
  };
  node.style({
    "outline-style": "solid",
    "outline-color": kind === "new" ? BLUE : MARK,
    "outline-width": 0,
    "outline-offset": 2,
    "outline-opacity": 1,
  });
  node
    .animate({ style: { "outline-width": 7, "outline-offset": 3 } }, { duration: ms * 0.3, easing: "ease-out" })
    .animate({ style: { "outline-width": 0, "outline-offset": 8, "outline-opacity": 0 } }, { duration: ms * 0.7, easing: "ease-in", complete: clear });
}

const TYPE_PX = 14;
const NAME_PX = 17;
const DETAIL_PX = 14;
const PILL_PX = 14; // the words of a branch

/** The elements of one magnet: its body and, when it says what it is, the rows of its text. */
function elementsOf(n: CyNode): ElementDefinition[] {
  const tone = TONES[n.tone];
  // an alternative is not faded: its body is an outline, with ink text, and its family shows
  // in the glyph, so the text keeps a full contrast on the ground
  const alt = !!n.dashed;
  const text = alt ? INK : tone.text;
  const rich = n.rich;
  const h = n.dense ? 30 : (n.h ?? NODE_H);
  const centre = n.x !== undefined && n.y !== undefined ? { x: n.x + n.w / 2, y: n.y } : null;
  if (n.dot || n.pill) {
    // a point of the rail, or a branch label: one element, with every field the style maps from
    const pill = !!n.pill;
    return [
      {
        group: "nodes",
        data: {
          id: n.id,
          label: pill ? n.label : "",
          fill: GROUND,
          text: INK,
          icon: pill ? glyphUri(n.glyph, tone.fill) : "",
          w: n.w,
          h: pill ? (n.h ?? NODE_H) : 1,
          tw: Math.max(40, n.w - 40),
          fs: PILL_PX,
          iw: pill ? 16 : 0,
          ix: 12,
          iy: "50%",
          tm: 14,
        },
        classes: pill ? "pill" : "dot",
        ...(centre ? { position: centre } : {}),
      },
    ];
  }
  const body: ElementDefinition = {
    group: "nodes",
    data: {
      id: n.id,
      label: rich ? "" : n.label,
      fill: alt ? GROUND : tone.fill,
      text,
      icon: glyphUri(n.glyph, alt ? tone.fill : tone.text),
      w: n.w,
      tw: Math.max(40, n.w - (n.dense ? 34 : 38)),
      h,
      fs: n.dense ? 14 : 16,
      iw: rich ? 18 : n.dense ? 12 : 16,
      ix: n.dense ? 8 : 10,
      iy: rich ? `${Math.round(h / 2 + rich.rows.name - 9)}px` : "50%",
      tm: n.dense ? 7 : 10,
    },
    classes: [n.dashed ? "dashed" : "", n.focus ? "focus" : "", n.mark ? "infile" : "", n.dense || rich ? "" : "wrap"].join(" "),
    ...(centre ? { position: centre } : {}),
  };
  if (!rich || !centre) return [body];
  const row = (kind: "kind" | "name" | "detail", label: string, at: number, fs: number, fw: number) =>
    ({
      group: "nodes",
      // the data that the style of a magnet maps from, so that a row has every field it is asked for
      data: {
        id: `${n.id}::${kind}`,
        owner: n.id,
        label,
        text,
        fs,
        fw,
        fill: GROUND,
        icon: "",
        w: 1,
        h: 1,
        tw: 600,
        iw: 0,
        ix: 0,
        iy: 0,
        tm: 0,
      },
      classes: `tag ${kind}`,
      position: { x: centre.x - n.w / 2 + rich.textX, y: centre.y + at },
    }) as ElementDefinition;
  const rows = [body];
  if (rich.kind && rich.rows.kind !== null) rows.push(row("kind", rich.kind, rich.rows.kind, TYPE_PX, 600));
  rows.push(row("name", rich.name.join("\n"), rich.rows.name, NAME_PX, 600));
  if (rich.detail.length && rich.rows.detail !== null) {
    rows.push(row("detail", rich.detail.join("\n"), rich.rows.detail, DETAIL_PX, 400));
  }
  return rows;
}

/** The element of one label: a node that is a box of the ground colour with the words in it. */
function labelElement(l: CyLabel): ElementDefinition {
  return {
    group: "nodes",
    data: {
      id: l.id,
      label: l.text,
      w: l.w,
      h: l.h,
      text: INK,
      fill: GROUND,
      icon: "",
      fs: EDGE_PX,
      fw: 600,
      tw: l.w + 40,
      iw: 0,
      ix: 0,
      iy: 0,
      tm: 0,
    },
    classes: "elabel",
    position: { x: l.x, y: l.y },
  };
}

export function CyGraph({
  nodes,
  edges,
  labels = [],
  interactive = false,
  onNode,
  label,
  refit = 0,
  snapMs = 0,
  drawMs = 0,
  frame = null,
  edgeLabels = false,
  floor,
  anchor,
  columns,
  keepView = false,
}: {
  nodes: CyNode[];
  edges: CyEdge[];
  labels?: CyLabel[]; // the words of the lines, one box each, for an owner that places them itself
  interactive?: boolean; // the explorer: pan and zoom, soft edges
  onNode?: (id: string) => void;
  label: string;
  refit?: number; // a change fits the graph to its panel again, for an owner that resized it
  snapMs?: number; // a node that is added snaps in over this long; 0 adds it at once
  drawMs?: number; // a lit edge draws over this long, after the nodes have snapped in
  frame?: Frame | null; // fit to this box and not to the nodes that are there
  edgeLabels?: boolean; // the explorer: say the relation on the last stretch of each edge
  floor?: number; // the explorer: the least zoom it is fitted to, so that a wide graph is panned and not shrunk
  anchor?: string; // the explorer: the node that starts in the middle of the pane when the graph is wider than it
  keepView?: boolean; // the explorer: fit once; nodes that come after (an opened branch) keep the view where it is
  columns?: Column[]; // the explorer's tree: its columns, so that the first view starts at the edge of one and cuts none
}) {
  const host = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const timers = useRef<number[]>([]);
  const lit = useRef<Set<string>>(new Set());
  const waiting = useRef<Set<string>>(new Set()); // lit edges whose stroke has not started
  const shape = useRef("");
  const fitted = useRef(false);
  const click = useRef(onNode);
  click.current = onNode;
  const framed = useRef(frame);
  framed.current = frame;
  const least = useRef(floor);
  least.current = floor;
  const anchored = useRef(anchor);
  anchored.current = anchor;
  const columned = useRef(columns);
  columned.current = columns;
  const haloed = useRef<Set<string>>(new Set()); // the nodes that have had their halo
  const frameKey = frame ? `${frame.x1},${frame.y1},${frame.x2},${frame.y2}` : "";

  // create the graph once, and make it follow the size of its panel
  useEffect(() => {
    if (!host.current) return;
    const wheelHost = host.current;
    // the explorer: the wheel pans (a list is long), Ctrl or Cmd with it, or a pinch, zooms around
    // the pointer. This listener is added before cytoscape makes its own on the same element, so it
    // runs first and its stopImmediatePropagation keeps cytoscape's wheel zoom from also running:
    // with both, every scroll zoomed out as well as panned, and the graph drifted out of its pane.
    const onWheel = (e: WheelEvent) => {
      const g = cy.current;
      if (!g) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (e.ctrlKey || e.metaKey) {
        const box = wheelHost.getBoundingClientRect();
        const level = Math.min(g.maxZoom(), Math.max(g.minZoom(), g.zoom() * Math.exp(-e.deltaY * 0.0022)));
        g.zoom({ level, renderedPosition: { x: e.clientX - box.left, y: e.clientY - box.top } });
      } else {
        g.panBy({ x: -e.deltaX, y: -e.deltaY });
      }
    };
    if (interactive) wheelHost.addEventListener("wheel", onWheel, { passive: false, capture: true });
    const instance = cytoscape({
      container: host.current,
      style: STYLE,
      elements: [],
      layout: { name: "preset" },
      minZoom: 0.2,
      maxZoom: 1.2,
      userZoomingEnabled: interactive,
      userPanningEnabled: interactive,
      boxSelectionEnabled: false,
      autoungrabify: true,
    });
    cy.current = instance;
    lit.current = new Set();
    waiting.current = new Set();
    shape.current = "";
    fitted.current = false;
    instance.on("tap", "node", (e) => click.current?.(e.target.id()));
    instance.on("mouseover", "node", (e) => {
      if (host.current) host.current.style.cursor = "pointer";
      e.target.connectedEdges(".quiet").addClass("peek");
    });
    instance.on("mouseout", "node", (e) => {
      if (host.current) host.current.style.cursor = "default";
      e.target.connectedEdges(".quiet").removeClass("peek");
    });
    // the explorer: a scroll, a drag or a zoom never takes the graph out of its pane
    if (interactive) {
      instance.on("viewport", () => {
        const real = instance.elements().not(".ghost");
        if (!real.length) return;
        const now = instance.pan();
        const next = clampPan(now, real.renderedBoundingBox(), { w: instance.width(), h: instance.height() });
        if (next.x !== now.x || next.y !== now.y) instance.pan(next);
      });
    }
    const fit = () => {
      instance.resize();
      fitAll(instance, interactive, framed.current, least.current, anchored.current, columned.current);
    };
    const observer = new ResizeObserver(fit);
    observer.observe(host.current);
    // an answer's graph sets its own height, so it is the panel around it that is watched
    if (host.current.parentElement) observer.observe(host.current.parentElement);
    // the canvas draws text in the font that is loaded when it paints
    void document.fonts
      ?.load('600 16px "Barlow"')
      .then(() => instance.forceRender());
    return () => {
      wheelHost.removeEventListener("wheel", onWheel, { capture: true });
      observer.disconnect();
      timers.current.forEach((t) => window.clearTimeout(t));
      instance.destroy();
      cy.current = null;
    };
  }, [interactive]);

  useEffect(() => {
    const g = cy.current;
    if (!g || refit === 0) return;
    g.resize();
    fitAll(g, interactive, framed.current, least.current, anchored.current, columned.current);
  }, [refit, interactive]);

  // the frame changes as the answer's facts arrive; the graph is fitted to it again
  useEffect(() => {
    const g = cy.current;
    if (g && frameKey) fitAll(g, interactive, framed.current, least.current, anchored.current, columned.current);
  }, [frameKey, interactive]);

  useEffect(() => {
    const g = cy.current;
    if (!g) return;
    const still = snapMs <= 0;
    const later = (ms: number, fn: () => void) => {
      if (still || ms <= 0) fn();
      else timers.current.push(window.setTimeout(fn, ms));
    };
    const ids = new Set(nodes.flatMap((n) => elementsOf(n).map((d) => d.data.id as string)));
    const labelIds = new Set(labels.map((l) => l.id));
    const freshLabels: cytoscape.NodeSingular[] = [];
    const edgeIds = new Set(edges.map((e) => e.id));
    const fresh: cytoscape.NodeSingular[] = [];
    const freshEdges: cytoscape.EdgeSingular[] = [];
    let added = false;

    g.batch(() => {
      g.nodes().forEach((n) => {
        if (n.hasClass("elabel") ? !labelIds.has(n.id()) : !ids.has(n.id())) {
          lit.current.delete(n.id());
          n.remove();
        }
      });
      g.edges().forEach((e) => {
        const base = e.id().replace(/~(o|i)$/, "");
        if (!edgeIds.has(base)) {
          lit.current.delete(base);
          waiting.current.delete(base);
          e.remove();
        }
      });
      for (const n of nodes) {
        for (const def of elementsOf(n)) {
          const id = def.data.id as string;
          const existing = g.getElementById(id);
          if (existing.empty()) {
            const el = g.add(def);
            el.addClass("link");
            fresh.push(el);
            added = true;
            continue;
          }
          existing.data(def.data);
          if (id === n.id) {
            existing.toggleClass("dashed", !!n.dashed);
            existing.toggleClass("focus", !!n.focus);
            existing.toggleClass("infile", !!n.mark);
          }
          // a node is moved when the layout changes: the explorer's, and the answer's when the
          // reader switches between the path and everything
          const to = def.position;
          if (to) {
            const from = existing.position();
            if (interactive || Math.abs(from.x - to.x) > 0.5 || Math.abs(from.y - to.y) > 0.5) {
              if (!existing.animated()) existing.position(to);
            }
          }
        }
      }
      for (const l of labels) {
        const def = labelElement(l);
        const existing = g.getElementById(l.id);
        if (existing.empty()) {
          freshLabels.push(g.add(def));
          added = true;
        } else {
          existing.data(def.data);
          const from = existing.position();
          if (Math.abs(from.x - l.x) > 0.5 || Math.abs(from.y - l.y) > 0.5) existing.position(def.position!);
        }
      }
      for (const e of edges) {
        const there = g.getElementById(e.id);
        if (!there.empty()) {
          // the layout of the answer changes with the width of its pane: a line is re-routed
          there.data("turn", e.turn ?? 0);
          there.data("px", e.px ?? 2);
          there.toggleClass("turn", e.turn !== undefined);
          there.toggleClass("hang", e.from !== undefined);
          if (e.from !== undefined) there.style("source-endpoint", `${e.from}px ${e.fromY ?? 0}px`);
          else there.removeStyle("source-endpoint");
          continue;
        }
        const el = g.add({
          group: "edges",
          data: { id: e.id, source: e.source, target: e.target, turn: e.turn ?? 0, text: e.text ?? "", off: e.labelAt ?? 0, px: e.px ?? 2 },
          classes: [
            edgeLabels && e.text && e.labelAt !== undefined ? "toward" : "",
            e.dashed ? "dashed" : "",
            e.dotted ? "dotted" : "",
            e.route ?? "",
            e.turn !== undefined ? "turn" : "",
            e.from !== undefined ? "hang" : "",
            e.quiet ? "quiet" : "",
            e.px !== undefined ? "wire" : "",
            e.cross !== undefined ? "cross" : "",
          ].join(" "),
        });
        if (e.cross !== undefined) el.style({ "control-point-distances": [e.cross], "control-point-weights": [0.5] });
        if (e.from !== undefined) el.style("source-endpoint", `${e.from}px ${e.fromY ?? 0}px`);
        freshEdges.push(el);
        added = true;
      }
    });

    const key = [...ids, ...labelIds].sort().join(",");
    if (framed.current) {
      // fitted to the frame by its own effect; the nodes that arrive do not move the picture
    } else if (needsFit({ keepView, fitted: fitted.current, changed: key !== shape.current || added })) {
      fitAll(g, interactive, null, least.current, anchored.current, columned.current);
      if (g.nodes().length) fitted.current = true;
    }
    shape.current = key;

    // a node that the file being added put in the graph or matched has a halo, once
    for (const n of nodes) {
      if (!n.pulse) continue;
      const mark = `${n.id}:${n.pulse}`;
      if (haloed.current.has(mark)) continue;
      haloed.current.add(mark);
      const el = g.getElementById(n.id);
      if (!el.empty()) halo(el, n.pulse, paced(PACE.ingest.pulse));
    }

    // a node that is added snaps in: a short drop into its place while it fades in; the edges
    // that join it fade in after it, and the lit stroke draws once its two ends are there
    if (!still && !interactive) {
      for (const node of fresh) {
        const home = node.position("y");
        node.style("opacity", 0);
        node.position("y", home - 10);
        node.animate(
          { position: { x: node.position("x"), y: home }, style: { opacity: 1 } },
          { duration: snapMs, easing: "ease-out-cubic" },
        );
      }
      // the words of a line come with the line: after the magnets have snapped in
      for (const word of freshLabels) {
        word.style("opacity", 0);
        later(snapMs, () => {
          if (!word.removed()) word.animate({ style: { opacity: 1 } }, { duration: 200 });
        });
      }
      for (const edge of freshEdges) {
        edge.style("opacity", 0);
        later(snapMs, () => {
          if (!edge.removed()) edge.animate({ style: { opacity: 1 } }, { duration: 200 });
        });
      }
    }

    // light the path: a node of the path has its lit border as it appears, and the doubled
    // stroke draws through the edges between two lit nodes
    const now = new Set<string>();
    for (const n of nodes) {
      const node = g.getElementById(n.id);
      if (n.lit) {
        now.add(n.id);
        if (!lit.current.has(n.id)) node.addClass("lit");
      } else if (lit.current.has(n.id)) {
        node.removeClass("lit");
      }
    }
    for (const e of edges) {
      const outer = `${e.id}~o`;
      const inner = `${e.id}~i`;
      if (e.lit) {
        now.add(e.id);
        if (!lit.current.has(e.id)) {
          // the doubled stroke does not exist before its turn, so nothing shows ahead of it
          waiting.current.add(e.id);
          later(snapMs, () => {
            if (!waiting.current.delete(e.id) || g.getElementById(e.id).empty()) return;
            if (!g.getElementById(outer).empty()) return;
            const mk = (id: string, cls: string) =>
              g.add({
                group: "edges",
                data: { id, source: e.source, target: e.target, text: e.text ?? "" },
                classes: `ghost ${cls}`,
              });
            const o = mk(outer, "outer");
            const i = mk(inner, "inner");
            if (still || drawMs <= 0) return;
            const from = o.sourceEndpoint();
            const to = o.targetEndpoint();
            const whole = Math.hypot(to.x - from.x, to.y - from.y);
            reveal(o, 0, whole);
            reveal(i, 0, whole);
            const start = performance.now();
            const step = (t: number) => {
              if (o.removed()) return;
              const p = Math.min(1, (t - start) / drawMs);
              const eased = 1 - Math.pow(1 - p, 3);
              reveal(o, whole * eased, whole);
              reveal(i, whole * eased, whole);
              if (p < 1) requestAnimationFrame(step);
              else {
                o.style({ "line-style": "solid" });
                i.style({ "line-style": "solid" });
              }
            };
            requestAnimationFrame(step);
          });
        }
      } else if (lit.current.has(e.id)) {
        waiting.current.delete(e.id);
        g.getElementById(outer).remove();
        g.getElementById(inner).remove();
      }
    }
    lit.current = now;
    // the animation settings of a render do not restart the effect
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges, labels, interactive, edgeLabels, keepView]);

  return <div className="cy" ref={host} role="img" aria-label={label} />;
}
