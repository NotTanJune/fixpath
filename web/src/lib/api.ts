import { readEvents } from "./sse";
import type { FactMapData } from "./factMap";
import type { RecordedRow, Recording } from "./playback";
import type {
  Activity,
  GraphElements,
  Health,
  Ontology,
  SourceDoc,
  SourceSummary,
  Suggestion,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** The plain sentence of a failed response: the detail of the API when it sent one. */
async function failure(response: Response): Promise<ApiError> {
  let detail = "";
  try {
    const body = await response.json();
    detail = typeof body?.detail === "string" ? body.detail : "";
  } catch {
    detail = "";
  }
  return new ApiError(detail || `The server answered ${response.status}.`, response.status);
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError("The server did not answer. Check that it is running.", 0);
  }
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

export const getHealth = () => getJson<Health>("/api/health");
export const getOntology = () => getJson<Ontology>("/api/ontology");
export const getSuggestions = () => getJson<Suggestion[]>("/api/suggestions");
export const getSources = () => getJson<SourceSummary[]>("/api/sources");
export const getSource = (id: string) =>
  getJson<SourceDoc>(`/api/sources/${encodeURIComponent(id)}`);
export const getGraph = (focus?: string, depth = 1) =>
  getJson<GraphElements>(
    focus ? `/api/graph?focus=${encodeURIComponent(focus)}&depth=${depth}` : "/api/graph",
  );

export const getActivity = (limit?: number, alarmLimit?: number) =>
  getJson<Activity>(
    limit === undefined && alarmLimit === undefined
      ? "/api/activity"
      : `/api/activity?limit=${limit ?? 8}&alarm_limit=${alarmLimit ?? 5}`,
  );

/** The recorded ingests of the test set: the rows, and one run's events. Both are read-only, so
 * they work on a server with no keys. */
export const getRecorded = () => getJson<RecordedRow[]>("/api/ingest/recorded");
export const getRecording = (number: number) => getJson<Recording>(`/api/ingest/recorded/${number}`);

export interface SseHandler {
  (name: string, data: unknown): void;
}

async function stream(
  path: string,
  init: RequestInit,
  onEvent: SseHandler,
  signal: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, signal });
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    throw new ApiError("The server did not answer. Check that it is running.", 0);
  }
  if (!response.ok) throw await failure(response);
  try {
    for await (const message of readEvents(response)) {
      let data: unknown = null;
      try {
        data = JSON.parse(message.data);
      } catch {
        data = message.data;
      }
      onEvent(message.event, data);
    }
  } catch (error) {
    if ((error as Error).name === "AbortError") return;
    throw new ApiError("The connection dropped before the answer finished.", 0);
  }
}

export function streamAsk(
  question: string,
  onEvent: SseHandler,
  signal: AbortSignal,
  history: { role: "technician" | "assistant"; text: string }[] = [],
): Promise<void> {
  return stream(
    "/api/ask",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ question, history }),
    },
    onEvent,
    signal,
  );
}

export function streamIngest(file: File, onEvent: SseHandler, signal: AbortSignal): Promise<void> {
  const body = new FormData();
  body.append("file", file);
  return stream("/api/ingest", { method: "POST", body }, onEvent, signal);
}

// The documents are looked up by path (a citation has a path), so the list is kept. A live
// drop adds a file, so the list is dropped after it and once more on a lookup miss.
let sourceList: Promise<SourceSummary[]> | null = null;
let generation = 0;
const docs = new Map<string, SourceDoc>();

export function sourcesOnce(): Promise<SourceSummary[]> {
  sourceList ??= getSources().catch((error) => {
    sourceList = null;
    throw error;
  });
  return sourceList;
}

let factMap: Promise<FactMapData> | null = null;

/** The fact map, fetched once a session; a failed fetch is forgotten so the next open tries again. */
export function mapOnce(): Promise<FactMapData> {
  factMap ??= getJson<FactMapData>("/api/map").catch((error) => {
    factMap = null;
    throw error;
  });
  return factMap;
}

/** Forgets the list and the documents, so that the next read asks the server again. */
export function resetSources(): void {
  sourceList = null;
  docs.clear();
  generation += 1;
}

/** Forgets the fact map, which a load has put new points on. */
export function resetMap(): void {
  factMap = null;
}

const staleListeners = new Set<() => void>();

/** Runs `listener` after a live drop has been ingested, when what the graph says about the
 * line has changed. Returns the function that stops it. */
export function onActivityStale(listener: () => void): () => void {
  staleListeners.add(listener);
  return () => staleListeners.delete(listener);
}

/** After a file was ingested: drop what was kept and load the list again, and tell what shows
 * the activity of the line to read it again. */
export function afterIngest(): Promise<SourceSummary[]> {
  resetSources();
  resetMap();
  for (const listener of [...staleListeners]) {
    try {
      listener();
    } catch {
      // one listener failing must not hold back the source list or the others
    }
  }
  return sourcesOnce();
}

/** A document by ID, kept until the next reset. */
export async function sourceDoc(id: string): Promise<SourceDoc> {
  const cached = docs.get(id);
  if (cached) return cached;
  const at = generation;
  const doc = await getSource(id);
  if (at === generation) docs.set(id, doc);
  return doc;
}

export async function sourceIdOfPath(path: string): Promise<string | null> {
  const find = (all: SourceSummary[]) => all.find((s) => s.path === path)?.id ?? null;
  const hit = find(await sourcesOnce());
  if (hit) return hit;
  // a miss: the file may have come in after the list was read, so ask once more
  sourceList = null;
  return find(await sourcesOnce());
}
