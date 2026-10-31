import type { Cause, GraphElements, PartNeed } from "./types";

/** How often this cause was behind past stops of the alarm on the machine, in plain words:
 * "6 of 6 past E-217 stops on HP-01". */
export function pastStops(cause: Pick<Cause, "support" | "of_total">, alarm: string | null, machine: string | null): string {
  if (cause.of_total <= 0) return "from the manual only, no past stops on record";
  const stops = cause.of_total === 1 ? "stop" : "stops";
  return `${cause.support} of ${cause.of_total} past ${alarm ? `${alarm} ` : ""}${stops}${machine ? ` on ${machine}` : ""}`;
}

/** "Most likely root cause" for the cause with the highest likelihood (or one tied for it),
 * "Less likely root cause" for the others. */
export function causeRank(cause: Pick<Cause, "likelihood">, all: Pick<Cause, "likelihood">[]): string {
  const top = Math.max(...all.map((c) => c.likelihood));
  return cause.likelihood >= top ? "Most likely root cause" : "Less likely root cause";
}

/** The plain sentence for a part that is not in stock in the amount that the fix needs; null when
 * there is enough. */
export function shortOnStock(part: Pick<PartNeed, "enough" | "qty" | "qty_on_hand" | "lead_time_days">): string | null {
  if (part.enough) return null;
  const lead = part.lead_time_days !== null ? ` Lead time ${part.lead_time_days} days.` : "";
  if (part.qty_on_hand === null) return `Stock unknown, ${part.qty} needed.${lead}`;
  return `Short: ${part.qty} needed, ${part.qty_on_hand} on hand.${lead}`;
}

/** The message of an alarm and the name of its machine, as the graph names them. */
export function alarmFacts(
  graph: GraphElements,
  alarm: string | null,
  machine: string | null,
): { message: string | null; machineName: string | null } {
  const label = (id: string | null, type: string) =>
    id ? (graph.nodes.find((n) => n.data.id === id && n.data.type === type)?.data.label ?? null) : null;
  return { message: label(alarm, "AlarmCode"), machineName: label(machine, "Asset") };
}
