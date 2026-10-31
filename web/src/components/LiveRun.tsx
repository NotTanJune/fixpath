import { lazy, Suspense, useEffect, useState } from "react";
import { mapOnce } from "../lib/api";
import type { FactMapData, Shown } from "../lib/factMap";
import { fallbackLine, type MapFailure } from "../lib/factsView";
import { queueLine } from "../lib/ingestSteps";
import { categoryOf } from "../lib/sourceGroups";
import { marked, RECORDED_END, type LiveState, type MapRun, type StepState } from "../lib/liveIngest";
import { PACE, paceFactor } from "../lib/pace";
import type { RunInfo } from "../lib/useIngestRun";
import { Step } from "./Step";

// three.js comes with the map, in a chunk of its own
const FactMap = lazy(() => import("./FactMap"));

const seconds = (s: StepState) => (s.seconds === null ? "" : `${s.seconds.toFixed(1)} s`);

/** The seven steps of adding a file, each lit in turn, with the seconds it took once it has ended. */
export function StepBar({ steps }: { steps: StepState[] }) {
  return (
    <ol className="run-steps" aria-label="Steps of adding the file">
      {steps.map((s) => (
        <li key={s.key} className={`run-step ${s.status}`} aria-current={s.status === "active" ? "step" : undefined}>
          <span className="run-step-name">{s.label}</span>
          <span className="run-step-sec num">{seconds(s)}</span>
        </li>
      ))}
    </ol>
  );
}

/** How many chunks are marked: one after another from when they arrive, at the pace; all of them at
 * pace 0. */
function useMarked(total: number, run: number): number {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    if (total === 0) {
      setShown(0);
      return;
    }
    const step = PACE.ingest.chunkMark * paceFactor();
    const start = performance.now();
    let timer = 0;
    const tick = () => {
      const n = marked(total, performance.now() - start, step);
      setShown(n);
      if (n < total) timer = window.setTimeout(tick, step);
    };
    tick();
    return () => window.clearTimeout(timer);
  }, [total, run]);
  return shown;
}

const SNIPPET = 200;

/** The file as the run has it: its chunks, in order, each marked in turn as it is cut. */
function ChunkPreview({ state }: { state: LiveState }) {
  const lit = useMarked(state.chunks.length, state.run);
  return (
    <div className="run-doc">
      {state.converted && (
        <p className="run-converted">
          Converted from {state.converted.from} to text, {state.converted.chars} characters.
        </p>
      )}
      {state.chunks.length === 0 && state.status === "running" && state.steps[1]!.status !== "done" && (
        <p className="quiet pad">Reading the file.</p>
      )}
      {state.chunks.length === 0 && state.steps[1]!.status === "done" && (
        <p className="quiet pad">A table or a log is read row by row, so it has no passages to cut.</p>
      )}
      {state.chunks.map((c, i) => (
        <section key={c.id} className={`run-chunk${i < lit ? " on" : ""}`} aria-label={`Chunk ${i + 1}`}>
          {c.page !== null && <span className="run-page num">Page {c.page}</span>}
          <p>
            {c.text}
            {c.text.length >= SNIPPET ? "..." : ""}
          </p>
        </section>
      ))}
    </div>
  );
}

/** The map of the run: every fact and passage, and the points this file adds. */
function RunMap({ run, shown, epoch }: { run: MapRun | null; shown: Shown | null; epoch: number }) {
  const [map, setMap] = useState<FactMapData | null>(null);
  const [failed, setFailed] = useState<MapFailure | null>(null);
  useEffect(() => {
    let live = true;
    setFailed(null);
    mapOnce()
      .then((m) => live && setMap(m))
      .catch(() => live && setFailed("load"));
    return () => {
      live = false;
    };
  }, [epoch]);
  if (failed) return <p className="quiet pad">{fallbackLine(failed)}</p>;
  if (!map) return <p className="quiet pad">Opening the map.</p>;
  return (
    <Suspense fallback={<p className="quiet pad">Opening the map.</p>}>
      <FactMap data={map} docId="" spans={[]} shown={shown} run={run} onPick={() => {}} onOpen={() => {}} onFail={setFailed} />
    </Suspense>
  );
}

/** What pane 2 shows while a file is being added: the steps, the file as it is cut into chunks, and
 * the map the file lands on. */
export function LiveRunPane({
  state,
  info,
  map,
  shown,
  epoch,
  onClose,
}: {
  state: LiveState;
  info: RunInfo;
  map: MapRun | null;
  shown: Shown | null;
  /** Counts the loads that put new points on the map, so that it is read again. */
  epoch: number;
  onClose: () => void;
}) {
  const facts = state.facts ? state.facts.nodes.length + state.facts.edges.length : null;
  const ended = state.status === "failed" || (state.status === "done" && info.kind === "recorded");
  let note: string | null = null;
  if (state.status === "failed") note = state.error;
  else if (state.status === "done") {
    note = info.kind === "recorded" ? RECORDED_END : state.loaded ? `Done. The graph now has ${state.loaded.nodes} nodes and ${state.loaded.edges} links.` : "Done.";
  } else if (state.queued && state.queued > 0 && state.steps[0]!.status !== "done") {
    note = queueLine(state.queued);
  }
  const category = state.doc ? categoryOf({ id: "", path: state.doc.path, doc_type: state.doc.type }) : "";
  return (
    <>
      <div className="gh">
        <h2>
          <Step n={2} /> File and facts
        </h2>
        {facts !== null && <span className="num">{facts} facts</span>}
      </div>
      <StepBar steps={state.steps} />
      {(note || ended) && (
        <div className="run-note-row">
          <p className={`run-note${state.status === "failed" ? " error" : ""}`} role="status">
            {note}
          </p>
          {ended && (
            <button type="button" className="run-close" onClick={onClose}>
              Back to the files
            </button>
          )}
        </div>
      )}
      <p className="file-line">
        <span className="file-name">{info.name}</span>
        {category && <span className="src-id">{category}</span>}
      </p>
      <div className="pl-doc">
        <ChunkPreview state={state} />
      </div>
      <section className="pl-facts" aria-label="Where the file lands on the map">
        <RunMap run={map} shown={shown} epoch={epoch} />
      </section>
    </>
  );
}
