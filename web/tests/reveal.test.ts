import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { askReducer, initialAsk, needsProvisionalLockout, type AskState } from "../src/lib/askReducer";
import { buildAskGraph } from "../src/lib/askGraph";
import { estimate } from "../src/lib/textMetrics";
import { PACE, type PaceEnv } from "../src/lib/pace";
import {
  advance,
  arrivedStages,
  gate,
  narration,
  nextDue,
  revealedGraph,
  skip,
  STAGES,
  START,
  summaryAt,
  viewOf,
  type Reveal,
} from "../src/lib/reveal";
import { needsBand } from "../src/lib/lockoutSteps";

const events = (n: number): { event: string; data: any }[] =>
  readFileSync(new URL(`../../data/recordings/api/ask_${n}.jsonl`, import.meta.url), "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));

/** The state after the first `count` events of a recorded question. */
function prefix(n: number, count: number): AskState {
  let state = askReducer(initialAsk, { type: "start", question: "q" });
  for (const e of events(n).slice(0, count)) {
    state = askReducer(state, { type: "event", name: e.event, data: e.data });
  }
  return state;
}
const full = (n: number) => prefix(n, events(n).length);

const env = (over: PaceEnv = {}): PaceEnv => ({ search: "", hash: "", storage: null, reduced: false, ...over });
const HOLD = PACE.stage.question; // every stage holds the same time at the agreed pace
const LAST = STAGES.length;

/** Plays the states against a fake clock: `timeline` says which state is current from which time. It
 * returns when each stage was shown, in ms. A stage is shown at the first moment the gate lets it. */
function play(
  timeline: { at: number; state: AskState }[],
  e: PaceEnv,
  end = 60000,
  start: Reveal = START,
): { shownAt: number[]; reveal: Reveal } {
  const shownAt: number[] = [];
  let r = start;
  let t = 0;
  const current = (now: number) => [...timeline].reverse().find((x) => x.at <= now)!.state;
  for (let guard = 0; guard < 500 && t <= end; guard++) {
    const g = gate(current(t), e);
    const next = advance(r, g, t, e);
    while (shownAt.length < next.shown) shownAt.push(t);
    const changed = next !== r;
    r = next;
    if (changed) continue; // look again at the same moment: a stage may follow at once
    const due = nextDue(r, g, t, e);
    const later = timeline.map((x) => x.at).filter((a) => a > t);
    const wake = [due, later[0] ?? null].filter((x): x is number => x !== null && x > t);
    if (!wake.length) break;
    t = Math.min(...wake);
  }
  return { shownAt, reveal: r };
}

describe("the stages", () => {
  test("are the seven of the reveal, in order, and the first six are the pace table", () => {
    expect([...STAGES]).toEqual(["question", "machineAlarm", "rootCause", "fix", "partsBin", "claims", "done"]);
    expect(STAGES.slice(0, 6)).toEqual(Object.keys(PACE.stage) as never);
  });
});

describe("what has arrived", () => {
  test("nothing before the question runs", () => {
    expect(arrivedStages(initialAsk)).toBe(0);
  });

  test("the question is read at once, then the machine and alarm with the triage", () => {
    const start = askReducer(initialAsk, { type: "start", question: "q" });
    expect(arrivedStages(start)).toBe(1);
    const evs = events(1);
    const triage = evs.findIndex((e) => e.event === "node_end" && e.data.node === "triage");
    expect(arrivedStages(prefix(1, triage + 1))).toBe(2);
  });

  test("the cause, the fix and the part come in with the graph agent's calls", () => {
    const evs = events(1);
    const at = (name: string) => evs.findIndex((e) => e.event === "tool_call" && e.data.name === name) + 1;
    expect(arrivedStages(prefix(1, at("alarm_causes")))).toBe(3);
    expect(arrivedStages(prefix(1, at("fixes_for")))).toBe(4);
    expect(arrivedStages(prefix(1, at("parts_for")))).toBe(5);
  });

  test("the claims and the end come in with the answer card", () => {
    expect(arrivedStages(full(1))).toBe(LAST);
  });
});

describe("a recorded answer at the default pace (all the data at once)", () => {
  test("the stages step at the pace, one every hold", () => {
    const { shownAt } = play([{ at: 0, state: full(1) }], env());
    expect(shownAt).toEqual([0, HOLD, 2 * HOLD, 3 * HOLD, 4 * HOLD, 5 * HOLD, 6 * HOLD]);
  });

  test("a slower pace multiplies every wait", () => {
    const { shownAt } = play([{ at: 0, state: full(1) }], env({ search: "?pace=2" }));
    expect(shownAt).toEqual([0, 2 * HOLD, 4 * HOLD, 6 * HOLD, 8 * HOLD, 10 * HOLD, 12 * HOLD]);
  });

  test("a faster pace divides them", () => {
    const { shownAt } = play([{ at: 0, state: full(1) }], env({ search: "?pace=0.5" }));
    expect(shownAt).toEqual([0, 0.5 * HOLD, HOLD, 1.5 * HOLD, 2 * HOLD, 2.5 * HOLD, 3 * HOLD]);
  });

  test("no stage ever shows before the one that comes first has had its time", () => {
    const { shownAt } = play([{ at: 0, state: full(1) }], env());
    for (let i = 1; i < shownAt.length; i++) expect(shownAt[i]! - shownAt[i - 1]!).toBeGreaterThanOrEqual(HOLD);
  });
});

describe("data that arrives slower than the pace", () => {
  test("a stage whose data is late shows when the data comes, with no wait added", () => {
    const evs = events(1);
    const triageAt = evs.findIndex((e) => e.event === "node_end" && e.data.node === "triage") + 1;
    const timeline = [
      { at: 0, state: prefix(1, 0) },
      { at: 300, state: prefix(1, triageAt) },
      { at: 9000, state: full(1) }, // the answer card comes at 9 s
    ];
    const { shownAt } = play(timeline, env());
    expect(shownAt[0]).toBe(0);
    expect(shownAt[1]).toBe(HOLD); // the triage was there; the pace held it
    // the cause, fix and part came after the triage in the recorded data, but with the card at 9 s
    // the remaining stages are all ready: the next one shows at 9 s, then the pace goes on
    expect(shownAt[2]).toBe(9000);
    expect(shownAt[3]).toBe(9000 + HOLD);
  });

  test("a stage that is due before its data waits for the data and then shows at once", () => {
    const start = askReducer(initialAsk, { type: "start", question: "q" });
    const timeline = [
      { at: 0, state: start },
      { at: 20000, state: full(2) },
    ];
    const { shownAt } = play(timeline, env());
    expect(shownAt).toEqual([0, 20000, 20000 + HOLD, 20000 + 2 * HOLD, 20000 + 3 * HOLD, 20000 + 4 * HOLD, 20000 + 5 * HOLD]);
  });
});

describe("pace factor 0", () => {
  test("shows everything that has arrived at once", () => {
    const { shownAt } = play([{ at: 0, state: full(1) }], env({ search: "?pace=0" }));
    expect(shownAt).toEqual([0, 0, 0, 0, 0, 0, 0]);
  });

  test("reduced motion is the same", () => {
    const { shownAt } = play([{ at: 0, state: full(1) }], env({ reduced: true }));
    expect(shownAt).toEqual([0, 0, 0, 0, 0, 0, 0]);
  });

  test("data that comes later shows when it comes", () => {
    const timeline = [
      { at: 0, state: prefix(1, 1) },
      { at: 5000, state: full(1) },
    ];
    const { shownAt } = play(timeline, env({ search: "?pace=0" }));
    expect(shownAt[0]).toBe(0);
    expect(shownAt.slice(1)).toEqual([5000, 5000, 5000, 5000, 5000, 5000].slice(0, shownAt.length - 1));
    expect(shownAt.length).toBe(LAST);
  });

  test("a saved answer shows at once, whatever the pace", () => {
    const saved: AskState = { ...full(1), saved: { at: "2026-10-29T10:00:00Z" } };
    const { shownAt } = play([{ at: 0, state: saved }], env({ search: "?pace=5" }));
    expect(shownAt).toEqual([0, 0, 0, 0, 0, 0, 0]);
  });
});

describe("show all now", () => {
  test("jumps to the end from any stage", () => {
    const state = full(1);
    let r = advance(START, gate(state, env()), 0, env());
    expect(r.shown).toBe(1);
    r = skip(r);
    r = advance(r, gate(state, env()), 100, env());
    expect(r.shown).toBe(LAST);
  });

  test("on a run that is still coming in shows what has arrived, and the rest as it arrives", () => {
    const early = prefix(1, 3);
    let r = advance(START, gate(early, env()), 0, env());
    r = skip(r);
    r = advance(r, gate(early, env()), 10, env());
    expect(r.shown).toBe(arrivedStages(early));
    const final = full(1);
    r = advance(r, gate(final, env()), 7000, env());
    expect(r.shown).toBe(LAST);
  });
});

describe("a run that ends in an error part way", () => {
  test("shows what had arrived at once and stops waiting", () => {
    const evs = events(1);
    const at = evs.findIndex((e) => e.event === "tool_call" && e.data.name === "fixes_for") + 1;
    const running = prefix(1, at);
    const cut = askReducer(running, { type: "closed", message: "The connection closed before the answer finished." });
    expect(cut.phase).toBe("error");
    const timeline = [
      { at: 0, state: running },
      { at: 1000, state: cut },
    ];
    const { shownAt, reveal } = play(timeline, env());
    // the pace would have shown the second stage at 1.4 s; at 1 s the cut came, and whatever
    // had arrived but was not yet shown shows now (the cut drops a fix that was only an ID)
    expect(shownAt).toEqual([0, 1000, 1000]);
    expect(reveal.shown).toBe(arrivedStages(cut));
    expect(reveal.shown).toBe(3);
    expect(gate(cut, env()).instant).toBe(true);
  });
});

describe("the lockout band and the fix", () => {
  const needing = [1, 2, 3, 4];

  test("full answers: no fix step, fix ID or fix node shows without the band, at any stage", () => {
    for (const n of needing) {
      const state = full(n);
      expect(needsBand(state.card!)).toBe(true);
      for (let shown = 0; shown <= LAST; shown++) {
        const v = viewOf(state, shown);
        const graph = revealedGraph(buildAskGraph(state), shown);
        const fixOnScreen = v.fixShown || graph.nodes.some((x) => x.type === "Procedure");
        if (fixOnScreen) expect(v.band).toBe("card");
      }
    }
  });

  test("the band is on the screen from the stage of the fix, and not before the card has it", () => {
    const v = (shown: number) => viewOf(full(1), shown).band;
    expect(v(3)).toBe("none");
    expect(v(4)).toBe("card");
    expect(v(LAST)).toBe("card");
  });

  test("every prefix of every recorded run, at every stage: a fix on screen has a band", () => {
    for (const n of needing) {
      for (let count = 0; count <= events(n).length; count++) {
        const state = prefix(n, count);
        for (let shown = 0; shown <= LAST; shown++) {
          const v = viewOf(state, shown);
          const graph = revealedGraph(buildAskGraph(state), shown);
          const fixOnScreen = v.fixShown || graph.nodes.some((x) => x.type === "Procedure");
          if (fixOnScreen) expect(v.band).not.toBe("none");
        }
      }
    }
  });

  test("before the card, the provisional band follows the rule of the cleanup round", () => {
    const evs = events(1);
    const at = evs.findIndex((e) => e.event === "tool_call" && e.data.name === "fixes_for") + 1;
    const state = prefix(1, at);
    expect(state.card).toBeNull();
    expect(needsProvisionalLockout(state)).toBe(true);
    expect(viewOf(state, LAST).band).toBe("provisional");
    // not before the machine is on the screen
    expect(viewOf(state, 1).band).toBe("none");
    expect(viewOf(state, 2).band).toBe("provisional");
  });

  test("a hazardous machine, with no fix yet, has the provisional band once it is on screen", () => {
    const evs = events(1);
    const triage = evs.findIndex((e) => e.event === "node_end" && e.data.node === "triage") + 1;
    const state = prefix(1, triage);
    expect(state.triage!.decisions.safety_critical).toBeGreaterThanOrEqual(0.5);
    expect(viewOf(state, 2).band).toBe("provisional");
  });

  test("after a cut the band rules still hold", () => {
    const evs = events(1);
    const at = evs.findIndex((e) => e.event === "tool_call" && e.data.name === "fixes_for") + 1;
    const cut = askReducer(prefix(1, at), { type: "closed", message: "cut" });
    const v = viewOf(cut, LAST);
    const graph = revealedGraph(buildAskGraph(cut), LAST);
    expect(graph.nodes.some((x) => x.type === "Procedure")).toBe(false);
    expect(v.band).toBe(needsProvisionalLockout(cut) ? "provisional" : "none");
    expect(v.band).toBe("provisional"); // the machine is hazardous
  });

  test("an answer that needs no lockout has no band", () => {
    const state = full(1);
    const card = { ...state.card!, safety: { ...state.card!.safety, required: false }, fix: { ...state.card!.fix!, requires_loto: false }, other_fixes: [] };
    expect(viewOf({ ...state, card }, LAST).band).toBe("none");
  });
});

describe("what each stage shows in the answer", () => {
  test("the sections come in the order of the stages", () => {
    const state = full(1);
    const flags = (n: number) => {
      const v = viewOf(state, n);
      return [v.machineAlarm, v.rootCause, v.fix, v.parts, v.claims, v.done];
    };
    expect(flags(0)).toEqual([false, false, false, false, false, false]);
    expect(flags(1)).toEqual([false, false, false, false, false, false]);
    expect(flags(2)).toEqual([true, false, false, false, false, false]);
    expect(flags(3)).toEqual([true, true, false, false, false, false]);
    expect(flags(4)).toEqual([true, true, true, false, false, false]);
    expect(flags(5)).toEqual([true, true, true, true, false, false]);
    expect(flags(6)).toEqual([true, true, true, true, true, false]);
    expect(flags(7)).toEqual([true, true, true, true, true, true]);
  });
});

describe("the graph, a stage at a time", () => {
  const graph = (n = 1, view: "path" | "all" = "path") => buildAskGraph(full(n), { view, measure: estimate });
  const ids = (shown: number, n = 1, view: "path" | "all" = "path") =>
    revealedGraph(graph(n, view), shown).nodes.map((x) => x.id);

  test("nothing is drawn before the machine and the alarm", () => {
    expect(ids(0)).toEqual([]);
    expect(ids(1)).toEqual([]);
  });

  test("each stage of the path adds its own nodes", () => {
    expect(ids(2).sort()).toEqual(["E-217", "HP-01"]);
    expect(ids(3)).toContain("FM-MAIN-CYLINDER-SEAL-WEAR");
    expect(ids(3)).not.toContain("PROC-HP-04");
    expect(ids(4)).toContain("PROC-HP-04");
    expect(ids(4)).not.toContain("SEAL-KIT-50");
    expect(ids(5)).toContain("SEAL-KIT-50");
    expect(ids(5)).toContain("B-12");
    expect(ids(5).some((id) => id.includes("#"))).toBe(false); // no cited page yet
    expect(ids(6).some((id) => id.includes("#"))).toBe(true);
    expect(ids(7)).toEqual(ids(6)); // the path view has nothing more to say at the end
  });

  test("the other facts of Everything come in with the end, not before", () => {
    const extra = ["FM-RELIEF-VALVE-MISADJUSTMENT", "PROC-HP-06", "OIL-HLP46-20L", "INC-2026-060"];
    for (let shown = 0; shown < LAST; shown++) {
      for (const id of extra) expect(ids(shown, 1, "all")).not.toContain(id);
    }
    for (const id of extra) expect(ids(LAST, 1, "all")).toContain(id);
    expect(ids(6, 1, "all").sort()).toEqual(ids(6, 1, "path").sort());
  });

  test("the whole graph is there at the end", () => {
    for (const view of ["path", "all"] as const) {
      const all = graph(1, view);
      const end = revealedGraph(all, LAST);
      expect(end.nodes.length).toBe(all.nodes.length);
      expect(end.edges.length).toBe(all.edges.length);
    }
  });

  test("an edge is drawn only when both of its ends are", () => {
    for (const view of ["path", "all"] as const) {
      for (let shown = 0; shown <= LAST; shown++) {
        const g = revealedGraph(graph(1, view), shown);
        const there = new Set(g.nodes.map((x) => x.id));
        for (const e of g.edges) {
          expect(there.has(e.source)).toBe(true);
          expect(there.has(e.target)).toBe(true);
        }
      }
    }
  });

  test("a node keeps its place as more of the graph is drawn", () => {
    const a = revealedGraph(graph(1, "all"), 4).nodes;
    const b = revealedGraph(graph(1, "all"), LAST).nodes;
    for (const n of a) {
      const m = b.find((x) => x.id === n.id)!;
      expect([m.x, m.y]).toEqual([n.x, n.y]);
    }
  });
});

describe("the narration", () => {
  test("is a plain line from the data, one per stage", () => {
    const state = full(1);
    expect(narration(state, 1, null)).toContain("Question read");
    expect(narration(state, 2, { message: "Main cylinder pressure loss", machineName: "Hydraulic press" })).toBe(
      "Machine and alarm: HP-01, Hydraulic press, alarm E-217 Main cylinder pressure loss",
    );
    expect(narration(state, 3, null)).toBe("Most likely cause: main cylinder seal wear, 6 of 6 past stops");
    expect(narration(state, 4, null)).toBe("Fix: Replace main cylinder seal kit, 6 steps, 180 min. Lockout before you start.");
    expect(narration(state, 5, null)).toContain("SEAL-KIT-50");
    expect(narration(state, 5, null)).toContain("bin B-12");
    expect(narration(state, 6, null)).toBe("Checking 4 claims against their sources: 4 kept, 0 unverified");
    expect(narration(state, 7, null)).toBe("Answer ready");
  });

  test("a short part is said so", () => {
    expect(narration(full(3), 5, null)).toContain("short");
  });

  test("before the card it uses what the graph agent found", () => {
    const evs = events(1);
    const at = evs.findIndex((e) => e.event === "tool_call" && e.data.name === "alarm_causes") + 1;
    const state = prefix(1, at);
    expect(narration(state, 3, null)).toBe("Cause found: main cylinder seal wear, checking the history");
  });

  test("has no text for a stage that is not shown", () => {
    expect(narration(full(1), 0, null)).toBe("");
  });
});

describe("the summary", () => {
  test("is on screen from the claims stage on, and takes no room before that", () => {
    const state = full(1);
    expect(state.card?.summary).toBeTruthy();
    for (const n of [0, 1, 2, 3, 4, 5]) expect(summaryAt(state, n)).toBeNull();
    expect(summaryAt(state, 6)).toBe(state.card!.summary);
    expect(summaryAt(state, 7)).toBe(state.card!.summary);
  });

  test("is not there when the card has none", () => {
    const state = full(1);
    expect(summaryAt({ ...state, card: { ...state.card!, summary: "" } }, 7)).toBeNull();
    expect(summaryAt({ ...state, card: null }, 7)).toBeNull();
  });
});
