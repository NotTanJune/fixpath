import { useCallback, useEffect, useState } from "react";
import { SkipLinks } from "./components/SkipLinks";
import { SourceSheet, type SheetRequest } from "./components/SourceSheet";
import { TopBar } from "./components/TopBar";
import { getOntology, getSuggestions } from "./lib/api";
import { useAsk, useHashRoute, useHealth, useHistory } from "./lib/hooks";
import { OntologyContext } from "./lib/ontology";
import { reportView } from "./lib/runReport";
import type { Citation, Ontology, Suggestion } from "./lib/types";
import { AskView } from "./views/AskView";
import { PipelineView } from "./views/PipelineView";

export default function App() {
  const route = useHashRoute();
  const report = reportView(route);
  const { health, failed, refresh } = useHealth();
  const [ontology, setOntology] = useState<Ontology | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
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
  }, []);

  const open = useCallback(
    (label: string, citations: Citation[]) => setSheet({ label, citations }),
    [],
  );
  const close = useCallback(() => setSheet(null), []);

  return (
    <OntologyContext.Provider value={ontology}>
      {route.view === "ingestion" && !report && <SkipLinks />}
      <TopBar view={route.view} health={health} failed={failed} />
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
      <SourceSheet request={sheet} onClose={close} />
    </OntologyContext.Provider>
  );
}
