import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import {
  askReducer,
  initialAsk,
  needsProvisionalLockout,
  type AskState,
} from "../src/lib/askReducer";

function transcript(n: number): { event: string; data: any }[] {
  const path = new URL(`../../data/recordings/api/ask_${n}.jsonl`, import.meta.url);
  return readFileSync(path, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function run(events: { event: string; data: any }[], from: AskState = initialAsk): AskState {
  let state = askReducer(from, { type: "start", question: "q" });
  for (const e of events) state = askReducer(state, { type: "event", name: e.event, data: e.data });
  return state;
}

const kinds = (s: AskState) => s.slots.map((slot) => slot.kind);

describe("path row reducer", () => {
  test("starts empty and running", () => {
    const state = askReducer(initialAsk, { type: "start", question: "HP-01 down" });
    expect(state.phase).toBe("running");
    expect(state.slots).toEqual([]);
    expect(state.question).toBe("HP-01 down");
  });

  test("the alarm magnet comes when triage ends", () => {
    const events = transcript(1);
    const upTo = events.findIndex((e) => e.event === "node_end" && e.data.node === "triage");
    const state = run(events.slice(0, upTo + 1));
    expect(kinds(state)).toEqual(["alarm"]);
    expect(state.slots[0]).toMatchObject({ id: "E-217", provisional: true });
    expect(state.triage?.status).toBe("confirm");
  });

  test("root cause after alarm_causes, fix after fixes_for, part after parts_for", () => {
    const events = transcript(1);
    const after = (tool: string) => {
      const i = events.findIndex((e) => e.event === "tool_call" && e.data.name === tool);
      return run(events.slice(0, i + 1));
    };
    expect(kinds(after("alarm_causes"))).toEqual(["alarm", "cause"]);
    expect(after("alarm_causes").slots[1]?.name).toBe("Main cylinder seal wear");
    expect(kinds(after("fixes_for"))).toEqual(["alarm", "cause", "fix"]);
    expect(kinds(after("parts_for"))).toEqual(["alarm", "cause", "fix", "part"]);
    expect(after("parts_for").slots[2]?.id).toBe("PROC-HP-04");
    expect(after("parts_for").slots.every((s) => s.provisional)).toBe(true);
  });

  test("a tool call that found nothing adds no magnet", () => {
    const state = run([
      {
        event: "tool_call",
        data: { name: "alarm_causes", args: {}, filled_by: "agent", result_count: 0 },
      },
    ]);
    expect(state.slots).toEqual([]);
    expect(state.toolCalls).toHaveLength(1);
  });

  test("the answer card is authoritative and adds the bin", () => {
    const state = run(transcript(1));
    expect(kinds(state)).toEqual(["alarm", "cause", "fix", "part", "bin"]);
    expect(state.slots.map((s) => s.id)).toEqual([
      "E-217",
      "FM-MAIN-CYLINDER-SEAL-WEAR",
      "PROC-HP-04",
      "SEAL-KIT-50",
      "B-12",
    ]);
    expect(state.slots.every((s) => !s.provisional)).toBe(true);
    expect(state.slots[4]?.detail).toBe("3 on hand");
    expect(state.slots[1]?.detail).toBe("6 of 6 cases");
    expect(state.phase).toBe("done");
    expect(state.card?.fix?.procedure_id).toBe("PROC-HP-04");
    expect(state.path.length).toBeGreaterThan(5);
  });

  test("every magnet carries the citations that open its source", () => {
    const state = run(transcript(1));
    expect(state.slots.every((s) => s.citations.length > 0)).toBe(true);
    const bin = state.slots[4];
    expect(bin?.citations[0]?.doc_path).toContain("inventory/");
  });

  test("a clarify run has no path row and keeps the reason and chips", () => {
    const state = run(transcript(5));
    expect(state.slots).toEqual([]);
    expect(state.clarify?.reason).toContain("Line 4");
    expect(state.clarify?.chips[0]?.value).toBe("RW-01");
    expect(state.card?.triage.status).toBe("clarify");
  });

  test("an answer that drops a provisional magnet drops it from the row", () => {
    const events = transcript(5);
    const early = [
      {
        event: "node_end",
        data: {
          node: "triage",
          status: "act",
          decisions: {
            alarm: "E-1",
            machine: "M-01",
            root_cause: null,
            intent: "troubleshoot",
            safety_critical: 0,
          },
          confidence: {},
        },
      },
    ];
    const first = run(early);
    expect(kinds(first)).toEqual(["alarm"]);
    const state = run(events, first);
    expect(state.slots).toEqual([]);
  });

  test("queued shows the position, then the first node clears it", () => {
    let state = run([{ event: "queued", data: { position: 2 } }]);
    expect(state.phase).toBe("queued");
    expect(state.queued).toBe(2);
    state = askReducer(state, { type: "event", name: "node_start", data: { node: "triage" } });
    expect(state.phase).toBe("running");
    expect(state.queued).toBeNull();
    expect(state.active).toBe("triage");
  });

  test("an error event is a plain sentence and ends the run", () => {
    const state = run([{ event: "error", data: { message: "Something went wrong." } }]);
    expect(state.phase).toBe("error");
    expect(state.error).toBe("Something went wrong.");
  });

  test("a failed connection ends the run with its message", () => {
    const state = askReducer(askReducer(initialAsk, { type: "start", question: "q" }), {
      type: "failed",
      message: "The server did not answer.",
    });
    expect(state.phase).toBe("error");
    expect(state.error).toBe("The server did not answer.");
  });

  test("done keeps the cost and the cache hits", () => {
    const state = run(transcript(2));
    expect(state.cost).toEqual({ cost_usd: 0, calls: 8, cache_hits: 8 });
  });

  test("the node ends build the trace in order", () => {
    const state = run(transcript(1));
    expect(state.nodes.map((n) => n.node)).toEqual([
      "triage",
      "graph_agent",
      "manual_agent",
      "synthesize",
      "verify",
      "assemble",
    ]);
  });

  test("keepalive-free events of an unknown name are ignored", () => {
    const state = run([{ event: "mystery", data: {} }]);
    expect(state.phase).toBe("running");
  });
});

describe("a stream that ends early", () => {
  test("a run that is still going when the stream closes is an error", () => {
    const state = askReducer(run([{ event: "node_start", data: { node: "triage" } }]), {
      type: "closed",
      message: "The connection closed before the answer finished.",
    });
    expect(state.phase).toBe("error");
    expect(state.error).toBe("The connection closed before the answer finished.");
  });

  test("a run that is done stays done when the stream closes", () => {
    const done = run(transcript(2));
    expect(askReducer(done, { type: "closed", message: "x" })).toBe(done);
  });
});

// Events built here, so that these tests do not depend on the text of the fixtures.
const TRIAGE_HOT = {
  event: "node_end",
  data: {
    node: "triage",
    status: "confirm",
    decisions: {
      alarm: "E-1",
      machine: "M-01",
      root_cause: "FM-X",
      intent: "troubleshoot",
      safety_critical: 0.84,
    },
    confidence: {},
  },
};
const TRIAGE_COLD = {
  event: "node_end",
  data: {
    node: "triage",
    status: "act",
    decisions: {
      alarm: "E-2",
      machine: "M-02",
      root_cause: null,
      intent: "troubleshoot",
      safety_critical: 0.1,
    },
    confidence: {},
  },
};
const call = (name: string, args: Record<string, unknown> = {}, result_count = 1) => ({
  event: "tool_call",
  data: { name, args, filled_by: "agent", result_count },
});
const fixSlots = (s: AskState) => s.slots.filter((x) => x.kind === "fix" || x.kind === "part");

describe("the provisional lockout band", () => {
  test("a fix slot with no card needs the band", () => {
    const state = run([TRIAGE_COLD, call("fixes_for")]);
    expect(kinds(state)).toContain("fix");
    expect(needsProvisionalLockout(state)).toBe(true);
  });

  test("a part slot with no card needs the band", () => {
    const state = run([TRIAGE_COLD, call("parts_for", { procedure_id: "PROC-X" })]);
    expect(needsProvisionalLockout(state)).toBe(true);
  });

  test("a hazard read at triage needs the band before any tool call", () => {
    const state = run([TRIAGE_HOT]);
    expect(fixSlots(state)).toEqual([]);
    expect(needsProvisionalLockout(state)).toBe(true);
  });

  test("the hazard threshold is 0.5", () => {
    const at = (p: number) =>
      run([
        {
          ...TRIAGE_COLD,
          data: { ...TRIAGE_COLD.data, decisions: { ...TRIAGE_COLD.data.decisions, safety_critical: p } },
        },
      ]);
    expect(needsProvisionalLockout(at(0.5))).toBe(true);
    expect(needsProvisionalLockout(at(0.49))).toBe(false);
  });

  test("a cold run before any fix needs no band", () => {
    expect(needsProvisionalLockout(run([TRIAGE_COLD, call("alarm_causes")]))).toBe(false);
    expect(needsProvisionalLockout(initialAsk)).toBe(false);
  });

  test("once the card exists the card's own band replaces it", () => {
    const state = run([
      TRIAGE_HOT,
      call("fixes_for"),
      { event: "answer", data: { ...transcript(1).find((e) => e.event === "answer")!.data } },
    ]);
    expect(state.card).not.toBeNull();
    expect(needsProvisionalLockout(state)).toBe(false);
  });
});

describe("a run that ends without a card keeps no fix on screen", () => {
  const mid = [TRIAGE_HOT, call("alarm_causes"), call("fixes_for"), call("parts_for", { procedure_id: "PROC-X" }, 2)];

  test("setup: the stream put a fix and a part on the row", () => {
    expect(kinds(run(mid))).toEqual(["alarm", "cause", "fix", "part"]);
  });

  test("failed clears the fix and part slots and keeps the earlier ones", () => {
    const state = askReducer(run(mid), { type: "failed", message: "x" });
    expect(kinds(state)).toEqual(["alarm", "cause"]);
  });

  test("closed clears them", () => {
    const state = askReducer(run(mid), { type: "closed", message: "x" });
    expect(state.phase).toBe("error");
    expect(kinds(state)).toEqual(["alarm", "cause"]);
  });

  test("an error event clears them", () => {
    const state = run([...mid, { event: "error", data: { message: "Budget." } }]);
    expect(state.phase).toBe("error");
    expect(kinds(state)).toEqual(["alarm", "cause"]);
  });

  test("the hazard band still shows after the cut, because triage read a hazard", () => {
    const state = askReducer(run(mid), { type: "closed", message: "x" });
    expect(needsProvisionalLockout(state)).toBe(true);
  });

  test("a cold run that is cut has no band and no fix", () => {
    const state = askReducer(run([TRIAGE_COLD, call("fixes_for")]), { type: "failed", message: "x" });
    expect(needsProvisionalLockout(state)).toBe(false);
    expect(fixSlots(state)).toEqual([]);
  });

  test("a finished card keeps its fix and part slots when an error follows", () => {
    const done = run(transcript(1));
    const state = askReducer(done, { type: "failed", message: "x" });
    expect(kinds(state)).toEqual(kinds(done));
  });
});

describe("a stream that closes after the answer", () => {
  const answered = () => {
    const events = transcript(1);
    return run(events.slice(0, events.findIndex((e) => e.event === "done")));
  };

  test("the answer is there and done is missing: not an error", () => {
    const state = askReducer(answered(), { type: "closed", message: "x" });
    expect(state.card).not.toBeNull();
    expect(state.phase).toBe("done");
    expect(state.error).toBeNull();
    expect(state.active).toBeNull();
  });

  test("the card and the row stay as they were", () => {
    const before = answered();
    const state = askReducer(before, { type: "closed", message: "x" });
    expect(state.card).toBe(before.card);
    expect(kinds(state)).toEqual(["alarm", "cause", "fix", "part", "bin"]);
  });

  test("a dropped connection after the answer is not an error either", () => {
    const state = askReducer(answered(), { type: "failed", message: "The connection dropped." });
    expect(state.phase).toBe("done");
    expect(state.error).toBeNull();
  });

  test("a close before the answer is still an error", () => {
    const state = askReducer(run([TRIAGE_COLD]), { type: "closed", message: "Closed early." });
    expect(state.phase).toBe("error");
    expect(state.error).toBe("Closed early.");
  });
});
