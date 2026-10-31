// The size logic of a two-pane split, apart from the component so that a test can read it.

/** The separator, in px: a column of its own between the panes, a gutter, so that the bar never
 * lies over the scrollbar of the pane beside it. A 2 px hairline and a grip are drawn in it. */
export const BAR = 24;

/** The hit area of the separator, in px. A gloved hand needs about 44 px. The bar is 24, so
 * the hit area grows by 20 px, all into the second pane: the first pane's scrollbar sits against
 * the bar, and the hit area must not cover it. */
export const HIT = 44;

/** How far the hit area reaches before the gutter (into the first pane) and after it (into the
 * second), in px. */
export function hitSpan(): { before: number; after: number } {
  return { before: 0, after: HIT - BAR };
}

/** What an arrow key moves the separator, in px. */
export const STEP = 24;

export interface Bounds {
  width: number; // the container, in px
  minFirst: number; // the least the first pane may take, in px
  minSecond: number; // the least the second pane may take, in px
  bar: number; // the separator, in px
}

/** The least and the most the first pane may take. A container too small for both minimums
 * shares the room by the ratio of the minimums, so that neither pane is drawn at 0. */
function limits({ width, minFirst, minSecond, bar }: Bounds): [number, number] {
  const room = Math.max(0, width - bar);
  const hi = room - minSecond;
  if (hi >= minFirst) return [minFirst, hi];
  const shared = Math.round((room * minFirst) / Math.max(1, minFirst + minSecond));
  return [shared, shared];
}

/** The first pane's size in px, held inside both minimums. */
export function clampFirst(px: number, bounds: Bounds): number {
  const [lo, hi] = limits(bounds);
  return Math.min(hi, Math.max(lo, Math.round(px)));
}

/** The first pane's size after a key, or null when the key is not for the separator. Enter
 * is not handled here: the caller resets to its own initial size. */
export function keyFirst(key: string, first: number, bounds: Bounds): number | null {
  const [lo, hi] = limits(bounds);
  switch (key) {
    case "ArrowLeft":
      return clampFirst(first - STEP, bounds);
    case "ArrowRight":
      return clampFirst(first + STEP, bounds);
    case "Home":
      return lo;
    case "End":
      return hi;
    default:
      return null;
  }
}

/** The aria values of the separator: where it is and how far it may go, in percent of the
 * container. */
export function separatorValues(first: number, bounds: Bounds) {
  const { width } = bounds;
  if (width <= 0) return { now: 0, min: 0, max: 0 };
  const [lo, hi] = limits(bounds);
  const pct = (px: number) => Math.round((px / width) * 100);
  return { now: pct(first), min: pct(lo), max: pct(hi) };
}

/** The part of the Storage interface the split uses, so that a test can pass its own. */
export interface Store {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** The browser's storage, or null where reading it throws (blocked site data, some previews). */
export function browserStore(): Store | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

const keyOf = (id: string) => `split:${id}`;

/** The saved fraction of the container for the first pane, or `initial` when nothing usable is
 * saved or storage throws. */
export function loadFraction(id: string, initial: number, store: Store | null = browserStore()): number {
  try {
    const raw = store?.getItem(keyOf(id));
    if (raw == null || raw.trim() === "") return initial;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 && value < 1 ? value : initial;
  } catch {
    return initial;
  }
}

/** Save the fraction of the container for the first pane; a storage that throws is ignored. */
export function saveFraction(id: string, fraction: number, store: Store | null = browserStore()): void {
  try {
    store?.setItem(keyOf(id), String(fraction));
  } catch {
    // the split still works for this visit
  }
}
