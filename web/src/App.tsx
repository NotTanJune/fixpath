import { useCallback, useEffect, useState } from "react";
import { SkipLinks } from "./components/SkipLinks";
import { SourceSheet, type SheetRequest } from "./components/SourceSheet";
import { TopBar } from "./components/TopBar";
import { UpkeepPanel } from "./components/UpkeepPanel";
import { getOntology, getSuggestions, getUpkeep } from "./lib/api";
import { useAsk, useHashRoute, useHealth, useHistory } from "./lib/hooks";
import { OntologyContext } from "./lib/ontology";
import { reportView } from "./lib/runReport";
import type { Citation, Ontology, Suggestion, Upkeep } from "./lib/types";
import { dueCount } from "./lib/upkeep";
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
            onOpen={open}
          />
        )}
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
