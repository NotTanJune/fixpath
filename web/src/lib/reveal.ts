// The paced reveal of an answer: which stage of it is on the screen now. Everything here is a
// pure function of the state of the run and a time that the caller passes in, so that a test can
// play a whole reveal against a fake clock. How long a stage holds is in PACE (lib/pace.ts),
// read through paced().

import { keyPart, needsProvisionalLockout, type AskState } from "./askReducer";
import { causeRank } from "./answerText";
import type { GEdge, GNode } from "./askGraph";
import { failureName } from "./format";
import { needsBand } from "./lockoutSteps";
import { PACE, paced, paceFactor, type PaceEnv } from "./pace";

/** The stages, in the order that the answer is told: the question read, the machine and alarm,
 * the root cause, the fix with its lockout band, the parts and their bin, the claims checked,
 * and the rest (summary, other causes, past breakdowns). */
export const STAGES = [
  "question",
  "machineAlarm",
  "rootCause",
  "fix",
  "partsBin",
  "claims",
  "done",
] as const;
export type Stage = (typeof STAGES)[number];
const at = (stage: Stage) => STAGES.indexOf(stage);

/** The reveal of one run: how many stages are shown (the first `shown`), and when the last one
 * was. `skipped` is set by "Show all now". */
export interface Reveal {
  shown: number;
  since: number;
  skipped: boolean;
}

export const START: Reveal = { shown: 0, since: 0, skipped: false };

// --- what has arrived ----------------------------------------------------------------

/** The status of the triage, from the card or the read of it that came first. */
function statusOf(state: AskState) {
  return state.card?.triage.status ?? state.triage?.status ?? null;
}

/** Whether the paced reveal tells this run. A question that the triage reads as one to ask back
 * about, or as out of scope, is a designed state with nothing to build up; a run that ends without
 * a read has nothing either. They show whole. */
export function revealApplies(state: AskState): boolean {
  const status = statusOf(state);
  if (status === "act" || status === "confirm") return true;
  return status === null && (state.phase === "running" || state.phase === "queued");
}

/** How many of the stages, from the first, have the data that they show. A stage can follow
 * only the ones before it. */
export function arrivedStages(state: AskState): number {
  const card = state.card;
  const has = (kind: string) => state.slots.some((s) => s.kind === kind);
  const decisions = state.triage?.decisions;
  const ready: boolean[] = [
    state.phase === "running" || state.phase === "done" || state.phase === "error",
    !!card || !!decisions?.machine || !!decisions?.alarm,
    !!card || has("cause"),
    !!card || has("fix"),
    !!card || has("part"),
    !!card,
    !!card,
  ];
  let n = 0;
  while (n < ready.length && ready[n]) n++;
  return n;
}

/** What the clock needs to know about a run: how many stages have arrived, and whether the pace
 * is set aside because the run is over, was saved, or has the factor 0. */
export function gate(state: AskState, env?: PaceEnv): { arrived: number; instant: boolean } {
  if (!revealApplies(state)) return { arrived: STAGES.length, instant: true };
  const instant = state.saved !== null || state.phase === "error" || paceFactor(env) === 0;
  return { arrived: arrivedStages(state), instant };
}

// --- the clock -------------------------------------------------------------------------

/** How long the stage before holds, at the current pace. */
function holdOf(stageIndex: number, env?: PaceEnv): number {
  const stage = STAGES[stageIndex - 1];
  return stage && stage !== "done" ? paced(PACE.stage[stage], env) : 0;
}

/** The reveal at time `now`. A stage shows when its data has arrived and the stage before it has
 * had its time; a stage whose data comes late shows when it comes, with no wait added. At most one
 * stage is added per call, unless the pace is set aside, when everything that has arrived shows. */
export function advance(r: Reveal, g: { arrived: number; instant: boolean }, now: number, env?: PaceEnv): Reveal {
  if (r.shown >= g.arrived) return r;
  if (g.instant || r.skipped) return { ...r, shown: g.arrived, since: now };
  if (r.shown > 0 && now - r.since < holdOf(r.shown, env)) return r;
  return { ...r, shown: r.shown + 1, since: now };
}

/** The time at which `advance` will add a stage, if nothing changes before; null when it waits for
 * data. A time that has passed is `now`. */
export function nextDue(
  r: Reveal,
  g: { arrived: number; instant: boolean },
  now: number,
  env?: PaceEnv,
): number | null {
  if (r.shown >= g.arrived) return null;
  if (g.instant || r.skipped || r.shown === 0) return now;
  return Math.max(now, r.since + holdOf(r.shown, env));
}

/** "Show all now". */
export function skip(r: Reveal): Reveal {
  return r.skipped ? r : { ...r, skipped: true };
}

// --- what is on the screen at a stage ---------------------------------------------------

export interface View {
  machineAlarm: boolean;
  rootCause: boolean;
  fix: boolean;
  parts: boolean;
  claims: boolean;
  done: boolean;
  /** Whether a fix, its steps or its ID, is on screen: it needs the band. */
  fixShown: boolean;
  /** The lockout band: the card's own, the one for the moments before it, or none. */
  band: "none" | "provisional" | "card";
}

/** What the answer column shows when the first `shown` stages are on screen. The band is there
 * whenever a fix could be: the card's with the fix stage, and before the card the provisional
 * one by the rule of the cleanup round (a fix or part found, or a hazardous machine) as soon as the
 * machine is on screen. */
export function viewOf(state: AskState, shown: number): View {
  const card = state.card;
  const v = (stage: Stage) => shown > at(stage);
  const band: View["band"] = card
    ? needsBand(card) && v("fix")
      ? "card"
      : "none"
    : needsProvisionalLockout(state) && v("machineAlarm")
      ? "provisional"
      : "none";
  return {
    machineAlarm: v("machineAlarm"),
    rootCause: v("rootCause"),
    fix: v("fix"),
    parts: v("partsBin"),
    claims: v("claims"),
    done: v("done"),
    fixShown:
      v("fix") &&
      (card ? !!card.fix || card.other_fixes.length > 0 : state.slots.some((s) => s.kind === "fix")),
    band,
  };
}

/** The summary the answer column shows when the first `shown` stages are on screen: it comes in
 * with the claims stage, in place, and before that it takes no room. */
export function summaryAt(state: AskState, shown: number): string | null {
  const text = state.card?.summary;
  return text && shown > at("claims") ? text : null;
}

/** The stage at which a node of the graph is drawn. The path builds up a stage at a time and the
 * page that cites the fix comes with the claims; the other facts that "Everything" adds are
 * drawn with the end. */
function nodeStage(n: GNode): Stage {
  if (n.layer === "extra") return "done";
  switch (n.type) {
    case "Asset":
    case "AlarmCode":
      return "machineAlarm";
    case "FailureMode":
      return "rootCause";
    case "Procedure":
      return "fix";
    case "Part":
    case "Bin":
      return "partsBin";
    case "Incident":
    case "Document":
      return "claims";
  }
}

/** The graph as it is drawn when the first `shown` stages are on screen: the nodes of the stages
 * that are shown, the edges whose two ends are drawn, and the words of those edges. The nodes keep
 * their places. */
export function revealedGraph<G extends { nodes: GNode[]; edges: GEdge[]; labels?: { edges: string[] }[] }>(
  graph: G,
  shown: number,
): G {
  const nodes = graph.nodes.filter((n) => shown > at(nodeStage(n)));
  const there = new Set(nodes.map((n) => n.id));
  const edges = graph.edges.filter((e) => there.has(e.source) && there.has(e.target));
  // the words of a fan-out are drawn as soon as one of its lines is
  const drawn = new Set(edges.map((e) => e.id));
  const labels = graph.labels?.filter((l) => l.edges.some((id) => drawn.has(id)));
  return { ...graph, nodes, edges, ...(labels ? { labels } : {}) };
}

// --- the narration -----------------------------------------------------------------------

/** The one line that tells the stage that has just come on screen, in plain words from the data of
 * the run. `facts` are what the graph says about the alarm, when it has said it. */
export function narration(
  state: AskState,
  shown: number,
  facts: { message: string | null; machineName: string | null } | null,
): string {
  if (shown <= 0) return "";
  const stage = STAGES[Math.min(shown, STAGES.length) - 1]!;
  const card = state.card;
  const d = state.triage?.decisions;
  const machine = card?.triage.asset_id ?? d?.machine ?? null;
  const alarm = card?.triage.alarm_code ?? d?.alarm ?? null;
  switch (stage) {
    case "question":
      return `Question read: ${state.question}`;
    case "machineAlarm": {
      const name = facts?.machineName ? `, ${facts.machineName}` : "";
      const code = alarm ? `, alarm ${alarm}${facts?.message ? ` ${facts.message}` : ""}` : "";
      return `Machine and alarm: ${machine ?? "not named"}${name}${code}`;
    }
    case "rootCause": {
      const id = card?.triage.failure_mode_id ?? null;
      const picked = card ? (card.causes.find((c) => c.failure_mode_id === id) ?? card.causes[0]) : null;
      if (picked) {
        const rank = causeRank(picked, card!.causes) === "Most likely root cause" ? "Most likely cause" : "Possible cause";
        const history =
          picked.of_total > 0 ? `${picked.support} of ${picked.of_total} past stops` : "from the manual only";
        return `${rank}: ${picked.name}, ${history}`;
      }
      const slot = state.slots.find((s) => s.kind === "cause");
      const name = slot?.id ? failureName(slot.id).toLowerCase() : "found";
      return slot?.id ? `Cause found: ${name}, checking the history` : "Cause found, checking the history";
    }
    case "fix": {
      const fix = card?.fix;
      if (fix) {
        const lock = needsBand(card!) ? " Lockout before you start." : "";
        return `Fix: ${fix.title}, ${fix.steps.length} steps, ${fix.est_minutes} min.${lock}`;
      }
      const slot = state.slots.find((s) => s.kind === "fix");
      return `Fix found${slot?.id ? `: ${slot.id}` : ""}. Lockout before you start.`;
    }
    case "partsBin": {
      if (!card) return "Spare parts found";
      const key = keyPart(card);
      if (!key) return "No spare parts needed";
      const more = card.parts.length > 1 ? `, and ${card.parts.length - 1} more` : "";
      const bin = key.bin ? ` from bin ${key.bin}` : "";
      const stock =
        key.qty_on_hand === null
          ? "stock unknown"
          : key.enough
            ? `${key.qty_on_hand} on hand`
            : `short, only ${key.qty_on_hand} on hand`;
      return `Parts: ${key.part_no}${bin}, ${stock}${more}`;
    }
    case "claims": {
      if (!card) return "Checking the claims against their sources";
      const kept = card.claims.length;
      const lost = card.unverified.length;
      const total = kept + lost;
      return `Checking ${total} ${total === 1 ? "claim" : "claims"} against their sources: ${kept} kept, ${lost} unverified`;
    }
    case "done":
      return "Answer ready";
  }
}

