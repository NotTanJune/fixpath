// The work pane follows the paced reveal: when a stage brings a section on screen, the pane
// scrolls so that the section is in view. The decisions are pure functions of boxes, so that a
// test can check them without a browser; useFollowReveal (lib/hooks.ts) does the scrolling.

import type { Stage } from "./reveal";

/** The sections of the answer column that a stage brings in, by selector: the region that
 * follows is the box that holds all of them. A stage with none has nothing to follow. */
export const STAGE_SECTIONS: Partial<Record<Stage, string[]>> = {
  rootCause: [".cause"],
  fix: [".lock", 'section[aria-label="Fix"]'],
  partsBin: ['section[aria-label="Stock"]', 'section[aria-label="Parts"]'],
  claims: [".sum"],
  done: ['section[aria-label="Other causes"]'],
};

/** A vertical span, in px of the page. */
export interface Span {
  top: number;
  bottom: number;
}

/** The box that holds every span, or null when there are none. */
export function union(spans: Span[]): Span | null {
  if (!spans.length) return null;
  return {
    top: Math.min(...spans.map((s) => s.top)),
    bottom: Math.max(...spans.map((s) => s.bottom)),
  };
}

/** Where the pane scrolls to so that `region` is in view, or null when it already is. `pane` is
 * the pane's own box on the page, `scrollTop` how far it is scrolled, `pad` the room left above
 * the region. A region that is not fully in view is brought to the top of the pane, so that
 * its start is what the reader sees; the browser clamps the end of the pane. */
export function followTo(pane: Span, region: Span, scrollTop: number, pad = 16): number | null {
  const inView = region.top >= pane.top && region.bottom <= pane.bottom;
  if (inView) return null;
  const target = scrollTop + (region.top - pane.top) - pad;
  return Math.max(0, Math.round(target));
}

/** Whether a stage that has just come on screen is followed: only a paced reveal, one stage at
 * a time. A jump over several stages (a saved answer, "Show all now", an answer that is shown at
 * once) is read from the top and is not followed. */
export function followsStage(before: number, after: number): boolean {
  return after === before + 1 && after >= 1;
}

/** Whether the reveal of the current run is still followed. The reader takes over by scrolling
 * the pane themselves; the next run follows again. */
export class Follow {
  private run = -1;
  private stopped = false;

  /** The reader scrolled the pane. */
  stop(): void {
    this.stopped = true;
  }

  /** Whether the stage change `before` to `after` of run `run` is followed. */
  wants(run: number, before: number, after: number): boolean {
    if (run !== this.run) {
      this.run = run;
      this.stopped = false;
    }
    return !this.stopped && followsStage(before, after);
  }
}
