// The page indicator of the PDF viewer, apart from the component so that a test can read it.

/** The page a reader typed: a whole number, held to the first and the last page. Anything else
 * is no page (null), and the indicator goes back to the page that is shown. */
export function parsePage(text: string, total: number): number | null {
  if (!(total >= 1)) return null;
  const t = text.trim();
  if (!/^\d+$/.test(t)) return null;
  return Math.min(total, Math.max(1, Number(t)));
}

/** The page after a step of the next (`1`) or previous (`-1`) button, held inside the document. */
export function stepPage(current: number, direction: 1 | -1, total: number): number {
  return Math.min(Math.max(1, total), Math.max(1, current + direction));
}

/** The page that is read now, 1-based: the one with the most of its height inside the view
 * (`top` to `bottom`, in the same px as the spans of the pages); the upper one on a tie; the
 * nearest one when the view shows only the gap between two. */
export function pageInView(spans: { top: number; bottom: number }[], top: number, bottom: number): number {
  let best = 0;
  let most = -Infinity;
  spans.forEach((s, i) => {
    const seen = Math.min(bottom, s.bottom) - Math.max(top, s.top);
    // a page that is not in the view scores by how far away it is, below any page that is in it
    const score = seen > 0 ? seen : -(Math.max(top - s.bottom, s.top - bottom, 0) + 1);
    if (score > most) {
      most = score;
      best = i;
    }
  });
  return best + 1;
}
