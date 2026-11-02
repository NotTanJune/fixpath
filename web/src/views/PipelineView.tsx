import { ChevronRight, ExternalLink, Maximize2, PanelLeftClose, PanelLeftOpen, X } from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { DropZone } from "../components/DropZone";
import { Explorer } from "../components/Explorer";
import type { OpenSource } from "../components/IdLink";
import { LiveRunPane } from "../components/LiveRun";
import { RunPanel } from "../components/RunPanel";
import { SourceView, type SourceFocus } from "../components/SourceView";
import { Split } from "../components/Split";
import { trapTab } from "../components/SourceSheet";
import { Step } from "../components/Step";
import { mapOnce, sourceDoc, sourcesOnce } from "../lib/api";
import { firstSpanIndex, type FactMapData, type Shown } from "../lib/factMap";
import { fallbackLine, readFactsView, writeFactsView, type FactsView, type MapFailure } from "../lib/factsView";
import { browserStorage } from "../lib/history";
import { useMedia } from "../lib/hooks";
import { hrefFor } from "../lib/router";
import { REPORT_ARG } from "../lib/runReport";
import { ADDED, categoryOf, groupSources, openKeys, toggleGroup, type OpenState } from "../lib/sourceGroups";
import { edgeParts, factSentence, fixed, plainLabel } from "../lib/format";
import { mapRun } from "../lib/liveIngest";
import { sourceLabel } from "../lib/sourceName";
import { useIngestRun } from "../lib/useIngestRun";
import type { SourceDoc, SourceSummary, Span } from "../lib/types";

// three.js comes with the map, in a chunk of its own
const FactMap = lazy(() => import("../components/FactMap"));

function SourceList({
  sources,
  selected,
}: {
  sources: SourceSummary[];
  selected: string | undefined;
}) {
  const groups = useMemo(() => groupSources(sources), [sources]);
  const [state, setState] = useState<OpenState>({ at: undefined, keys: [] });
  const open = openKeys(state, groups, selected);
  const nav = useRef<HTMLElement>(null);

  // the open file is in view when the list opens on it
  useEffect(() => {
    nav.current?.querySelector<HTMLElement>("[aria-current]")?.scrollIntoView({ block: "nearest" });
  }, [selected, groups]);

  return (
    <nav ref={nav} className="sources" aria-label="Source files">
      <div className="sources-scroll">
        {groups.map((g) => {
          const isOpen = open.includes(g.key);
          const list = `src-${g.key}`;
          return (
            <section key={g.key} className="grp">
              <h3>
                <button
                  type="button"
                  aria-expanded={isOpen}
                  aria-controls={list}
                  onClick={() => setState(toggleGroup(state, groups, selected, g.key))}
                >
                  <ChevronRight className="chev" size={18} aria-hidden="true" />
                  <span className="grp-name">{g.label}</span>
                  <span className="num grp-count">{g.items.length}</span>
                </button>
              </h3>
              {isOpen && (
                <ul id={list}>
                  {g.items.map((s) => {
                    const label = sourceLabel(s);
                    return (
                      <li key={s.id}>
                        <a
                          href={hrefFor("ingestion", s.id)}
                          className={s.id === selected ? "src on" : "src"}
                          aria-current={s.id === selected ? "true" : undefined}
                          title={label.raw}
                        >
                          <span className="src-name">{label.name}</span>
                          <span className="src-id">{g.key === ADDED ? categoryOf(s) : label.id}</span>
                        </a>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </nav>
  );
}

/** The facts the file gave, one row each, in plain sentences. The row of the open fact is marked;
 * a row opens its fact in the document. */
function FactsTable({
  spans,
  active,
  onPick,
}: {
  spans: Span[];
  active: number | null;
  onPick: (index: number) => void;
}) {
  const body = useRef<HTMLTableSectionElement>(null);
  // a fact picked in the document brings its row into view
  useEffect(() => {
    if (active === null) return;
    body.current?.querySelector<HTMLElement>("tr.on")?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!spans.length) return <p className="quiet pad">No facts in this file.</p>;
  return (
    <table className="tbl fact-table" aria-label="Facts in this file">
      <thead>
        <tr>
          <th>Fact</th>
          <th className="r-al">Confidence</th>
        </tr>
      </thead>
      <tbody ref={body}>
        {spans.map((s, i) => {
          const edge = edgeParts(s.fact_id);
          return (
            <tr key={`${s.fact_id}-${i}`} className={i === active ? "on" : ""}>
              <td className="fact-cell">
                <button type="button" className="fact-pick" aria-pressed={i === active} onClick={() => onPick(i)}>
                  {edge ? (
                    <>
                      <span className="fact-sentence">{factSentence(s.fact_id)}</span>
                      <span className="fact-ids">
                        <span className="id">{edge.from}</span>
                        <span className="id">{edge.to}</span>
                      </span>
                    </>
                  ) : (
                    <span className="fact-sentence">
                      <span className="id">{s.fact_id}</span> {plainLabel(s.label)}
                    </span>
                  )}
                </button>
              </td>
              <td className="r-al num conf" title={`Read by ${s.extractor}`}>
                {fixed(s.confidence)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** The map at the size of the whole area below the top bar. It holds the focus: Tab stays inside,
 * Escape and Close shut it. A fact picked in it moves the document under it, and a point of another file opens that file; the
 * dialog stays. */
function FullMap({
  doc,
  map,
  shown,
  onPick,
  onOpen,
  onFail,
  onClose,
}: {
  doc: SourceDoc;
  map: FactMapData;
  shown: Shown | null;
  onPick: (index: number) => void;
  onOpen: (docId: string, factId: string | null) => void;
  onFail: (reason: MapFailure) => void;
  onClose: () => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    // until the map's canvas is there to take the focus, the dialog holds it
    root.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close.current();
      } else if (e.key === "Tab" && root.current) trapTab(e, root.current);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const name = sourceLabel(doc);
  // the same count as the caption: a fact quoted twice in the file is one fact
  const facts = new Set(doc.spans.map((s) => s.fact_id)).size;
  return (
    <div ref={root} className="fm-full" role="dialog" aria-modal="true" aria-label="Fact map, full size" tabIndex={-1}>
      <div className="gh fm-full-head">
        <h2 title={name.raw}>{name.name}</h2>
        <span className="num">
          {facts} {facts === 1 ? "fact" : "facts"}
        </span>
        <button type="button" className="icon-btn fm-close" onClick={onClose}>
          <X size={20} aria-hidden="true" />
          Close
        </button>
      </div>
      <div className="fm-full-body">
        <Suspense fallback={<p className="quiet">Opening the map.</p>}>
          <FactMap
            data={map}
            docId={doc.id}
            spans={doc.spans}
            shown={shown}
            onPick={(index) => {
              // going to a fact or a file leaves the full-size map, so the document is in view
              onClose();
              onPick(index);
            }}
            onOpen={(docId, factId) => {
              onClose();
              onOpen(docId, factId);
            }}
            onFail={onFail}
            autoFocus
          />
        </Suspense>
      </div>
    </div>
  );
}

/** The facts of the open file: the 3D map (default) or the list, with a switch. The list is the
 * full keyboard and screen reader path to every fact; the map falls back to it without WebGL. */
function FactsArea({
  doc,
  active,
  shown,
  full,
  setFull,
  onPick,
  onOpen,
}: {
  doc: SourceDoc;
  active: number | null;
  shown: Shown | null;
  /** The full-size map is open. It is kept above the file, which is gone while another one loads. */
  full: boolean;
  setFull: (open: boolean) => void;
  onPick: (index: number) => void;
  onOpen: (docId: string, factId: string | null) => void;
}) {
  const storage = browserStorage();
  const [view, setView] = useState<FactsView>(() => readFactsView(storage));
  const [map, setMap] = useState<FactMapData | null>(null);
  const [failed, setFailed] = useState<MapFailure | null>(null);
  const opener = useRef<HTMLButtonElement>(null);
  // a map that cannot be drawn leaves nothing for the dialog to show
  useEffect(() => {
    if (failed) setFull(false);
  }, [failed]);
  const closeFull = () => {
    setFull(false);
    opener.current?.focus();
  };
  useEffect(() => {
    if (view !== "map" || map || failed) return;
    mapOnce()
      .then(setMap)
      .catch(() => setFailed("load"));
  }, [view, map, failed]);
  const choose = (next: FactsView) => {
    setView(next);
    writeFactsView(storage, next);
    // a map that did not load is asked for again; a browser without WebGL stays on the list
    if (next === "map" && failed === "load") setFailed(null);
  };
  if (!doc.spans.length) return <p className="quiet pad">No facts in this file.</p>;
  // when the map cannot be drawn the list is what shows, and the switch says so
  const showing: FactsView = failed ? "list" : view;
  const opening = <p className="quiet pad">Opening the map.</p>;
  return (
    <>
      <div className="fm-bar">
        <div className="fm-switch" role="group" aria-label="Show the facts as">
          <button type="button" aria-pressed={showing === "map"} onClick={() => choose("map")}>
            Map
          </button>
          <button type="button" aria-pressed={showing === "list"} onClick={() => choose("list")}>
            List
          </button>
        </div>
        {showing === "map" && map && (
          <button
            ref={opener}
            type="button"
            className="icon-btn"
            aria-label="Open the map full size"
            title="Open the map full size"
            onClick={() => setFull(true)}
          >
            <Maximize2 size={20} aria-hidden="true" />
          </button>
        )}
      </div>
      {view === "map" && failed && <p className="quiet">{fallbackLine(failed)}</p>}
      {view === "map" && !failed && !map && opening}
      {view === "map" && !failed && map && full && (
        <FullMap doc={doc} map={map} shown={shown} onPick={onPick} onOpen={onOpen} onFail={setFailed} onClose={closeFull} />
      )}
      {view === "map" && !failed && map && !full && (
        <Suspense fallback={opening}>
          <FactMap
            data={map}
            docId={doc.id}
            spans={doc.spans}
            shown={shown}
            onPick={onPick}
            onOpen={onOpen}
            onFail={setFailed}
          />
        </Suspense>
      )}
      {(view === "list" || failed) && (
        <div className="fm-list">
          <FactsTable spans={doc.spans} active={active} onPick={onPick} />
        </div>
      )}
    </>
  );
}

/** The span of the file that a mark stands for, from the spans it names; the next one when the
 * first is the open one, so that a mark of several facts can be clicked through. */
export function spanOfMark(attr: string | undefined, active: number | null): number | null {
  const all = (attr ?? "")
    .split(" ")
    .filter(Boolean)
    .map(Number)
    .filter((n) => Number.isInteger(n) && n >= 0);
  if (!all.length) return null;
  return all.find((n) => n !== active) ?? all[0]!;
}

function SourcePane({
  doc,
  error,
  active,
  shown,
  full,
  setFull,
  setActive,
  onOpen,
}: {
  doc: SourceDoc | null;
  error: string | null;
  active: number | null;
  shown: Shown | null;
  full: boolean;
  setFull: (open: boolean) => void;
  setActive: (i: number | null) => void;
  onOpen: (docId: string, factId: string | null) => void;
}) {
  const focus = useMemo<SourceFocus>(() => {
    const s = doc && active !== null ? doc.spans[active] : undefined;
    if (!s) return { quotes: [], fact: null, page: null };
    return {
      quotes: s.quote.trim() ? [s.quote] : [],
      fact: s.fact_id,
      page: s.page,
      range: { start: s.start, end: s.end },
    };
  }, [doc, active]);

  // a mark of the document opens its fact in the table: by a click, or Enter on a focused mark
  const pickMark = (el: EventTarget) => {
    const mark = (el as Element).closest<HTMLElement>("[data-spans]");
    const at = spanOfMark(mark?.dataset.spans, active);
    if (at !== null) setActive(at);
  };

  return (
    <>
      <div className="gh">
        <h2>
          <Step n={2} /> File and facts
        </h2>
        {doc && <span className="num">{doc.spans.length} facts</span>}
      </div>
      <p className="file-line">
        {doc ? (
          <>
            <span className="file-name" title={sourceLabel(doc).raw}>
              {sourceLabel(doc).name}
            </span>
            <span className="src-id">{doc.id}</span>
            <a
              className="link file-original"
              href={`/api/sources/${encodeURIComponent(doc.id)}/file`}
              target="_blank"
              rel="noopener noreferrer"
              title={sourceLabel(doc).raw}
            >
              Open the original file
              <ExternalLink size={16} aria-hidden="true" />
            </a>
          </>
        ) : (
          "Opening the file"
        )}
      </p>
      <div
        className="pl-doc"
        onClick={(e) => pickMark(e.target)}
        onKeyDown={(e) => {
          if ((e.key === "Enter" || e.key === " ") && (e.target as Element).closest("[data-spans]")) {
            e.preventDefault();
            pickMark(e.target);
          }
        }}
      >
        {error && <p className="plain-error">{error}</p>}
        {!error && !doc && <p className="quiet pad">Opening the file.</p>}
        {doc && <SourceView doc={doc} focus={focus} />}
      </div>
      <section id="pl-facts" tabIndex={-1} className="pl-facts" aria-label="Facts in this file">
        {doc && <FactsArea
            doc={doc}
            active={active}
            shown={shown}
            full={full}
            setFull={setFull}
            onPick={setActive}
            onOpen={onOpen}
          />}
      </section>
    </>
  );
}

/** From this width the sources stay beside the other two panes; below it they are a column that
 * opens over them, as the rail of the Ask view does. */
const WIDE = "(min-width: 1280px)";
/** Below this width the three steps stack and the page scrolls. */
const SIDE_BY_SIDE = "(min-width: 860px)";

export function PipelineView({
  selected,
  report,
  replay,
}: {
  /** The file in the route, if it names one. */
  selected: string | undefined;
  /** The Run report page is open instead of the files. */
  report: boolean;
  /** The server runs without keys: it plays recorded ingests instead of reading new files. */
  replay: boolean;
  onOpen: OpenSource;
}) {
  const [sources, setSources] = useState<SourceSummary[]>([]);
  // counts the loads that changed the graph and the map, which are then read again
  const [epoch, setEpoch] = useState(0);
  const run = useIngestRun(
    (all) => {
      setSources(all);
      setEpoch((n) => n + 1);
    },
    (path) => {
      // a file that was added opens in the ordinary view, with its map and its graph
      const hit = path ? sourcesOnce().then((all) => all.find((s) => s.path === path)) : Promise.resolve(undefined);
      hit.then((s) => s && (window.location.hash = hrefFor("ingestion", s.id))).catch(() => {});
    },
  );
  const running = run.state.status !== "idle" && run.info !== null;
  // a picked file closes a run that has ended
  useEffect(() => {
    if (run.state.status === "done" || run.state.status === "failed") run.dismiss();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    sourcesOnce()
      .then(setSources)
      .catch((e: Error) => setError(e.message));
  }, []);

  const wide = useMedia(WIDE);
  const sideBySide = useMedia(SIDE_BY_SIDE);
  const [drawer, setDrawer] = useState(false);
  // a file that starts to be added is on the page: the drawer of the sources, if it is open, makes room
  useEffect(() => {
    if (run.info) setDrawer(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.info]);
  const strip = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const shut = () => {
    setDrawer(false);
    strip.current?.focus();
  };
  useEffect(() => {
    if (wide) setDrawer(false);
  }, [wide]);
  // the drawer: Escape closes it, Tab stays inside it
  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        shut();
      } else if (e.key === "Tab" && panel.current) trapTab(e, panel.current);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);
  // a file picked in the drawer is open: the drawer closes with it
  useEffect(() => {
    if (drawer) setDrawer(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  // the file stays open while the Run report is on the page, so that Files comes back to it
  const [last, setLast] = useState<string | undefined>(selected);
  useEffect(() => {
    if (selected) setLast(selected);
  }, [selected]);
  const current = selected ?? last ?? sources.find((s) => s.doc_type === "manual")?.id;
  const [doc, setDoc] = useState<SourceDoc | null>(null);
  const [docError, setDocError] = useState<string | null>(null);
  const [active, setActive] = useState<number | null>(null);
  // what the graph pane draws, which the fact map follows
  const [shown, setShown] = useState<Shown | null>(null);
  const [fullMap, setFullMap] = useState(false);
  // a click on a point of another file opens that file, and then its fact
  const pending = useRef<{ docId: string; factId: string | null } | null>(null);
  useEffect(() => {
    if (!current) return;
    let live = true;
    setDoc(null);
    setDocError(null);
    setActive(null);
    sourceDoc(current)
      .then((d) => {
        if (!live) return;
        setDoc(d);
        // set after the reset above, in the same render as the file
        const want = pending.current;
        if (want?.docId === d.id) {
          pending.current = null;
          const at = want.factId === null ? -1 : firstSpanIndex(d.spans, want.factId);
          if (at >= 0) setActive(at);
        }
      })
      .catch((e: Error) => live && setDocError(e.message));
    return () => {
      live = false;
    };
  }, [current]);
  const openAt = (docId: string, factId: string | null) => {
    pending.current = { docId, factId };
    window.location.hash = hrefFor("ingestion", docId);
  };
  // a node the viewer chose in the graph pane that the open file holds: the document jumps to it
  const centreOn = (id: string) => {
    const at = doc ? firstSpanIndex(doc.spans, id) : -1;
    if (at >= 0) setActive(at);
  };
  const pick = (path: string) => {
    const hit = sources.find((s) => s.path === path);
    if (hit) window.location.hash = hrefFor("ingestion", hit.id);
  };

  const left = (
    <div className="pl-left">
      <div className="gh">
        <h2>
          <Step n={1} /> Source files
        </h2>
        <span className="num">{sources.length} files</span>
      </div>
      <DropZone busy={run.busy} replay={replay} onFile={run.startLive} onWatch={run.startRecorded} />
      {error ? (
        <p className="plain-error pad">{error}</p>
      ) : (
        <SourceList sources={sources} selected={current} />
      )}
    </div>
  );
  const middle = (
    <div className="pl-mid">
      {running ? (
        <LiveRunPane
          state={run.state}
          info={run.info!}
          map={mapRun(run.state)}
          shown={shown}
          epoch={epoch}
          onClose={run.dismiss}
        />
      ) : current ? (
        <SourcePane
          doc={doc}
          error={docError}
          active={active}
          shown={shown}
          full={fullMap}
          setFull={setFullMap}
          setActive={setActive}
          onOpen={openAt}
        />
      ) : (
        <p className="quiet pad">Reading the file list.</p>
      )}
    </div>
  );
  const right = (
    <div className="pl-right">
      <Explorer
        doc={running ? null : doc}
        active={active}
        run={running ? run.state : null}
        epoch={epoch}
        onShown={setShown}
        onCentre={centreOn}
      />
    </div>
  );
  const inner = (first: ReactNode) => <Split id="pl-b" initial={0.512} minFirst={420} minSecond={360} first={first} second={right} />;

  let grid: ReactNode;
  if (!sideBySide) {
    grid = (
      <div className="pl-stack">
        {left}
        {middle}
        {right}
      </div>
    );
  } else if (wide) {
    grid = <Split id="pl-a" initial={0.18} minFirst={220} minSecond={420 + 360 + 24} first={left} second={inner(middle)} />;
  } else {
    grid = (
      <div className="pl-narrow">
        <div className="pl-strip">
          <button
            ref={strip}
            type="button"
            className="rail-toggle"
            aria-label="Show the source files"
            aria-expanded={drawer}
            onClick={() => setDrawer(true)}
          >
            <PanelLeftOpen size={20} aria-hidden="true" />
          </button>
        </div>
        <div className="pl-narrow-body">{inner(middle)}</div>
        {drawer && (
          <>
            <div className="rail-scrim" aria-hidden="true" onClick={shut} />
            <aside ref={panel} className="pl-drawer" aria-label="Source files" role="dialog" aria-modal="true">
              <button type="button" className="rail-toggle pl-drawer-close" aria-label="Close the source files" onClick={shut}>
                <PanelLeftClose size={20} aria-hidden="true" />
              </button>
              {left}
            </aside>
          </>
        )}
      </div>
    );
  }

  const show = (arg: string | undefined) => {
    window.location.hash = hrefFor("ingestion", arg);
  };

  return (
    <div className="pipeline">
      <div className="pl-top">
        <div className="fm-switch" role="group" aria-label="Show">
          <button type="button" aria-pressed={!report} onClick={() => show(current)}>
            Files
          </button>
          <button type="button" aria-pressed={report} onClick={() => show(REPORT_ARG)}>
            Run report
          </button>
        </div>
        {!report && (
          <p className="pl-lede">
            Pick a file to see its facts and where they sit in the graph.
          </p>
        )}
      </div>
      {report ? (
        <div className="pl-report">
          <RunPanel onPick={pick} />
        </div>
      ) : (
        <div className="pl-grid">{grid}</div>
      )}
    </div>
  );
}
