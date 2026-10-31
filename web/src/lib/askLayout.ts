import { buildAskGraph } from "./askGraph";
import type { AskState } from "./askReducer";

/** Whether the graph panel is drawn beside the answer. It is not before a question; from the
 * moment one runs it is, so that the panes do not jump when the first node arrives; a run
 * that triage reads as a clarify or an out of scope question, or that ends in an error with
 * nothing on the graph, has nothing to draw and gets the whole width. */
export function hasGraph(state: AskState): boolean {
  if (state.phase === "idle") return false;
  const status = state.card?.triage.status ?? state.triage?.status ?? null;
  if (status === "clarify" || status === "out_of_scope") return false;
  if (state.phase === "running" || state.phase === "queued") return true;
  return buildAskGraph(state).nodes.length > 0;
}

/** Whether the "New question" control is shown: on every screen but the idle one, so that a
 * presenter always has one click back to the six suggestions. */
export function canStartOver(state: AskState): boolean {
  return state.phase !== "idle";
}

/** Whether "Show the reveal again" is shown: on a saved answer, in both modes. Every saved
 * question got an answer, so a replay serves it from the cache. */
export function canShowRevealAgain(state: AskState): boolean {
  return state.saved !== null;
}
