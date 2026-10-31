import type { GraphView } from "./askGraph";

/** The view the reader chose on one answer: the run it was chosen on, and the view. */
export interface HeldView {
  run: number;
  view: GraphView;
}

/** What the graph panel shows for the answer of run `run`: the answer path alone, unless the reader
 * chose "Everything" on this very answer. Nothing carries over to the next question, to an answer
 * that is reopened, or to a reload, because each of them is a new run (or a new page): Everything
 * is the busy view, and every answer opens on the path. Nothing is read from or written to storage. */
export function viewOf(held: HeldView | null, run: number): GraphView {
  return held !== null && held.run === run ? held.view : "path";
}
