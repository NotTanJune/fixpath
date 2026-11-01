import { useEffect, useMemo, useRef, useState } from "react";
import type { AskState } from "../lib/askReducer";
import { buildAskGraph, textRows, TEXT_X, type Facts, type GraphView } from "../lib/askGraph";
import { viewOf, type HeldView } from "../lib/graphView";
import { sourceLabel } from "../lib/format";
import { PACE, paced } from "../lib/pace";
import { revealedGraph } from "../lib/reveal";
import type { TechBreakdown } from "../lib/writeBack";
import { glyphOf, Glyph, toneOf, useOntology } from "../lib/ontology";
import type { OpenSource } from "./IdLink";
import { CyGraph, type CyEdge, type CyLabel, type CyNode, type Frame } from "./CyGraph";

/** What the three kinds of line mean in "Everything": the colours need no key now that every magnet
 * says what it is. The key sits by the view control, at a size that is read without looking for it. */
function LineKey() {
  return (
    <ul className="gkey" aria-label="Lines">
      <li>
        <svg width="40" height="12" aria-hidden="true">
          <path d="M0 6H40" stroke="#1F4FD1" strokeWidth="9" />
          <path d="M0 6H40" stroke="#F9F5D7" strokeWidth="4" />
        </svg>
        Answer path
      </li>
      <li>
        <svg width="40" height="12" aria-hidden="true">
          <path d="M0 6H40" stroke="#1D2023" strokeWidth="2" strokeDasharray="6 5" />
        </svg>
        Other cause
      </li>
      <li>
        <svg width="40" height="12" aria-hidden="true">
          <path d="M1 6H40" stroke="#5A6168" strokeWidth="2" strokeDasharray="2 3" />
        </svg>
        Cited page
      </li>
    </ul>
  );
}

function emptyNote(state: AskState): string {
  const status = state.card?.triage.status ?? state.triage?.status;
  if (status === "clarify") return "No path yet. Pick one above to draw it.";
  if (status === "out_of_scope") return "No path for this question.";
  if (state.phase === "error") return "No path, the question did not finish.";
  return "The path to the fix draws here.";
}

/** The graph of the answer, built up a stage at a time as the reveal tells it: a node that has
 * not been revealed is not drawn. The picture is fitted to the whole graph of the view from the
 * start, so that it does not move as nodes arrive. */
export function GraphPanel({
  state,
  shown,
  onOpen,
  refit,
  facts,
  technician,
}: {
  state: AskState;
  shown: number;
  onOpen: OpenSource;
  refit?: number;
  facts?: Facts | null;
  technician?: TechBreakdown[];
}) {
  const ontology = useOntology();
  // what the reader chose belongs to this answer: the next question opens on the path again
  const [held, setHeld] = useState<HeldView | null>(null);
  const view = viewOf(held, state.run);
  const choose = (next: GraphView) => setHeld({ run: state.run, view: next });
  // the width of the pane the graph is drawn in: the path is sized to it
  const plane = useRef<HTMLDivElement>(null);
  const [paneWidth, setPaneWidth] = useState<number | undefined>(undefined);
  useEffect(() => {
    const el = plane.current;
    if (!el) return;
    const read = () => setPaneWidth(el.clientWidth > 0 ? el.clientWidth : undefined);
    read();
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const message = facts?.message ?? null;
  const machineName = facts?.machineName ?? null;
  const whole = useMemo(
    () => buildAskGraph(state, { view, ontology, facts: { message, machineName }, paneWidth, technician }),
    [state, view, ontology, message, machineName, paneWidth, technician],
  );
  const graph = useMemo(() => revealedGraph(whole, shown), [whole, shown]);
  const byId = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);
  const frame = useMemo<Frame | null>(() => {
    if (!whole.nodes.length) return null;
    const left = Math.min(...whole.nodes.map((n) => n.x));
    const right = Math.max(...whole.nodes.map((n) => n.x + n.w));
    // with nothing beside the path the frame is mirrored about it, so that the path is centred
    const beside = whole.nodes.some((n) => n.side);
    const reach = Math.max(-left, right);
    return {
      x1: beside ? left : -reach,
      x2: beside ? right : reach,
      y1: Math.min(...whole.nodes.map((n) => n.y - n.h / 2)),
      y2: Math.max(...whole.nodes.map((n) => n.y + n.h / 2)),
    };
  }, [whole]);
  const snapMs = paced(PACE.nodeSnap);
  const drawMs = paced(PACE.edgeDraw);

  const nodes = useMemo<CyNode[]>(
    () =>
      graph.nodes.map((n) => ({
        id: n.id,
        label: n.label,
        rich: {
          kind: n.kind,
          name: n.nameLines,
          detail: n.detailLines,
          textX: TEXT_X,
          rows: textRows(n),
        },
        tone: toneOf(n.type, ontology),
        glyph: glyphOf(n.type, ontology),
        w: n.w,
        h: n.h,
        x: n.x,
        y: n.y,
        dashed: n.dashed,
        lit: n.lit !== null,
      })),
    [graph, ontology],
  );
  const edges = useMemo<CyEdge[]>(
    () =>
      graph.edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        dashed: e.dashed,
        dotted: e.dotted,
        turn: e.turn,
        // a line that leaves a fix right of its middle, for the page that hangs under it
        ...(e.from !== undefined ? { from: e.from, fromY: (byId.get(e.source)?.h ?? 0) / 2 } : {}),
        lit: e.lits.length > 0,
      })),
    [graph, byId],
  );

  const labels = useMemo<CyLabel[]>(
    () => graph.labels.map((l) => ({ id: `label:${l.id}`, text: l.text, x: l.x, y: l.y, w: l.w, h: l.h })),
    [graph],
  );

  const count = graph.nodes.length;
  const label = count
    ? `Graph of ${count} nodes: ${graph.nodes
        .filter((n) => n.lit)
        .map((n) => n.label)
        .join(", then ")}`
    : "Graph, empty until a question is answered";
  return (
    <section className="graph" aria-label="Knowledge graph">
      <div className="gh">
        <h2>Knowledge graph</h2>
        <div className="gview" role="group" aria-label="What the graph shows">
          {(
            [
              ["path", "Answer path"],
              ["all", "Everything"],
            ] as const
          ).map(([value, text]) => (
            <button
              key={value}
              type="button"
              aria-pressed={view === value}
              onClick={() => choose(value)}
            >
              {text}
            </button>
          ))}
        </div>
        {view === "all" && <LineKey />}
      </div>
      <div className="graph-plane" ref={plane} role="region" tabIndex={0} aria-label="Graph, scrolls when taller than the panel">
        {count === 0 && (
          <p className="graph-empty">
            <Glyph name="unlink" size={16} /> {emptyNote(state)}
          </p>
        )}
        <CyGraph
          nodes={nodes}
          edges={edges}
          labels={labels}
          label={label}
          refit={refit}
          snapMs={snapMs}
          drawMs={drawMs}
          frame={frame}
          onNode={(id) => {
            const n = byId.get(id);
            const first = n?.citations[0];
            if (n && first)
              onOpen(
                n.type === "Document" ? sourceLabel(first.doc_path, first.page) : n.id,
                n.citations,
              );
          }}
        />
      </div>
    </section>
  );
}
