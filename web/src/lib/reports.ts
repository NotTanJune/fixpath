import type { NovelCase, NovelRecord } from "./novel";

export type Stage = "captured" | "checked" | "matched" | "merged";

/** A new issue a technician reported. It is held in the page only, so a reload loses it. */
export type Report = {
  id: string;
  question: string;
  record: NovelRecord;
  adds: NovelCase["adds"];
  stage: Stage;
  at: string;
};

export const STAGES: { stage: Stage; label: string; detail: string }[] = [
  {
    stage: "captured",
    label: "Captured",
    detail: "The report is saved with the technician and time as its source.",
  },
  {
    stage: "checked",
    label: "Checked",
    detail: "Every field fits the ontology: a cause, a fix, parts and a machine.",
  },
  {
    stage: "matched",
    label: "Matched",
    detail: "Compared with the graph. Nothing matches, so new nodes are made.",
  },
  {
    stage: "merged",
    label: "Merged",
    detail: "Added to the graph. The next technician who asks gets this fix.",
  },
];

/** The stage after this one, or null once the report is merged. */
export function nextStage(s: Stage): Stage | null {
  const i = STAGES.findIndex((x) => x.stage === s);
  return STAGES[i + 1]?.stage ?? null;
}

/** NEW-001, NEW-002, ... for the nth report of this session. */
export function reportId(n: number): string {
  return `NEW-${String(n).padStart(3, "0")}`;
}
