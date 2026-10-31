import { failureName, sentenceCase } from "./format";
import type {
  AnswerCard,
  Chip,
  Citation,
  NodeEnd,
  PartNeed,
  PathStep,
  ToolCall,
  Triage,
  TriageStatus,
} from "./types";

export type SlotKind = "alarm" | "cause" | "fix" | "part" | "bin";
export const SLOT_ORDER: SlotKind[] = ["alarm", "cause", "fix", "part", "bin"];

/** One magnet of the path row. provisional is true until the answer card lands. */
export interface Slot {
  kind: SlotKind;
  id: string;
  name: string;
  detail: string;
  provisional: boolean;
  short?: boolean;
  citations: Citation[];
}

export interface TriageRead {
  status: TriageStatus;
  decisions: NonNullable<NodeEnd["decisions"]>;
  confidence: Record<string, number | null>;
}

export interface AskState {
  /** Counts the runs, so that a new question that repeats the last one is still a new run. */
  run: number;
  phase: "idle" | "queued" | "running" | "done" | "error";
  question: string;
  queued: number | null;
  active: string | null;
  nodes: NodeEnd[];
  toolCalls: ToolCall[];
  triage: TriageRead | null;
  clarify: { chips: Chip[]; reason: string | null } | null;
  card: AnswerCard | null;
  path: PathStep[];
  slots: Slot[];
  cost: { cost_usd: number; calls: number; cache_hits: number } | null;
  error: string | null;
  /** Set when the screen shows an answer saved in this browser, and not a live run. */
  saved: { at: string } | null;
}

export type AskAction =
  | { type: "start"; question: string }
  | { type: "event"; name: string; data: any }
  | { type: "failed"; message: string }
  | { type: "closed"; message: string }
  | { type: "restore"; question: string; events: { name: string; data: unknown }[]; at: string }
  | { type: "reset" };

export const initialAsk: AskState = {
  run: 0,
  phase: "idle",
  question: "",
  queued: null,
  active: null,
  nodes: [],
  toolCalls: [],
  triage: null,
  clarify: null,
  card: null,
  path: [],
  slots: [],
  cost: null,
  error: null,
  saved: null,
};

function put(slots: Slot[], slot: Slot): Slot[] {
  const rest = slots.filter((s) => s.kind !== slot.kind);
  return [...rest, slot].sort((a, b) => SLOT_ORDER.indexOf(a.kind) - SLOT_ORDER.indexOf(b.kind));
}

function find(slots: Slot[], kind: SlotKind): Slot | undefined {
  return slots.find((s) => s.kind === kind);
}

/** The part that the technician fetches first: the one the fix steps name earliest. */
export function keyPart(card: Pick<AnswerCard, "fix" | "parts">): PartNeed | undefined {
  const steps = card.fix?.steps ?? [];
  const at = (part: PartNeed) => {
    const i = steps.findIndex((s) => s.includes(part.part_no));
    return i === -1 ? Number.MAX_SAFE_INTEGER : i;
  };
  return [...card.parts].sort((a, b) => at(a) - at(b))[0];
}

function pickedCause(card: AnswerCard) {
  const id = card.triage.failure_mode_id;
  return card.causes.find((c) => c.failure_mode_id === id) ?? card.causes[0];
}

export function slotsFromCard(card: AnswerCard): Slot[] {
  const slots: Slot[] = [];
  const cause = pickedCause(card);
  const alarm = card.triage.alarm_code;
  if (alarm) {
    slots.push({
      kind: "alarm",
      id: alarm,
      name: alarm,
      detail: card.triage.asset_id ? `on ${card.triage.asset_id}` : "",
      provisional: false,
      citations: cause?.citations ?? [],
    });
  }
  if (cause) {
    slots.push({
      kind: "cause",
      id: cause.failure_mode_id,
      name: sentenceCase(cause.name),
      detail:
        cause.of_total > 0 ? `${cause.support} of ${cause.of_total} cases` : "from the manual",
      provisional: false,
      citations: cause.citations,
    });
  }
  if (card.fix) {
    slots.push({
      kind: "fix",
      id: card.fix.procedure_id,
      name: card.fix.procedure_id,
      detail: card.fix.manual_ref ? `Manual ${card.fix.manual_ref}` : `${card.fix.est_minutes} min`,
      provisional: false,
      citations: card.fix.citations,
    });
  }
  const part = keyPart(card);
  if (part) {
    slots.push({
      kind: "part",
      id: part.part_no,
      name: part.part_no,
      detail: `x${part.qty}`,
      provisional: false,
      citations: part.citations,
    });
    if (part.bin) {
      const sheets = part.citations.filter((c) => c.doc_path.startsWith("inventory/"));
      const rest = part.citations.filter((c) => !c.doc_path.startsWith("inventory/"));
      slots.push({
        kind: "bin",
        id: part.bin,
        name: part.bin,
        detail:
          part.qty_on_hand === null
            ? "stock unknown"
            : part.enough
              ? `${part.qty_on_hand} on hand`
              : `short: ${part.qty_on_hand} on hand`,
        provisional: false,
        short: !part.enough,
        citations: [...sheets, ...rest],
      });
    }
  }
  return slots;
}

function fromToolCall(state: AskState, call: ToolCall): Slot[] {
  const { slots } = state;
  if (call.result_count <= 0) return slots;
  const decisions = state.triage?.decisions;
  if (call.name === "alarm_causes") {
    const fm = decisions?.root_cause;
    return put(slots, {
      kind: "cause",
      id: fm ?? "",
      name: fm ? failureName(fm) : "Cause found",
      detail: fm ? "checking history" : `${call.result_count} possible`,
      provisional: true,
      citations: [],
    });
  }
  if (call.name === "fixes_for") {
    return find(slots, "fix")
      ? slots
      : put(slots, {
          kind: "fix",
          id: "",
          name: "Fix found",
          detail: "",
          provisional: true,
          citations: [],
        });
  }
  if (call.name === "parts_for") {
    let next = slots;
    const procedure = call.args["procedure_id"];
    const fix = find(next, "fix");
    if (typeof procedure === "string" && (!fix || !fix.id)) {
      next = put(next, {
        kind: "fix",
        id: procedure,
        name: procedure,
        detail: "",
        provisional: true,
        citations: [],
      });
    }
    return find(next, "part")
      ? next
      : put(next, {
          kind: "part",
          id: "",
          name: call.result_count === 1 ? "1 part" : `${call.result_count} parts`,
          detail: "",
          provisional: true,
          citations: [],
        });
  }
  return slots;
}

/** Where a run that ends without a card stands: the fix and part are only ids and a count. */
function withoutProvisionalFix(state: AskState): AskState {
  if (state.card) return state;
  const slots = state.slots.filter((s) => s.kind !== "fix" && s.kind !== "part");
  return slots.length === state.slots.length ? state : { ...state, slots };
}

/** Hazard reads at or above this ask for the lockout band before the card exists. */
export const HAZARD_BAND = 0.5;

/**
 * True while no answer card exists and the screen would otherwise show a fix, a part or a
 * hazardous machine without the lockout band. The card carries the real band and replaces it.
 */
export function needsProvisionalLockout(state: AskState): boolean {
  if (state.card) return false;
  if (state.slots.some((s) => s.kind === "fix" || s.kind === "part")) return true;
  return (state.triage?.decisions.safety_critical ?? 0) >= HAZARD_BAND;
}

function triageOf(card: AnswerCard): Triage {
  return card.triage;
}

export function askReducer(state: AskState, action: AskAction): AskState {
  switch (action.type) {
    case "reset":
      return { ...initialAsk, run: state.run };
    case "start":
      return { ...initialAsk, run: state.run + 1, phase: "running", question: action.question };
    case "failed":
    case "closed": {
      // the answer card is the end of the answer: a stream that ends after it, before the
      // done event with the cost, is a finished run and not an error
      if (state.card && (state.phase === "running" || state.phase === "queued")) {
        return { ...state, phase: "done", active: null, queued: null };
      }
      // the stream ended: a run that is still going did not finish
      const running = state.phase === "running" || state.phase === "queued";
      if (action.type === "closed" && !running) return state;
      return withoutProvisionalFix({
        ...state,
        phase: "error",
        error: action.message,
        active: null,
        queued: null,
      });
    }
    case "event":
      return onEvent(state, action.name, action.data);
    case "restore": {
      // the saved events go through the same reducer, in their saved order, at once
      let next: AskState = {
        ...initialAsk,
        run: state.run + 1,
        phase: "running",
        question: action.question,
      };
      for (const e of action.events) next = onEvent(next, e.name, e.data);
      next = askReducer(next, {
        type: "closed",
        message: "The saved answer did not finish.",
      });
      return { ...next, saved: { at: action.at } };
    }
  }
}

function onEvent(state: AskState, name: string, data: any): AskState {
  switch (name) {
    case "queued":
      return { ...state, phase: "queued", queued: Number(data?.position ?? 0) };
    case "node_start":
      return { ...state, phase: "running", queued: null, active: data.node };
    case "node_end": {
      const node = data as NodeEnd;
      let next: AskState = { ...state, nodes: [...state.nodes, node], active: null };
      if (node.node === "triage" && node.decisions && node.status) {
        const read: TriageRead = {
          status: node.status,
          decisions: node.decisions,
          confidence: node.confidence ?? {},
        };
        next = { ...next, triage: read };
        const alarm = node.decisions.alarm;
        if (alarm && (node.status === "act" || node.status === "confirm")) {
          next = {
            ...next,
            slots: put(next.slots, {
              kind: "alarm",
              id: alarm,
              name: alarm,
              detail: node.decisions.machine ? `on ${node.decisions.machine}` : "",
              provisional: true,
              citations: [],
            }),
          };
        }
      }
      return next;
    }
    case "tool_call": {
      const call = data as ToolCall;
      const next = { ...state, toolCalls: [...state.toolCalls, call] };
      return { ...next, slots: fromToolCall(next, call) };
    }
    case "clarify":
      return { ...state, clarify: { chips: data.chips ?? [], reason: data.reason ?? null } };
    case "answer": {
      const card = data as AnswerCard;
      const open = triageOf(card).status === "act" || triageOf(card).status === "confirm";
      return {
        ...state,
        card,
        path: card.path ?? state.path,
        slots: open ? slotsFromCard(card) : [],
        clarify:
          card.triage.status === "clarify"
            ? { chips: card.triage.chips, reason: card.triage.reason }
            : state.clarify,
      };
    }
    case "graph_path":
      return { ...state, path: data.path ?? [] };
    case "done":
      return {
        ...state,
        phase: "done",
        active: null,
        queued: null,
        cost: {
          cost_usd: Number(data.cost_usd ?? 0),
          calls: Number(data.calls ?? 0),
          cache_hits: Number(data.cache_hits ?? 0),
        },
      };
    case "error":
      return withoutProvisionalFix({
        ...state,
        phase: "error",
        active: null,
        queued: null,
        error: String(data?.message ?? "Something went wrong. Try again."),
      });
    default:
      return state;
  }
}
