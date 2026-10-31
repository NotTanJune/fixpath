// Playing a recorded ingest back: its events are delivered one after another, with a wait after
// each that is long enough for what the view does with it (the chunks marked in turn, the squares
// flying in). At pace 0 they are delivered at once.

import type { RunEvent } from "./liveIngest";
import { parseAt } from "./liveIngest";
import { PACE } from "./pace";

/** One line of a recording, as the API sends it. */
export interface Recorded {
  event: string;
  data: unknown;
  at: string;
}

/** One recording as the API sends it. */
export interface Recording {
  number: number;
  file: string | null;
  events: Recorded[];
}

/** One row of the list of recordings: the report row of a file of the test set. */
export interface RecordedRow {
  number: number;
  file: string;
  status: string;
  doc_type: string | null;
  detail: string | null;
  converted_from: string | null;
  chunks: number;
  facts: number;
  matched: number;
  new: number;
}

export function toRunEvents(events: Recorded[]): RunEvent[] {
  return events.map((e) => ({ event: e.event, data: e.data, at: parseAt(e.at) }));
}

const count = (value: unknown) => (Array.isArray(value) ? value.length : 0);

/** How long to wait after an event, in ms at the pace `factor`: the time between two events, or
 * the time the view needs for the event if that is longer. */
export function holdAfter(event: RunEvent, factor: number): number {
  const t = PACE.ingest;
  const data = (event.data ?? {}) as Record<string, unknown>;
  let hold: number = t.event;
  if (event.event === "chunked") hold = Math.max(t.event, count(data.chunks) * t.chunkMark);
  else if (event.event === "placed") {
    const n = count(data.passages);
    const m = count(data.facts);
    if (n || m) hold = Math.max(t.event, (n ? t.fly + (n - 1) * t.flyStagger : 0) + m * t.factPop);
  }
  return hold * factor;
}

/** Delivers the events in order. After each, but the last, it waits `holdAfter` ms through `wait`.
 * With a factor of 0 it never waits. It stops when the signal aborts. */
export async function play(
  events: RunEvent[],
  options: {
    deliver: (event: RunEvent) => void;
    wait: (ms: number) => Promise<void>;
    factor: number;
    signal?: AbortSignal;
  },
): Promise<void> {
  for (let i = 0; i < events.length; i++) {
    if (options.signal?.aborted) return;
    const event = events[i]!;
    options.deliver(event);
    if (i < events.length - 1 && options.factor > 0) {
      await options.wait(holdAfter(event, options.factor));
    }
  }
}
