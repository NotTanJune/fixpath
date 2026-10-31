// How big the answer's graph is drawn in its pane, as a pure function so that a test can check it.

const NAME_UNITS = 17; // the size of a name in the explorer's graph units (askGraph NAME_PX)

/** The graph is not shrunk to fit the height of its pane below this: its smallest words are drawn
 * at 14 px, and a taller graph keeps this size and its pane scrolls. */
export const FRAMED_FLOOR = 1;

export interface Fit {
  zoom: number;
  /** The height of the plane the graph is drawn on, in px: the pane's, or more when it scrolls. */
  height: number;
  scrolls: boolean;
}

/** The size of a graph of `box` in a pane of `pane`, with `pad` px around it. The width of the pane
 * decides the size. The height does too when the graph fits at FRAMED_FLOOR or more; a graph that
 * would have to be smaller keeps FRAMED_FLOOR and scrolls. */
export function framedFit(
  pane: { w: number; h: number },
  box: { w: number; h: number },
  pad = 12,
  minZoom = 0.2,
  maxZoom = 1.2,
): Fit {
  const bw = Math.max(1, box.w);
  const bh = Math.max(1, box.h);
  const byHeight = Math.max((pane.h - 2 * pad) / bh, FRAMED_FLOOR);
  const zoom = Math.min(maxZoom, Math.max(minZoom, Math.min((pane.w - 2 * pad) / bw, byHeight)));
  const height = Math.max(pane.h, Math.ceil(bh * zoom + 2 * pad));
  return { zoom, height, scrolls: height > pane.h };
}

/** The least zoom of the Pipeline explorer. Its names are 17 px, and its type, detail and line
 * words 14 px, in the graph's own units; at this zoom a name is drawn at 14 px or more and the
 * rest at 12 px or more, so a graph wider than its pane is panned and not shrunk. */
export const EXPLORER_FLOOR = 0.86;

/** The least zoom at which a name (17 px in graph units) is still drawn at 14 px. */
export const NAME_FLOOR = 14 / NAME_UNITS;

/** The zoom the explorer's graph starts at: fitted to the width of its pane, never larger than
 * life and never below EXPLORER_FLOOR. */
export function explorerZoom(paneWidth: number, boxWidth: number, pad = 12, floor = EXPLORER_FLOOR): number {
  const fit = paneWidth > 0 ? (paneWidth - 2 * pad) / Math.max(1, boxWidth) : 1;
  return Math.min(1, Math.max(floor, fit));
}

/** How much of the explorer's graph, in px, always stays inside its pane: the wheel and a drag move
 * it, but never out of sight, so it never has to be found again by resizing the pane. */
export const KEEP_PX = 80;

/** The pan that keeps at least KEEP_PX of the graph (all of it, when it is smaller) inside the
 * view. `box` is the graph's rendered box at `pan`; the graph moves only as far as it has to. */
export function clampPan(
  pan: { x: number; y: number },
  box: { x1: number; y1: number; x2: number; y2: number },
  view: { w: number; h: number },
): { x: number; y: number } {
  const shift = (lo: number, hi: number, size: number) => {
    const keep = Math.min(KEEP_PX, hi - lo);
    if (hi < keep) return keep - hi;
    if (lo > size - keep) return size - keep - lo;
    return 0;
  };
  return { x: pan.x + shift(box.x1, box.x2, view.w), y: pan.y + shift(box.y1, box.y2, view.h) };
}

/** A column of the explorer's tree, in graph units: where it starts and ends, and how heavy its
 * branch is (the count of what it holds). */
export interface Column {
  x1: number;
  x2: number;
  weight: number;
}

/** The left edge of the first view of a tree wider than its pane: one of the columns' left edges, so
 * that no column is cut at the left. Of those, the one that keeps the centre card in view (whole if
 * it can be, else its middle), then shows the most whole columns, then the fewest cut at the right,
 * then holds the heaviest branch, then is the leftmost. */
function startEdge(view: number, columns: Column[], centre: { x1: number; x2: number }): number {
  const middle = (centre.x1 + centre.x2) / 2;
  let best = { score: [-1, -1, 0, -1, 0], left: columns[0]!.x1 };
  for (const edge of columns) {
    const left = edge.x1;
    const right = left + view;
    const inside = columns.filter((c) => c.x1 >= left - 1e-6 && c.x2 <= right + 1e-6);
    const cut = columns.some((c) => c.x1 >= left - 1e-6 && c.x1 < right && c.x2 > right + 1e-6);
    const seen = centre.x1 >= left - 1e-6 && centre.x2 <= right + 1e-6 ? 2 : middle >= left && middle <= right ? 1 : 0;
    const score = [seen, inside.length, cut ? 0 : 1, Math.max(0, ...inside.map((c) => c.weight)), -left];
    const better = score.findIndex((v, i) => v !== best.score[i]);
    if (better !== -1 && score[better]! > best.score[better]!) best = { score, left };
  }
  return best.left;
}

/** Where the explorer's tree starts in its pane, for a graph drawn at `zoom`. A tree that fits is
 * centred across the pane; a wider one starts with the middle of its top card (`focusX`, in graph
 * units) in the middle of the pane, but never with a gap left of or right of the tree. It starts at
 * the top. With no `focusX` the tree starts at the left.
 *
 * With `where` (the columns of the tree and its centre card) the wider tree starts instead with the
 * left edge of a column at the left of the pane, so that the first view never cuts a column there:
 * the one that keeps the centre card in view and shows the most whole columns (see startEdge). */
export function startPan(
  pane: { w: number },
  box: { x1: number; y1: number; x2: number },
  zoom: number,
  focusX: number | undefined,
  pad = 12,
  where?: { columns: Column[]; centre: { x1: number; x2: number } },
): { x: number; y: number } {
  const y = pad - box.y1 * zoom;
  const width = (box.x2 - box.x1) * zoom;
  if (width <= pane.w - 2 * pad) return { x: (pane.w - width) / 2 - box.x1 * zoom, y };
  if (where && where.columns.length) {
    const left = startEdge((pane.w - 2 * pad) / zoom, where.columns, where.centre);
    return { x: pad - left * zoom, y };
  }
  const flush = pad - box.x1 * zoom;
  if (focusX === undefined) return { x: flush, y };
  const far = pane.w - pad - box.x2 * zoom;
  return { x: Math.min(flush, Math.max(far, pane.w / 2 - focusX * zoom)), y };
}

/** The zoom of the explorer's first view, so that its right edge does not cut a column. When the
 * view that starts at a column's edge ends inside a column, the tree is drawn a little smaller, just
 * enough for that column to be whole, but never below NAME_FLOOR (a name stays at 14 px); when it
 * would have to be smaller than that, `zoom` is kept and the column is cut. */
export function snapZoom(
  paneWidth: number,
  boxWidth: number,
  zoom: number,
  columns: Column[],
  centre: { x1: number; x2: number },
  pad = 12,
): number {
  const room = paneWidth - 2 * pad;
  if (!columns.length || boxWidth * zoom <= room) return zoom;
  const view = room / zoom;
  const left = startEdge(view, columns, centre);
  const cut = columns.find((c) => c.x1 >= left - 1e-6 && c.x1 < left + view && c.x2 > left + view + 1e-6);
  if (!cut) return zoom;
  const next = room / (cut.x2 - left);
  return next >= NAME_FLOOR - 1e-9 ? next : zoom;
}

/** Whether the explorer's graph is fitted to its pane again after its nodes were set. A graph that
 * keeps its view is fitted once, when it first has nodes: the nodes that come after (a branch the
 * reader opened) add to the picture where the reader is and do not send the view back to the start. */
export function needsFit(o: { keepView: boolean; fitted: boolean; changed: boolean }): boolean {
  return o.keepView ? !o.fitted : o.changed;
}
