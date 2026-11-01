import { shortDate } from "./format";
import type { Upkeep, UpkeepItem } from "./types";

const URGENT = new Set(["overdue", "due_soon"]);

/** How many parts are overdue or due soon. */
export function dueCount(u: Upkeep): number {
  return u.items.filter((i) => URGENT.has(i.status)).length;
}

/** The sentence under a row: the usual gap, the last episode and when the next is due. */
export function dueLine(item: UpkeepItem): string {
  if (item.status === "not_enough_history" || item.gap_days === null || !item.due) {
    return "Not enough history to predict.";
  }
  const lead = `Usually every ${item.gap_days} days. Last on ${shortDate(item.last)}.`;
  const due = shortDate(item.due);
  return item.status === "overdue" ? `${lead} Past due since ${due}.` : `${lead} Due around ${due}.`;
}

/** The first overdue or due soon item of a machine, or null. */
export function itemFor(u: Upkeep, machine: string): UpkeepItem | null {
  return u.items.find((i) => i.machine === machine && URGENT.has(i.status)) ?? null;
}

/** The one line an answer carries when its machine has a part that is due. */
export function noteLine(item: UpkeepItem): string {
  const head = `Upkeep: ${item.cause_label} on ${item.machine}`;
  if (item.status === "overdue") return `${head} is past due (usually every ${item.gap_days} days).`;
  return `${head} is due around ${item.due ? shortDate(item.due) : "soon"}.`;
}
