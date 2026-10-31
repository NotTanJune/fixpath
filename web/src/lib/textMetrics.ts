// The width of a line of text in Barlow, so that a graph node is as wide as its text needs and no
// wider. In the browser the width is measured on a canvas in the loaded face; where there is no
// canvas (a test, a server) it is estimated per character class, a little wide, so that an
// estimate never makes a node too narrow.

/** The width in px of `text` at `px` and `weight`. */
export type Measure = (text: string, px: number, weight: number) => number;

const FACE = '"Barlow", system-ui, sans-serif';

/** An estimate from the kind of each character; measured against the face and rounded up. */
export const estimate: Measure = (text, px, weight) => {
  let units = 0;
  for (const ch of text) {
    if (ch === " ") units += 0.26;
    else if (ch === "-" || ch === ",") units += 0.36;
    else if (/[A-Z]/.test(ch)) units += 0.62;
    else if (/[0-9]/.test(ch)) units += 0.54;
    else if (/[a-z]/.test(ch)) units += 0.48;
    else units += 0.4;
  }
  return Math.ceil(units * px * (weight >= 600 ? 1 : 0.97));
};

let canvas: CanvasRenderingContext2D | null | undefined;
const cache = new Map<string, number>();

function context(): CanvasRenderingContext2D | null {
  if (canvas !== undefined) return canvas;
  try {
    canvas = typeof document === "undefined" ? null : document.createElement("canvas").getContext("2d");
  } catch {
    canvas = null;
  }
  return canvas;
}

/** Measure on a canvas in the loaded face, rounded up to a whole px; an estimate where there is no
 * canvas. Callers wait for the face to load (document.fonts) before they rely on it. */
export const measure: Measure = (text, px, weight) => {
  const ctx = context();
  if (!ctx) return estimate(text, px, weight);
  const font = `${weight} ${px}px ${FACE}`;
  const key = `${font}|${text}`;
  let width = cache.get(key);
  if (width === undefined) {
    ctx.font = font;
    width = Math.ceil(ctx.measureText(text).width);
    cache.set(key, width);
  }
  return width;
};

/** Break `text` into lines of at most `max` px at the spaces, as few lines as it takes. A word
 * wider than `max` stays whole on a line of its own: it is never cut. */
export function wrapLines(text: string, max: number, px: number, weight: number, m: Measure = measure): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (line && m(next, px, weight) > max) {
      lines.push(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  return lines;
}
