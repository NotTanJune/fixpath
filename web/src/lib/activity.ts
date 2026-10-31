// Words and requests for the rail's list of what happened lately on the line.

import type { SheetRequest } from "../components/SourceSheet";
import { MONTHS } from "./format";
import type { SourceSummary } from "./types";

/** "45 min", "1 h", "2 h 5 min"; empty when the downtime is not known. */
export function downtimeText(minutes: number | null): string {
  if (minutes === null || !Number.isFinite(minutes)) return "";
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

const SGT = "Asia/Singapore";

function parts(date: Date, options: Intl.DateTimeFormatOptions): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of new Intl.DateTimeFormat("en-GB", { timeZone: SGT, ...options }).formatToParts(date)) {
    out[p.type] = p.value;
  }
  return out;
}

/** When an alarm was last seen, in plant time: "10:10" today, "28 Sep, 17:51" before. */
export function seenAt(iso: string | null, now: Date = new Date()): string {
  const when = iso ? new Date(iso) : null;
  if (!when || Number.isNaN(when.getTime())) return "";
  const clock = parts(when, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const time = `${clock.hour}:${clock.minute}`;
  // the month is looked up here, because the short name of September differs between engines
  const day = (d: Date) => parts(d, { day: "numeric", month: "numeric", year: "numeric" });
  const a = day(when);
  const b = day(now);
  return a.day === b.day && a.month === b.month && a.year === b.year
    ? time
    : `${a.day} ${MONTHS[Number(a.month) - 1]}, ${time}`;
}

/** The request that opens the source sheet for a row: its document by ID, with the file path
 * from the source list when it is there. There is no quote to find, because a symptom can
 * repeat in a file, so the sheet marks the spans of the fact that the label names. Null when
 * the row has no document. */
export function sourceRequest(
  label: string,
  docId: string | null,
  sources: Pick<SourceSummary, "id" | "path">[],
): SheetRequest | null {
  if (!docId) return null;
  const path = sources.find((s) => s.id === docId)?.path ?? docId;
  return {
    label,
    citations: [{ doc_path: path, page: null, chunk_id: docId, quote: "" }],
  };
}
