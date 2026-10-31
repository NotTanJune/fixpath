import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { askReducer, initialAsk, type AskState } from "../src/lib/askReducer";
import { canShowRevealAgain, canStartOver, hasGraph } from "../src/lib/askLayout";
import { restoredState } from "../src/lib/history";

function run(n: number, stop?: (e: { event: string; data: any }) => boolean): AskState {
  const path = new URL(`../../data/recordings/api/ask_${n}.jsonl`, import.meta.url);
  let state = askReducer(initialAsk, { type: "start", question: "q" });
  for (const line of readFileSync(path, "utf8").split("\n").filter(Boolean)) {
    const e = JSON.parse(line);
    state = askReducer(state, { type: "event", name: e.event, data: e.data });
    if (stop?.(e)) break;
  }
  return state;
}

describe("when the graph panel is shown", () => {
  test("not before a question is asked", () => {
    expect(hasGraph(initialAsk)).toBe(false);
  });

  test("from the moment a question runs, so that the panes do not jump", () => {
    expect(hasGraph(askReducer(initialAsk, { type: "start", question: "q" }))).toBe(true);
    const queued = askReducer(askReducer(initialAsk, { type: "start", question: "q" }), {
      type: "event",
      name: "queued",
      data: { position: 1 },
    });
    expect(hasGraph(queued)).toBe(true);
  });

  test("for an answer that draws a path, whether it is act or confirm", () => {
    expect(hasGraph(run(2))).toBe(true);
    expect(hasGraph(run(1))).toBe(true);
  });

  test("not for a clarify, finished or read from triage while it still runs", () => {
    expect(hasGraph(run(5))).toBe(false);
    expect(hasGraph(run(6))).toBe(false);
    const read = run(5, (e) => e.event === "node_end" && e.data.node === "triage");
    expect(read.phase).toBe("running");
    expect(hasGraph(read)).toBe(false);
  });

  test("not for an error with nothing to draw, and yes for one after a path", () => {
    const early = askReducer(askReducer(initialAsk, { type: "start", question: "q" }), {
      type: "failed",
      message: "The server did not answer.",
    });
    expect(early.phase).toBe("error");
    expect(hasGraph(early)).toBe(false);
    const late = askReducer(run(2, (e) => e.event === "answer"), {
      type: "event",
      name: "error",
      data: { message: "x" },
    });
    expect(late.phase).toBe("error");
    expect(hasGraph(late)).toBe(true);
  });
});

describe("starting over and showing the reveal again", () => {
  const saved = (n: number) =>
    restoredState({
      question: "q",
      at: "2026-10-29T09:00:00Z",
      events: readFileSync(new URL(`../../data/recordings/api/ask_${n}.jsonl`, import.meta.url), "utf8")
        .split("\n")
        .filter(Boolean)
        .map((line) => {
          const e = JSON.parse(line);
          return { name: e.event, data: e.data };
        }),
    });

  test("New question is offered for every state except the idle screen", () => {
    expect(canStartOver(initialAsk)).toBe(false);
    expect(canStartOver(run(2))).toBe(true); // an answer
    expect(canStartOver(run(5))).toBe(true); // a clarify
    expect(canStartOver(saved(2))).toBe(true); // a saved answer
    expect(canStartOver(askReducer(initialAsk, { type: "start", question: "q" }))).toBe(true); // running
    const failed = askReducer(askReducer(initialAsk, { type: "start", question: "q" }), {
      type: "failed",
      message: "x",
    });
    expect(canStartOver(failed)).toBe(true); // an error
  });

  test("reset returns every one of those to the idle screen with the question cleared", () => {
    for (const state of [run(2), run(5), saved(2), askReducer(initialAsk, { type: "start", question: "q" })]) {
      const back = askReducer(state, { type: "reset" });
      expect(back.phase).toBe("idle");
      expect(back.question).toBe("");
      expect(back.card).toBeNull();
      expect(back.saved).toBeNull();
      expect(back.slots).toEqual([]);
      expect(hasGraph(back)).toBe(false);
      expect(canStartOver(back)).toBe(false);
    }
  });

  test("the run counter is not lost, so the next question is a new run", () => {
    const before = run(2);
    const back = askReducer(before, { type: "reset" });
    expect(back.run).toBe(before.run);
    expect(askReducer(back, { type: "start", question: "q" }).run).toBe(before.run + 1);
  });

  test("Show the reveal again is offered on a saved answer in both modes, and only there", () => {
    expect(canShowRevealAgain(saved(2))).toBe(true);
    expect(canShowRevealAgain(run(2))).toBe(false);
    expect(canShowRevealAgain(initialAsk)).toBe(false);
  });

  test("starting the saved question again is a live run: not saved, new run number", () => {
    const s = saved(2);
    const again = askReducer(s, { type: "start", question: s.question });
    expect(again.saved).toBeNull();
    expect(again.phase).toBe("running");
    expect(again.run).toBe(s.run + 1);
  });
});
