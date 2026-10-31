import { ArrowLeft, Maximize2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { getGraph } from "../lib/api";
import { leafRows, TEXT_X, textRows } from "../lib/askGraph";
import {
  backOf,
  centreCaption,
  centreForFile,
  degreesOf,
  fallbackCentre,
  factNodeIds,
  groupPicker,
  mergeGraph,
  navTo,
  type Nav,
} from "../lib/explorerFocus";
import { columnsOf, explorerPlan, shownOf, type ExEdge, type ExNode } from "../lib/explorerGraph";
import { sentenceCase } from "../lib/format";
import { flashIds, freshIds, runCentre, runGraph, type LiveState } from "../lib/liveIngest";
import { glyphOf, labelOf, relationLabel, toneOf, useOntology } from "../lib/ontology";
import type { SourceDoc } from "../lib/types";
import { CyGraph, type CyEdge, type CyNode } from "./CyGraph";
import { NodePicker } from "./NodePicker";
import { trapTab } from "./SourceSheet";
import { Step } from "./Step";

/** The graph at the size of the whole area below the top bar. It holds the focus: Tab stays inside,
 * Escape and Close shut it. */
function FullFrame({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const root = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    // until the graph has something to take the focus, the dialog holds it
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
  return (
    <div ref={root} className="fm-full ex-full" role="dialog" aria-modal="true" aria-label="Graph, full size" tabIndex={-1}>
      {children}
    </div>
  );
}

/** What the graph knows about one node, and how it connects. It follows the file that is open: it
 * centres on the most connected node that the file names, or on the node of the fact that is
 * picked. A node that is clicked becomes the centre, and Back returns to the one before. */
export function Explorer({
  doc,
  active,
  run = null,
  epoch = 0,
  onShown,
  onCentre,
}: {
  doc: SourceDoc | null;
  active: number | null;
  /** A file is being added: the graph gains what it adds, and centres on the most connected node it
   * matched or added. The new nodes pulse and the matched ones flash. */
  run?: LiveState | null;
  /** Counts the loads that changed the graph, so that it is read again. */
  epoch?: number;
  /** What the pane draws now: its centre, and the ids of the nodes and lines drawn. */
  onShown?: (shown: { centre: string; ids: string[] }) => void;
  /** The viewer chose this node as the centre, by a click or in the picker. */
  onCentre?: (id: string) => void;
}) {
  const ontology = useOntology();
  const [stored, setStored] = useState<{ nodes: ExNode[]; edges: ExEdge[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nav, setNav] = useState<Nav | null>(null);
  const [opened, setOpened] = useState<{ centre: string; keys: Set<string> }>({ centre: "", keys: new Set() });
  const [query, setQuery] = useState("");
  // the graph at full size: the same centre, trail and opened branches, drawn in a dialog
  const [full, setFull] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const closeFull = () => {
    setFull(false);
    opener.current?.focus();
  };

  useEffect(() => {
    let live = true;
    getGraph()
      .then((g) => {
        if (!live) return;
        setError(null);
        setStored({
          nodes: g.nodes.map((n) => ({ id: n.data.id, label: n.data.label, type: n.data.type })),
          edges: g.edges.map((e) => ({ id: e.data.id, source: e.data.source, target: e.data.target, rel: e.data.rel })),
        });
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [epoch]);

  // what the file being added puts in the graph shows before the graph is read again
  const added = useMemo(() => (run ? runGraph(run) : null), [run?.placed]);
  const graph = useMemo(() => (stored ? mergeGraph(stored, added) : null), [stored, added]);
  const fresh = useMemo(() => new Set(run ? freshIds(run) : []), [run?.matched, run?.placed]);
  const flash = useMemo(() => new Set(run ? flashIds(run) : []), [run?.matched]);

  const degree = useMemo(() => degreesOf(graph?.edges ?? []), [graph]);
  const byId = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const inFile = useMemo(() => new Set(factNodeIds(doc?.spans ?? [])), [doc]);

  // the centre follows the file and the fact that is picked; a new file starts a new trail
  const target = useMemo(() => {
    if (!graph) return null;
    if (run) return runCentre(run, degree);
    if (!doc) return null;
    return centreForFile(doc.spans, degree, active) ?? fallbackCentre(graph.nodes);
  }, [graph, doc, degree, active, run]);
  // a run is a scope of its own, as a file is
  const scope = run ? `run ${run.run}` : (doc?.id ?? null);
  const lastDoc = useRef<string | null>(null);
  useEffect(() => {
    if (!target || !scope) return;
    const same = lastDoc.current === scope;
    lastDoc.current = scope;
    setNav((n) => (n && same ? navTo(n, target) : { centre: target, back: [] }));
  }, [target, scope]);

  const wordOf = (type: string) => labelOf(type, ontology);
  const plan = useMemo(() => {
    if (!graph || !nav) return null;
    return explorerPlan({
      nodes: graph.nodes,
      edges: graph.edges,
      centre: nav.centre,
      wordOf: (type) => labelOf(type, ontology),
      forward: (rel) => relationLabel(rel, ontology),
      expanded: opened.centre === nav.centre ? opened.keys : new Set(),
    });
  }, [graph, nav, ontology, opened]);

  // the fact map follows what the pane draws
  const shown = useRef(onShown);
  shown.current = onShown;
  useEffect(() => {
    if (plan && plan.magnets.length) shown.current?.(shownOf(plan));
  }, [plan]);

  const nodes = useMemo<CyNode[]>(() => {
    if (!plan) return [];
    const real = plan.magnets.map((m) => ({
      id: m.id,
      label: m.name.join(" "),
      rich: {
        kind: m.kind,
        name: m.name,
        detail: m.detail,
        textX: TEXT_X,
        rows: (m.kind ? textRows : leafRows)({ h: m.h, nameLines: m.name, detailLines: m.detail }),
      },
      tone: toneOf(m.type, ontology),
      glyph: glyphOf(m.type, ontology),
      w: m.w,
      h: m.h,
      x: m.x,
      y: m.y,
      focus: m.centre,
      mark: inFile.has(m.id),
      pulse: fresh.has(m.id) ? ("new" as const) : flash.has(m.id) ? ("match" as const) : undefined,
    }));
    const more = plan.more.map((c) => ({
      id: c.id,
      label: c.label,
      tone: "so" as const,
      glyph: "archive",
      w: c.w,
      x: c.x,
      y: c.y,
      dashed: true,
      dense: true,
    }));
    // a branch is a pill: the icon of its type, or the clock for past incidents
    const pills = plan.pills.map((p) => ({
      id: p.id,
      label: `${p.words}\n${p.countText}`,
      tone: toneOf(p.type, ontology),
      glyph: p.past ? "clock" : glyphOf(p.type, ontology),
      w: p.w,
      h: p.h,
      x: p.x,
      y: p.y,
      pill: true,
    }));
    const dots = plan.junctions.map((j) => ({ id: j.id, label: "", tone: "so" as const, glyph: "", w: 1, x: j.x - 0.5, y: j.y, dot: true }));
    return [...real, ...more, ...pills, ...dots];
  }, [plan, ontology, inFile, fresh, flash]);
  const edges = useMemo<CyEdge[]>(() => {
    if (!plan) return [];
    const wires = plan.wires.map((w) => ({ id: `wire:${w.id}`, source: w.from, target: w.to, px: w.px }));
    // a faint curve between two nodes of different branches, bent to one side or the other
    const cross = plan.cross.map((c, i) => ({ id: c.id, source: c.from, target: c.to, cross: (i % 2 ? -1 : 1) * 36 }));
    return [...cross, ...wires];
  }, [plan]);

  const columns = useMemo(() => (plan ? columnsOf(plan) : []), [plan]);
  const centre = nav ? byId.get(nav.centre) : undefined;
  const centreName = centre ? (centre.type === "FailureMode" ? sentenceCase(centre.label) : centre.id) : "";
  const groups = useMemo(() => groupPicker(graph?.nodes ?? [], query, (type) => labelOf(type, ontology)), [graph, query, ontology]);
  const shownText = centre
    ? centre.type === "FailureMode"
      ? sentenceCase(centre.label)
      : centre.label && centre.label !== centre.id
        ? `${centre.id} ${centre.label}`
        : centre.id
    : "";
  const links = plan ? plan.lines.length + plan.cross.length : 0;

  const go = (id: string) => {
    if (id.startsWith("more:")) {
      setOpened((o) => {
        const keys = new Set(o.centre === nav?.centre ? o.keys : []);
        keys.add(id.slice("more:".length));
        return { centre: nav?.centre ?? "", keys };
      });
      return;
    }
    setNav((n) => (n ? navTo(n, id) : n));
    onCentre?.(id);
  };

  // the picker and Back, for the pane and for the dialog, each with its own label id
  const centreRow = (labelId: string, after?: ReactNode) => (
    <div className="ex-row">
      <span id={labelId} className="ex-label">
        Centred on
      </span>
      <NodePicker groups={groups} valueText={shownText} labelledBy={labelId} onQuery={setQuery} onPick={go} />
      <button type="button" className="ex-back" disabled={!nav || nav.back.length === 0} onClick={() => setNav((n) => (n ? backOf(n) : n))}>
        <ArrowLeft size={18} aria-hidden="true" />
        Back
      </button>
      {after}
    </div>
  );
  const caption = centre && <p className="explorer-caption">{centreCaption(wordOf(centre.type), centreName)}</p>;
  // one graph at a time: the pane's is gone while the dialog has its own
  const tree = plan && (
    <CyGraph
      key={nav?.centre}
      nodes={nodes}
      edges={edges}
      interactive
      anchor={nav?.centre}
      columns={columns}
      keepView
      label={`Graph of ${plan.magnets.length} nodes around ${centreName}`}
      onNode={go}
    />
  );
  const legend =
    inFile.size > 0 ? (
      <div className="legend" aria-label="Key">
        <span className="lg-item">
          <span className="lg-dot" aria-hidden="true" />
          In this file
        </span>
      </div>
    ) : null;

  return (
    <section id="pl-graph" className="explorer" aria-label="In the graph">
      <div className="gh">
        <h2>
          <Step n={3} /> In the graph
        </h2>
        {plan && (
          <span className="num">
            {plan.magnets.length} nodes &middot; {links} links
          </span>
        )}
      </div>
      <div className="explorer-bar">
        {centreRow(
          "ex-centre",
          plan && (
            <button
              ref={opener}
              type="button"
              className="icon-btn"
              aria-label="Open the graph full size"
              title="Open the graph full size"
              onClick={() => setFull(true)}
            >
              <Maximize2 size={20} aria-hidden="true" />
            </button>
          ),
        )}
      </div>
      {caption}
      <div className="explorer-plane">
        {error && <p className="plain-error pad">{error}</p>}
        {!error && !plan && <p className="quiet pad">Reading the graph.</p>}
        {!full && tree}
      </div>
      {legend}
      {full && plan && (
        <FullFrame onClose={closeFull}>
          <div className="gh fm-full-head">
            {centreRow("ex-centre-full")}
            <button type="button" className="icon-btn fm-close" onClick={closeFull}>
              <X size={20} aria-hidden="true" />
              Close
            </button>
          </div>
          <div className="ex-full-body">
            {caption}
            <div className="explorer-plane">{tree}</div>
            {legend}
          </div>
        </FullFrame>
      )}
    </section>
  );
}
