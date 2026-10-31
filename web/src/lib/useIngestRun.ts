// One ingest at a time on the Ingestion view, live or recorded: the state of the live view, and the
// calls that start a run and close it. A live run streams from the server and, when it has ended,
// loads the new file's sources again; a recorded run is played back from the API and touches
// nothing but the view.
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { afterIngest, getRecording, streamIngest } from "./api";
import { refusalText } from "./ingestSteps";
import { idleRun, liveIngest, type LiveState } from "./liveIngest";
import { paceFactor, paced, PACE } from "./pace";
import { play, toRunEvents, type RecordedRow } from "./playback";
import type { SourceSummary } from "./types";

export interface RunInfo {
  kind: "live" | "recorded";
  /** The file's own name. */
  name: string;
}

export interface IngestRun {
  state: LiveState;
  info: RunInfo | null;
  /** A run is on the way: no other can start. */
  busy: boolean;
  startLive: (file: File) => void;
  startRecorded: (row: RecordedRow) => void;
  /** Close the view: the run stops, and the view goes back to the files. */
  dismiss: () => void;
}

const WAIT_FAILED = "The recording could not be loaded. Try again.";

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = window.setTimeout(done, ms);
    function done() {
      window.clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done);
  });
}

/** `onLoaded` gets the source list as soon as a live file is in the graph; `onFinished` gets the
 * path of that file when the view has held its end state for a moment, and the view is closed. */
export function useIngestRun(
  onLoaded: (sources: SourceSummary[]) => void,
  onFinished: (docPath: string | null) => void,
): IngestRun {
  const [state, dispatch] = useReducer(liveIngest, idleRun);
  const [info, setInfo] = useState<RunInfo | null>(null);
  const stop = useRef<AbortController | null>(null);
  const count = useRef(0);
  const loaded = useRef(onLoaded);
  loaded.current = onLoaded;
  const finished = useRef(onFinished);
  finished.current = onFinished;

  const begin = useCallback((next: RunInfo, at: number) => {
    stop.current?.abort();
    const own = new AbortController();
    stop.current = own;
    count.current += 1;
    setInfo(next);
    dispatch({ event: "begin", data: { run: count.current }, at });
    return own;
  }, []);

  const startLive = useCallback(
    (file: File) => {
      const own = begin({ kind: "live", name: file.name }, performance.now());
      let path: string | null = null;
      const now = () => performance.now();
      streamIngest(
        file,
        (name, data) => {
          if (own.signal.aborted) return;
          if (name === "extracted") path = String((data as { doc_path?: string } | null)?.doc_path ?? "") || null;
          dispatch({ event: name, data, at: now() });
          if (name !== "done") return;
          // the file is a source now: the list, the map and the graph are read again, and then the
          // view holds its end state for a moment and opens the file
          afterIngest()
            .then((all) => !own.signal.aborted && loaded.current(all))
            .catch(() => {})
            .then(() => sleep(paced(PACE.ingest.settle), own.signal))
            .then(() => {
              if (own.signal.aborted) return;
              dispatch({ event: "reset", data: null, at: now() });
              setInfo(null);
              finished.current(path);
            });
        },
        own.signal,
      ).catch((e: Error) => {
        if (!own.signal.aborted) dispatch({ event: "error", data: { message: refusalText(e.message) }, at: now() });
      });
    },
    [begin],
  );

  const startRecorded = useCallback(
    (row: RecordedRow) => {
      const own = begin({ kind: "recorded", name: row.file }, 0);
      getRecording(row.number)
        .then((recording) => {
          if (own.signal.aborted) return;
          const events = toRunEvents(recording.events);
          // the clock of a recording starts at its first event
          dispatch({ event: "begin", data: { run: count.current }, at: events[0]?.at ?? 0 });
          return play(events, {
            deliver: (e) => !own.signal.aborted && dispatch(e),
            wait: (ms) => sleep(ms, own.signal),
            factor: paceFactor(),
            signal: own.signal,
          });
        })
        .catch(() => {
          if (!own.signal.aborted) dispatch({ event: "error", data: { message: WAIT_FAILED }, at: 0 });
        });
    },
    [begin],
  );

  const dismiss = useCallback(() => {
    stop.current?.abort();
    stop.current = null;
    dispatch({ event: "reset", data: null, at: 0 });
    setInfo(null);
  }, []);

  useEffect(() => () => stop.current?.abort(), []);

  return { state, info, busy: state.status === "running", startLive, startRecorded, dismiss };
}
