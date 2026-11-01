import { useCallback, useEffect, useState } from "react";
import { SkipLinks } from "./components/SkipLinks";
import { SourceSheet, type SheetRequest } from "./components/SourceSheet";
import { TopBar } from "./components/TopBar";
import { UpkeepPanel } from "./components/UpkeepPanel";
import { getOntology, getSuggestions, getUpkeep } from "./lib/api";
import { useAsk, useHashRoute, useHealth, useHistory } from "./lib/hooks";
import { OntologyContext } from "./lib/ontology";
import { paced } from "./lib/pace";
import { nextStage, reportId, type Report } from "./lib/reports";
import { reportView } from "./lib/runReport";
import type { NovelCase, NovelRecord } from "./lib/novel";
import type { Citation, Ontology, Suggestion, Upkeep } from "./lib/types";
import { dueCount } from "./lib/upkeep";
import { NewIssuesView } from "./views/NewIssuesView";
import { AskView } from "./views/AskView";
import { PipelineView } from "./views/PipelineView";

export default function App() {
  const route = useHashRoute();
  const report = reportView(route);
  const { health, failed, refresh } = useHealth();
  const [ontology, setOntology] = useState<Ontology | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [upkeep, setUpkeep] = useState<Upkeep | null>(null);
  const [upkeepFailed, setUpkeepFailed] = useState(false);
  const [upkeepOpen, setUpkeepOpen] = useState(false);
  const [sheet, setSheet] = useState<SheetRequest | null>(null);
  // the new issues reported in this session: held here only, lost on reload
  const [issues, setIssues] = useState<Report[]>([]);
  const history = useHistory();
  const ask = useAsk(refresh, history.record);

  useEffect(() => {
    getOntology()
      .then(setOntology)
      .catch(() => setOntology(null));
    getSuggestions()
      .then(setSuggestions)
      .catch(() => setSuggestions([]));
    getUpkeep()
      .then(setUpkeep)
      .catch(() => setUpkeepFailed(true));
  }, []);

  // a report moves captured, checked, matched, merged: one step each 900 ms, all at once when
  // the reveal is switched off (prefers-reduced-motion or ?pace=0)
  useEffect(() => {
    const live = issues.find((r) => nextStage(r.stage));
    if (!live) return;
    const step = (r: Report): Report => {
      const n = nextStage(r.stage);
      return n ? { ...r, stage: n } : r;
    };
    const wait = paced(900);
    if (wait <= 0) {
      setIssues((all) =>
        all.map((r) => {
          let x = r;
          while (nextStage(x.stage)) x = step(x);
          return x;
        }),
      );
      return;
    }
    const t = setTimeout(
      () => setIssues((all) => all.map((r) => (r.id === live.id ? step(r) : r))),
      wait,
    );
    return () => clearTimeout(t);
  }, [issues]);

  const sendIssue = useCallback(
    (novel: NovelCase, record: NovelRecord): string => {
      const id = reportId(issues.length + 1);
      setIssues((all) => [
        ...all,
        { id, question: novel.question, record, adds: novel.adds, stage: "captured", at: new Date().toISOString() },
      ]);
      return id;
    },
    [issues.length],
  );

  const open = useCallback(
    (label: string, citations: Citation[]) => {
      setUpkeepOpen(false);
      setSheet({ label, citations });
    },
    [],
  );
  const close = useCallback(() => setSheet(null), []);
  const closeUpkeep = useCallback(() => setUpkeepOpen(false), []);

  return (
    <OntologyContext.Provider value={ontology}>
      {route.view === "ingestion" && !report && <SkipLinks />}
      <TopBar
        view={route.view}
        health={health}
        failed={failed}
        due={upkeep ? dueCount(upkeep) : 0}
        upkeepOpen={upkeepOpen}
        onUpkeep={() => setUpkeepOpen((o) => !o)}
      />
      <main className={`view view-${route.view}`}>
        {route.view === "ask" && (
          <AskView
            ask={ask}
            history={history}
            health={health}
            suggestions={suggestions}
            upkeep={upkeep}
            onUpkeep={() => setUpkeepOpen(true)}
            onOpen={open}
            issues={issues}
            onReport={sendIssue}
          />
        )}
        {route.view === "issues" && <NewIssuesView issues={issues} replay={health?.replay_mode ?? false} />}
        {route.view === "ingestion" && (
          <PipelineView
            selected={report ? undefined : route.arg}
            report={report}
            replay={health?.replay_mode ?? false}
            onOpen={open}
          />
        )}
      </main>
      <UpkeepPanel
        open={upkeepOpen}
        upkeep={upkeep}
        failed={upkeepFailed}
        onClose={closeUpkeep}
        onOpen={open}
      />
      <SourceSheet request={sheet} onClose={close} />
    </OntologyContext.Provider>
  );
}
