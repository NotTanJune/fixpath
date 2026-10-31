// The questions asked in this browser, kept so that an answer can be opened again at once,
// with no network call and no model call. One localStorage key holds the list, newest first;
// every access is inside try/catch, because reading or writing storage throws where site data
// is blocked, and the page has to work without it.

import { seenAt } from "./activity";
import { askReducer, initialAsk, type AskState } from "./askReducer";

export const HISTORY_KEY = "history";
export const MAX_RUNS = 12;

/** One server-sent event the reducer consumed. */
export interface SavedEvent {
  name: string;
  data: unknown;
}

export interface SavedRun {
  question: string;
  at: string; // ISO time of the save
  events: SavedEvent[];
}

/** The part of the Storage interface the history uses, so that a test can pass its own. */
export interface HistoryStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** The browser's storage, or null where even reaching for it throws. */
export function browserStorage(): HistoryStore | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

const same = (a: string, b: string) => key(a) === key(b);
const key = (question: string) => question.trim().replace(/\s+/g, " ").toLowerCase();

function usable(value: unknown): value is SavedRun {
  if (typeof value !== "object" || value === null) return false;
  const run = value as Record<string, unknown>;
  return (
    typeof run.question === "string" &&
    typeof run.at === "string" &&
    Array.isArray(run.events) &&
    run.events.every(
      (e) => typeof e === "object" && e !== null && typeof (e as SavedEvent).name === "string",
    )
  );
}

/** The saved runs, newest first. Nothing usable, a throwing store and corrupt JSON all give
 * an empty list; one bad entry is dropped and the others stay. */
export function loadRuns(store: HistoryStore | null = browserStorage()): SavedRun[] {
  try {
    const raw = store?.getItem(HISTORY_KEY);
    if (!raw) return [];
    const list: unknown = JSON.parse(raw);
    return Array.isArray(list) ? list.filter(usable).slice(0, MAX_RUNS) : [];
  } catch {
    return [];
  }
}

/** Writes the list. A store that is full loses the oldest runs until the rest fit; one that
 * throws every time keeps nothing, and the page goes on without it. */
function write(runs: SavedRun[], store: HistoryStore | null): void {
  for (let keep = runs.length; keep > 0; keep--) {
    try {
      store?.setItem(HISTORY_KEY, JSON.stringify(runs.slice(0, keep)));
      return;
    } catch {
      // too big, or blocked: try with one run less
    }
  }
}

/** Saves a run on top of the list: the same question moves up and takes the new events
 * instead of appearing twice, and the list is cut to MAX_RUNS. Returns the list as kept for
 * this visit, whether or not storage took it. */
export function saveRun(
  question: string,
  events: SavedEvent[],
  at: string = new Date().toISOString(),
  store: HistoryStore | null = browserStorage(),
): SavedRun[] {
  const q = question.trim();
  const rest = loadRuns(store).filter((r) => !same(r.question, q));
  const runs = [{ question: q, at, events }, ...rest].slice(0, MAX_RUNS);
  write(runs, store);
  return runs;
}

export function clearRuns(store: HistoryStore | null = browserStorage()): void {
  try {
    store?.removeItem(HISTORY_KEY);
  } catch {
    try {
      store?.setItem(HISTORY_KEY, "[]");
    } catch {
      // nothing more to do
    }
  }
}

/** The state of the screen for a saved run: its events through the reducer, in order. */
export function restoredState(run: SavedRun): AskState {
  return askReducer(initialAsk, {
    type: "restore",
    question: run.question,
    events: run.events,
    at: run.at,
  });
}

/** When a saved answer was saved, in plant time: "14:32" today, "27 Oct, 14:32" on another
 * day; empty for a value that is not a time. */
export function savedAt(iso: string, now: Date = new Date()): string {
  return seenAt(iso, now);
}
