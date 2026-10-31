export interface Box {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface Frame {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The narrowest pane the sheet is drawn over. A pane narrower than this has no room to read
 * a document in, so the sheet takes the whole content area instead. */
export const MIN_PANE = 240;

const frameOf = (b: Box): Frame => ({
  left: b.left,
  top: b.top,
  width: b.right - b.left,
  height: b.bottom - b.top,
});

/** Where the source sheet sits: over the graph pane, edge to edge, when the page has one wide
 * enough; otherwise over the content area right of the rail; otherwise over the viewport
 * under the top bar. */
export function sheetBox(
  pane: Box | null,
  content: Box | null,
  viewport: { width: number; height: number },
  bar: number,
): Frame {
  if (pane && pane.right - pane.left >= MIN_PANE && pane.bottom > pane.top) return frameOf(pane);
  if (content && content.right > content.left && content.bottom > content.top) return frameOf(content);
  return { left: 0, top: bar, width: viewport.width, height: viewport.height - bar };
}
