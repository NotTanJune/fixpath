import { ArrowRight, Archive, Plus } from "lucide-react";
import { useEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import {
  ChipGroups,
  Flags,
  FixSection,
  fixesAlongside,
  Lockout,
  MainIssue,
  OtherCauses,
  PartsList,
  PastBreakdowns,
  ProvisionalLockout,
  RootCause,
  splitCauses,
  StockList,
  Summary,
  UpkeepNote,
} from "../components/answer";
import { AgentTrace } from "../components/AgentTrace";
import { Rail } from "../components/Rail";
import { WriteBack } from "../components/WriteBack";
import { GraphPanel } from "../components/GraphPanel";
import type { OpenSource } from "../components/IdLink";
import { Split } from "../components/Split";
import { canShowRevealAgain, canStartOver, hasGraph } from "../lib/askLayout";
import { composeAsk, errorText, failureName, MAX_QUESTION, sentenceCase } from "../lib/format";
import { PACE, paceFactor, paced } from "../lib/pace";
import { arrivedStages, narration, revealApplies, STAGES, summaryAt, viewOf } from "../lib/reveal";
import { useAlarmFacts, useFollowReveal, useReveal } from "../lib/hooks";
import type { AskState } from "../lib/askReducer";
import { savedAt, type SavedRun } from "../lib/history";
import type { Chip, Health, Suggestion, Upkeep } from "../lib/types";
import { itemFor } from "../lib/upkeep";
import { newBreakdown, type TechBreakdown } from "../lib/writeBack";

const NODE_WORDS: Record<string, string> = {
  triage: "Reading the question",
  graph_agent: "Looking up the graph",
  manual_agent: "Reading the manuals",
  synthesize: "Writing the summary",
  verify: "Checking the summary against its sources",
  assemble: "Putting the answer together",
};

const FIELD_WORDS: Record<string, string> = {
  root_cause: "root cause",
  safety_critical: "hazard",
  machine: "machine",
  alarm: "alarm",
};

/** A note that a part is short is said plainly in the parts list, so it is not a flag too. */
function shortPartNote(note: string, parts: { part_no: string; enough: boolean }[]): boolean {
  return note.includes("not enough in stock") && parts.some((p) => !p.enough && note.startsWith(p.part_no));
}

function unsure(chips: Chip[]): string {
  const names = [...new Set(chips.map((c) => FIELD_WORDS[c.field] ?? c.field))];
  return names.join(" and ");
}

function Working({ state }: { state: AskState }) {
  if (state.phase === "queued") {
    const n = state.queued ?? 0;
    return (
      <p className="working queued">
        <span className="dash-sq" aria-hidden="true" />
        {n <= 0
          ? "Waiting for your turn."
          : `Waiting for your turn. ${n} ${n === 1 ? "question" : "questions"} ahead.`}
      </p>
    );
  }
  if (state.phase !== "running") return null;
  return (
    <p className="working">
      <span className="dash-sq" aria-hidden="true" />
      {state.active ? (NODE_WORDS[state.active] ?? state.active) : "Reading the question"}
    </p>
  );
}

export function AskView({
  ask,
  history,
  health,
  suggestions,
  upkeep,
  onUpkeep,
  onOpen,
}: {
  ask: {
    state: AskState;
    send: (q: string) => void;
    stop: () => void;
    restore: (run: SavedRun) => void;
    reset: () => void;
  };
  history: { runs: SavedRun[]; clear: () => void };
  health: Health | null;
  suggestions: Suggestion[];
  upkeep: Upkeep | null;
  onUpkeep: () => void;
  onOpen: OpenSource;
}) {
  const { state, send, stop, restore, reset } = ask;
  const [draft, setDraft] = useState("");
  const [asked, setAsked] = useState(0);
  const { shown, skipAll } = useReveal(state);
  const pane = useRef<HTMLElement>(null);
  useFollowReveal(pane, shown, state.run);
  const busy = state.phase === "running" || state.phase === "queued";
  const replay = health?.replay_mode ?? false;
  const card = state.card;
  const status = card?.triage.status ?? state.triage?.status ?? null;
  const open =
    status === "act" || status === "confirm" || (status === null && state.phase !== "idle");
  const idle = state.phase === "idle";
  const side = hasGraph(state);
  const [refit, setRefit] = useState(0);
  const field = useRef<HTMLInputElement>(null);
  // what a technician reported on this answer: held here only, so a new question clears it
  const [reports, setReports] = useState<TechBreakdown[]>([]);
  useEffect(() => setReports([]), [state.run]);

  // the form moves when the screen changes layout, and the keyboard stays in it
  const before = useRef({ idle, side });
  useEffect(() => {
    const moved = before.current.idle !== idle || before.current.side !== side;
    if (moved && !idle && document.activeElement === document.body) field.current?.focus();
    before.current = { idle, side };
  }, [idle, side]);

  // "New question" empties the field and leaves the keyboard in it once the idle screen is up
  const startOver = useRef(false);
  useEffect(() => {
    if (idle && startOver.current) {
      startOver.current = false;
      field.current?.focus();
    }
  }, [idle]);
  const newQuestion = () => {
    startOver.current = true;
    setDraft("");
    reset();
  };

  // every question that is sent is counted, so that the rail can make room for its answer
  const start = (q: string) => {
    setAsked((n) => n + 1);
    send(q);
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (busy) return stop();
    const q = draft.trim();
    if (q) start(q.slice(0, MAX_QUESTION));
  };
  const ask_ = (q: string) => {
    setDraft(q);
    start(q);
  };
  const reopen = (run: SavedRun) => {
    setDraft(run.question);
    restore(run);
  };
  const pick = (chip: Chip) => ask_(composeAsk(state.question, chip.label));

  const triage = card?.triage;
  const decisions = state.triage?.decisions;
  const machine = triage?.asset_id ?? decisions?.machine ?? null;
  const alarm = triage?.alarm_code ?? decisions?.alarm ?? null;
  const { picked, others } = card ? splitCauses(card) : { picked: null, others: [] };
  const causeId = picked?.failure_mode_id ?? triage?.failure_mode_id ?? decisions?.root_cause ?? null;
  const causeName = picked
    ? sentenceCase(picked.name)
    : causeId
      ? sentenceCase(failureName(causeId))
      : null;
  const facts = useAlarmFacts(alarm, machine);
  const v = viewOf(state, shown);
  const summary = summaryAt(state, shown);
  const applies = revealApplies(state);
  const revealing = applies && state.phase !== "error"; // a run that ended in an error is told whole
  const caughtUp = shown >= arrivedStages(state);
  const current: Record<string, string | null> = {
    root_cause: triage?.failure_mode_id ?? null,
    machine: triage?.asset_id ?? null,
  };
  const due = upkeep && machine ? itemFor(upkeep, machine) : null;
  const chips = triage?.chips ?? state.clarify?.chips ?? [];
  const reason = triage?.reason ?? state.clarify?.reason ?? null;
  const showChips = (status === "confirm" || status === "clarify") && chips.length > 0;

  const questionForm = (
    <form className="askform" onSubmit={submit} role="search">
      <input
        ref={field}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        maxLength={MAX_QUESTION}
        placeholder="What stopped, and what do you see?"
        aria-label="Question"
        autoComplete="off"
        enterKeyHint="send"
      />
      <button type="submit" disabled={!busy && !draft.trim()}>
        {busy ? "Stop" : "Ask"}
      </button>
    </form>
  );

  const suggested = suggestions.length > 0 && (
    <ul className="suggest list" aria-label="Suggested questions">
      {suggestions.map((s) => {
        const off = (replay && !s.replayable) || busy;
        return (
          <li key={s.question}>
            <button
              type="button"
              className="sug"
              disabled={off}
              title={replay && !s.replayable ? "Needs live mode" : undefined}
              onClick={() => ask_(s.question)}
            >
              {s.question}
              <ArrowRight size={20} aria-hidden="true" />
            </button>
          </li>
        );
      })}
    </ul>
  );

  const rail = (
    <Rail
      runs={history.runs}
      current={idle ? "" : state.question}
      asked={asked}
      idle={idle}
      onReopen={reopen}
      onClear={history.clear}
      onOpen={onOpen}
    />
  );

  // before the first question the field and the suggestions have the whole area
  if (idle) {
    return (
      <div className="ask">
        {rail}
        <div className="ask-main">
          <section className="idle-ask" aria-label="Ask a question">
            <h1>What is wrong on Line 3?</h1>
            {questionForm}
            {suggested}
          </section>
        </div>
      </div>
    );
  }

  const work = (
    <section className="work" aria-busy={busy} ref={pane}>
      <h1 className="sr">Ask</h1>
      {canStartOver(state) && (
        <div className="new-q-bar">
          <button type="button" className="new-q" onClick={newQuestion}>
            <Plus size={20} aria-hidden="true" /> New question
          </button>
        </div>
      )}
      {questionForm}

        {/* a fixed slot: the status line comes and goes without moving what is below it. It is
            the polite live region: each stage of the reveal says one line here. */}
        <div className="live" aria-live="polite">
          {state.saved ? (
            <p className="saved-note">
              <span className="saved-when">
                <Archive size={16} aria-hidden="true" /> Saved answer from {savedAt(state.saved.at)}
              </span>
              {canShowRevealAgain(state) && (
                <button type="button" className="saved-again" onClick={() => start(state.question)}>
                  Show the reveal again
                </button>
              )}
            </p>
          ) : busy && (state.phase === "queued" || caughtUp) ? (
            <Working state={state} />
          ) : revealing && shown > 0 ? (
            <p className="narr" key={shown} style={{ "--fade": `${paced(PACE.narrationFade)}ms` } as CSSProperties}>
              {narration(state, shown, facts)}
            </p>
          ) : null}
          {revealing && shown > 0 && !state.saved && shown < STAGES.length && paceFactor() > 0 && (
            <button
              type="button"
              className="show-all"
              onClick={() => {
                skipAll();
                field.current?.focus(); // the button goes away with the reveal; the keyboard stays
              }}
            >
              Show all now
            </button>
          )}
        </div>

        {state.phase === "error" && (
          <p className="error-band" role="alert">
            <span className="err-sq" aria-hidden="true" />
            {errorText(state.error, replay)}
          </p>
        )}

        {status === "clarify" && reason && <h2 className="headline">{reason}</h2>}
        {status === "out_of_scope" && (
          <>
            <h2 className="headline">{reason}</h2>
            <p className="quiet">Ask about a machine, an alarm code or a spare part.</p>
          </>
        )}
        {status === "clarify" && showChips && (
          <>
            {replay && <p className="prompt">Picking one works in live mode.</p>}
            <ChipGroups chips={chips} current={current} disabled={replay} onPick={pick} />
          </>
        )}

        {open && v.machineAlarm && (
          <>
            <MainIssue
              machine={machine}
              alarm={alarm}
              message={facts.message}
              citations={picked?.citations ?? []}
              onOpen={onOpen}
            />
            {due && v.done && <UpkeepNote item={due} onSee={onUpkeep} />}
            {causeName && v.rootCause && (
              <RootCause
                cause={picked}
                all={card?.causes ?? []}
                name={causeName}
                id={causeId}
                alarm={alarm}
                machine={machine}
                onOpen={onOpen}
              >
                {status === "confirm" && showChips && (
                  <>
                    <p className="prompt">
                      Not sure about the {unsure(chips)}.{" "}
                      {replay ? "Picking one works in live mode." : "Tap the right one."}
                    </p>
                    <ChipGroups chips={chips} current={current} disabled={replay} onPick={pick} />
                  </>
                )}
              </RootCause>
            )}
          </>
        )}

        {open && summary && <Summary text={summary} />}

        {v.band === "provisional" && <ProvisionalLockout />}

        {card && (
          <>
            {v.band === "card" && <Lockout card={card} onOpen={onOpen} />}
            {v.fix && card.fix && (
              <FixSection
                fix={card.fix}
                parts={card.parts}
                also={fixesAlongside(card)}
                onOpen={onOpen}
              />
            )}
            {v.parts && card.stock.length > 0 && <StockList stock={card.stock} onOpen={onOpen} />}
            {v.parts && card.parts.length > 0 && <PartsList parts={card.parts} onOpen={onOpen} />}
            {v.done && (
              <Flags
                notes={card.notes.filter(
                  (n) => n && !n.startsWith("this step failed") && !shortPartNote(n, card.parts),
                )}
              />
            )}
            {v.done && others.length > 0 && <OtherCauses card={card} onOpen={onOpen} />}
            {v.done && (card.breakdowns.length > 0 || card.history) && (
              <PastBreakdowns items={card.breakdowns} history={card.history} onOpen={onOpen} />
            )}
            {replay && v.done && open && machine && (
              <WriteBack
                key={state.run}
                onSubmit={(input) =>
                  setReports((all) => [...all, newBreakdown({ ...input, machine }, all.length + 1)])
                }
              />
            )}
          </>
        )}
      {(v.done || state.phase === "error" || !applies) && <AgentTrace state={state} />}
    </section>
  );

  return (
    <div className="ask">
      {rail}
      <div className="ask-main">
        {side ? (
          <Split
            id="ask"
            initial={0.56}
            minFirst={520}
            minSecond={360}
            onResize={() => setRefit((n) => n + 1)}
            first={work}
            second={
              <aside className="side">
                <GraphPanel state={state} shown={shown} onOpen={onOpen} refit={refit} facts={facts} technician={reports} />
              </aside>
            }
          />
        ) : (
          work
        )}
      </div>
    </div>
  );
}
