// The zoom of a PDF page, apart from the component so that a test can read it. Positions of a
// quote are in page units (px at zoom 1); the zoom turns them into px of the screen.

import type { Rect } from "./pdfQuote";

export const ZOOM_MIN = 0.25;
export const ZOOM_MAX = 4;

/** The size the text of a cited quote is drawn at, in px: about what a printed manual is read at. */
export const READ_PX = 15;

/** The least it is drawn at to make the cited line and the lines around it fit the view: still
 * readable. Only when even this cannot hold the line is the page panned sideways. */
export const READ_PX_MIN = 12;

/** How many lines above and below the quote are held in view with it. */
const AROUND_LINES = 6;

/** The room kept beside a quote that is brought into view, in px. */
const SIDE = 24;

/** The box of a text item is its font size times this: from a little below the baseline to a little
 * below the top (see PdfView, where the boxes are made). */
export const BOX_PER_FONT = 1.08;

/** The least a page is fitted to: a pane that is not measured yet or is nearly shut. */
const MIN_PANE = 200;

export function clampZoom(zoom: number): number {
  if (!Number.isFinite(zoom)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom));
}

/** The zoom at which the widest page fills the pane. */
export function fitZoom(paneWidth: number, pageWidth: number): number {
  if (!(pageWidth > 0)) return 1;
  return clampZoom(Math.max(paneWidth, MIN_PANE) / pageWidth);
}

/** The width most pages of a document have (pages whose widths round to the same whole number
 * count as one; the widest of them is returned, so that rounding never crops a page). A tie goes
 * to the narrower width. No pages give 0. A manual of portrait pages with a few landscape ones
 * is fitted by the portrait width, as a PDF reader does. */
export function commonWidth(widths: number[]): number {
  const groups = new Map<number, { count: number; widest: number }>();
  for (const w of widths) {
    if (!(w > 0)) continue;
    const key = Math.round(w);
    const g = groups.get(key) ?? { count: 0, widest: 0 };
    groups.set(key, { count: g.count + 1, widest: Math.max(g.widest, w) });
  }
  let best: { key: number; count: number; widest: number } | null = null;
  for (const [key, g] of groups) {
    if (!best || g.count > best.count || (g.count === best.count && key < best.key)) best = { key, ...g };
  }
  return best?.widest ?? 0;
}

/** The zoom one page is drawn at. `base` is the zoom of the document: the fit of the common width
 * in fit mode, or the zoom the reader or the quote set. In fit mode a page wider than the common
 * width is fitted to the pane on its own, so that every page fills the pane; once a zoom is set,
 * one zoom applies to every page. */
export function pageZoom(base: number, fit: boolean, paneWidth: number, pageWidth: number, common: number): number {
  if (fit && pageWidth > common + 0.5) return fitZoom(paneWidth, pageWidth);
  return base;
}

/** The size in px at which text with a box of `boxHeight` (at zoom 1) is drawn at `zoom`. */
export function textPx(boxHeight: number, zoom: number): number {
  return (boxHeight / BOX_PER_FONT) * zoom;
}

/** The lines of text near a quote, as rectangles: the boxes of the page whose middle lies within
 * AROUND_LINES lines of the quote and whose text is of the quote's size (a heading or a caption
 * is not). The quote's own lines are among them. */
export function neighbourRects(boxes: { x: number; y: number; w: number; h: number }[], quote: Rect[]): Rect[] {
  if (!quote.length) return [];
  const heights = quote.map((r) => r.h).filter((h) => h > 0).sort((a, b) => a - b);
  const h = heights[Math.floor((heights.length - 1) / 2)] ?? 0;
  if (!(h > 0)) return [];
  const top = Math.min(...quote.map((r) => r.y)) - AROUND_LINES * h * 1.5;
  const bottom = Math.max(...quote.map((r) => r.y + r.h)) + AROUND_LINES * h * 1.5;
  return boxes
    .filter((b) => b.w > 0 && b.h > 0 && Math.abs(b.h - h) <= h * 0.2 && b.y + b.h / 2 >= top && b.y + b.h / 2 <= bottom)
    .map((b) => ({ x: b.x, y: b.y, w: b.w, h: b.h }));
}

/** The zoom a cited quote opens at: the one that draws its text at READ_PX, or the fit when the
 * pane is wide enough that the fit is larger. The middle height of its rectangles decides, so
 * that one large heading in a quote does not set the size. The quote and the lines around it
 * (`around`) are held whole in the view (`viewWidth`, px): the zoom is let down from READ_PX as far
 * as it takes, and no lower than READ_PX_MIN; only below that is the line panned sideways. */
export function readingZoom(
  rects: Rect[],
  fit: number,
  viewWidth = 0,
  target = READ_PX,
  around: Rect[] = [],
): number {
  const heights = rects.map((r) => r.h).filter((h) => h > 0).sort((a, b) => a - b);
  if (!heights.length) return clampZoom(fit);
  const middle = heights[Math.floor((heights.length - 1) / 2)]!;
  const reading = (px: number) => (px * BOX_PER_FONT) / middle;
  let zoom = Math.max(fit, reading(target));
  const all = [...rects, ...around];
  const left = Math.min(...all.map((r) => r.x));
  const right = Math.max(...all.map((r) => r.x + r.w));
  const room = viewWidth - 2 * SIDE;
  if (room > 0 && (right - left) * zoom > room) {
    zoom = Math.max(fit, reading(READ_PX_MIN), Math.min(zoom, room / (right - left)));
  }
  return clampZoom(zoom);
}

const STOPS = [0.25, 0.33, 0.5, 0.67, 0.75, 1, 1.25, 1.5, 2, 2.5, 3, 4];

/** The next stop above (`1`) or below (`-1`) the zoom, whatever the zoom is now. */
export function stepZoom(zoom: number, direction: 1 | -1): number {
  const eps = 0.005;
  if (direction === 1) return STOPS.find((s) => s > zoom + eps) ?? ZOOM_MAX;
  return [...STOPS].reverse().find((s) => s < zoom - eps) ?? ZOOM_MIN;
}

/** The zoom after a wheel turned with Ctrl or Cmd held (a pinch on a trackpad arrives the same
 * way): up when it turns away from the reader. */
export function wheelZoom(zoom: number, deltaY: number): number {
  return clampZoom(zoom * Math.exp(-deltaY * 0.0022));
}

/** What to bring into view for a quote: the box of all its lines, or, when that is taller than
 * the view, its first line. */
export function focusRect(rects: Rect[], maxHeight: number): Rect | null {
  if (!rects.length) return null;
  const x = Math.min(...rects.map((r) => r.x));
  const y = Math.min(...rects.map((r) => r.y));
  const right = Math.max(...rects.map((r) => r.x + r.w));
  const bottom = Math.max(...rects.map((r) => r.y + r.h));
  const box = { x, y, w: right - x, h: bottom - y };
  return box.h <= maxHeight ? box : rects[0]!;
}

/** The scroll offsets that put the middle of `rect` (page units, on a page whose top left is at
 * `page` px of the content) in the middle of the view, held inside the content. A rectangle wider
 * than the view is put with its left edge in view instead, so that its line starts where it is read.
 * When the view pans sideways and the lines around the quote (`around`, page units, the quote's
 * own lines among them) are given, their start is put at the edge instead, as long as the whole
 * rectangle is still in the view that way: the lines of the procedure are then read from their
 * first word, not cropped on both sides. */
export function centreScroll(
  rect: Rect,
  zoom: number,
  page: { x: number; y: number },
  view: { w: number; h: number },
  content: { w: number; h: number },
  around: Rect[] = [],
): { left: number; top: number } {
  const room = view.w - 2 * SIDE;
  const wide = rect.w * zoom > room;
  const start = around.length ? Math.min(rect.x, ...around.map((r) => r.x)) : rect.x;
  const lined = around.length > 0 && content.w > view.w && (rect.x + rect.w - start) * zoom <= room;
  const wantX = lined
    ? page.x + start * zoom - SIDE
    : wide
      ? page.x + rect.x * zoom - SIDE
      : page.x + (rect.x + rect.w / 2) * zoom - view.w / 2;
  const wantY = page.y + (rect.y + rect.h / 2) * zoom - view.h / 2;
  const hold = (want: number, room: number) => Math.min(Math.max(0, room), Math.max(0, want));
  return { left: hold(wantX, content.w - view.w), top: hold(wantY, content.h - view.h) };
}

/** How far to scroll after a zoom so that the point of the page that was under the pointer is
 * under it again: `anchor` is that point in page units, `sheet` where the page's top left is on
 * the screen now, `pointer` where the pointer is. */
export function anchorShift(
  anchor: { fx: number; fy: number },
  zoom: number,
  sheet: { x: number; y: number },
  pointer: { x: number; y: number },
): { dx: number; dy: number } {
  return { dx: sheet.x + anchor.fx * zoom - pointer.x, dy: sheet.y + anchor.fy * zoom - pointer.y };
}

/** Browsers refuse a canvas of more pixels than this (Safari's limit is the smallest). */
const MAX_CANVAS_PIXELS = 16_000_000;

/** The canvas pixels per CSS px for a page at `zoom`: the screen's device ratio, so that text is
 * sharp, held down where the canvas would be too large to be drawn. */
export function canvasScale(zoom: number, dpr: number, pageWidth: number, pageHeight: number): number {
  const ratio = dpr > 0 ? dpr : 1;
  const css = pageWidth * zoom * (pageHeight * zoom);
  if (css * ratio * ratio <= MAX_CANVAS_PIXELS) return ratio;
  return Math.sqrt(MAX_CANVAS_PIXELS / css);
}
