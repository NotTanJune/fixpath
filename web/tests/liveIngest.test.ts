import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  RECORDED_END,
  STEPS,
  freshIds,
  flashIds,
  idleRun,
  liveIngest,
  mapRun,
  marked,
  parseAt,
  runCaption,
  runCentre,
  runGraph,
  type LiveState,
  type RunEvent,
} from "../src/lib/liveIngest";

const FIXTURES = join(import.meta.dir, "..", "..", "data", "recordings", "ingest");
const recorded = (n: number): RunEvent[] =>
  readFileSync(join(FIXTURES, `${n}.jsonl`), "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => {
      const e = JSON.parse(line);
      return { event: e.event, data: e.data, at: parseAt(e.at) };
    });

const play = (events: RunEvent[], from: LiveState = idleRun): LiveState => events.reduce(liveIngest, from);
/** A run of one handover, as a recording has it (a made-up second a step). The specifics below are tested on
 * this, so that a recording that is made again does not change them; the recordings themselves are only
 * checked for what holds of every one. */
const DOC = "handovers/01_handover.txt";
const stamp = (n: number) => parseAt(`2026-01-01T00:00:${String(n).padStart(2, "0")}Z`);
const ev = (n: number, event: string, data: unknown): RunEvent => ({ event, data, at: stamp(n) });
const PASSAGES = ["DOC-aaaa-0-0"];
const EDGES = ["OCCURRED_ON|HP-01", "ROOT_CAUSE|FM-MAIN-CYLINDER-SEAL-WEAR", "TRIGGERED_BY|E-217", "USED|OIL-HLP46-20L", "USED|SEAL-KIT-50"];
const SAMPLE: RunEvent[] = [
  ev(0, "step", { node: "prepare" }),
  ev(1, "extracted", { doc_path: DOC, doc_type: "handover", extractor: "sol-text", nodes: 1, edges: 4, chunks: 1 }),
  ev(2, "chunked", { doc_path: DOC, chunks: [{ id: PASSAGES[0], page: null, ord: 0, text: "Shift handover 19 Oct Shift A\nFrom: NA   To: RP" }] }),
  ev(3, "facts", {
    nodes: [{ id: `${DOC}#1`, type: "Incident", label: "stopped twice with ERR 217, oil on the floor" }],
    edges: [1, 2, 3, 4].map((i) => ({ id: `${DOC}#1|REL|X${i}`, src: `${DOC}#1`, rel: "REL", dst: `X${i}` })),
  }),
  ev(4, "step", { node: "extract" }),
  ev(5, "matched", {
    merged: [
      { from: "HP-01", to: "HP-01" },
      { from: "E-217", to: "E-217" },
      { from: "SK-50", to: "SEAL-KIT-50" },
      { from: "HLP46", to: "OIL-HLP46-20L" },
    ],
    new: ["INC-2026-062"],
  }),
  ev(6, "step", { node: "resolve" }),
  ev(7, "step", { node: "link" }),
  ev(8, "step", { node: "validate" }),
  ev(9, "placed", {
    passages: [{ id: PASSAGES[0], x: -0.9858, y: -0.4299, z: -0.0678 }],
    facts: [
      { id: "INC-2026-062", kind: "node", type: "Incident", label: "stopped twice with ERR 217, oil on the floor", x: 0.28, y: -0.23, z: 0.3, passages: PASSAGES },
      ...EDGES.map((e, i) => ({
        id: `INC-2026-062|${e}`,
        kind: "link",
        type: e.split("|")[0],
        label: "link",
        x: 0.2 + i / 100,
        y: -0.2,
        z: 0.3,
        passages: PASSAGES,
      })),
    ],
  }),
  ev(10, "step", { node: "load" }),
  ev(11, "done", { report: { run: { loaded: { nodes: 165, edges: 447 } } } }),
];
const sample = () => play([{ event: "begin", data: { run: 7 }, at: 0 }, ...SAMPLE]);

const begun = (at = 0) => liveIngest(idleRun, { event: "begin", data: { run: 1 }, at });
const at = (event: string, data: unknown, ms: number): RunEvent => ({ event, data, at: ms });
const status = (s: LiveState) => s.steps.map((x) => x.status);

describe("the steps", () => {
  test("seven, in the order of the bar, all waiting before a run", () => {
    expect(STEPS.map((s) => s.label)).toEqual(["Read", "Chunk", "Embed", "Facts", "Match", "Link", "Load"]);
    expect(idleRun.status).toBe("idle");
    expect(status(idleRun)).toEqual(Array(7).fill("pending"));
    expect(idleRun.steps.every((s) => s.seconds === null)).toBe(true);
  });

  test("a run that begins is on Read", () => {
    const s = begun();
    expect(s.status).toBe("running");
    expect(status(s)).toEqual(["active", "pending", "pending", "pending", "pending", "pending", "pending"]);
  });

  test("extracted ends Read and puts the run on Chunk", () => {
    const s = play([at("extracted", { doc_path: "handovers/a.txt", doc_type: "handover", nodes: 1, edges: 4, chunks: 1 }, 1500)], begun(0));
    expect(s.steps[0]).toEqual({ key: "read", label: "Read", status: "done", seconds: 1.5 });
    expect(s.steps[1]!.status).toBe("active");
    expect(s.doc).toEqual({ path: "handovers/a.txt", type: "handover" });
  });

  test("the steps end in the order the events come: Chunk, Facts, Match, Link, Embed, Load", () => {
    let s = begun(0);
    const run: [string, unknown, number, number][] = [
      ["extracted", { doc_path: "a.txt", doc_type: "log" }, 1000, 0],
      ["chunked", { doc_path: "a.txt", chunks: [] }, 1000, 1],
      ["facts", { nodes: [], edges: [] }, 1250, 3],
      ["matched", { merged: [], new: [] }, 3000, 4],
      ["step", { node: "link" }, 5000, 5],
      ["placed", { passages: [], facts: [] }, 5500, 2],
      ["step", { node: "load" }, 6000, 6],
    ];
    const seen: number[] = [];
    for (const [event, data, ms, index] of run) {
      s = liveIngest(s, at(event, data, ms));
      expect(s.steps[index]!.status).toBe("done");
      seen.push(s.steps[index]!.seconds!);
    }
    // the seconds are the time since the event before
    expect(seen).toEqual([1, 0, 0.25, 1.75, 2, 0.5, 0.5]);
    expect(s.status).toBe("running");
  });

  test("the other nodes of the graph end no step but move the clock", () => {
    let s = begun(0);
    s = liveIngest(s, at("step", { node: "prepare" }, 700));
    expect(status(s)[0]).toBe("active");
    s = liveIngest(s, at("extracted", { doc_path: "a.txt", doc_type: "log" }, 1000));
    expect(s.steps[0]!.seconds).toBe(0.3);
  });

  test("done ends Load, and a run that went to its end has seven finished steps", () => {
    expect(play(SAMPLE, idleRun).status).toBe("idle");
    const full = sample();
    expect(full.status).toBe("done");
    expect(status(full)).toEqual(Array(7).fill("done"));
    expect(full.steps.map((x) => x.seconds)).toEqual(Array(7).fill(1));
  });

  test("done ends Load even when the node's own event did not come", () => {
    const events = SAMPLE.filter((e) => !(e.event === "step" && (e.data as any).node === "load"));
    const s = play([{ event: "begin", data: { run: 1 }, at: 0 }, ...events]);
    expect(status(s)).toEqual(Array(7).fill("done"));
    expect(s.status).toBe("done");
  });
});

describe("what a run holds", () => {
  test("the document, its chunks, facts, matches and placed points", () => {
    const s = sample();
    expect(s.doc).toEqual({ path: DOC, type: "handover" });
    expect(s.converted).toBeNull();
    expect(s.chunks).toHaveLength(1);
    expect(s.chunks[0]).toMatchObject({ id: PASSAGES[0], page: null, ord: 0 });
    expect(s.facts!.nodes).toHaveLength(1);
    expect(s.facts!.edges).toHaveLength(4);
    expect(s.matched!.new).toEqual(["INC-2026-062"]);
    expect(s.matched!.merged).toHaveLength(4);
    expect(s.placed!.passages).toHaveLength(1);
    expect(s.placed!.facts).toHaveLength(6);
    expect(s.loaded).toEqual({ nodes: 165, edges: 447 });
    expect(s.error).toBeNull();
  });

  test("a converted file says what it was converted from", () => {
    const s = play([at("converted", { from: ".json", to: ".txt", chars: 889 }, 0), ...SAMPLE], begun(0));
    expect(s.converted).toEqual({ from: ".json", to: ".txt", chars: 889 });
  });

  test("a PDF has pages on its chunks, and a run can place no new facts", () => {
    const chunks = [1, 2].map((page) => ({ id: `C${page}`, page, ord: page - 1, text: "x" }));
    const s = play(
      [
        at("extracted", { doc_path: "manuals/a.pdf", doc_type: "manual" }, 1000),
        at("chunked", { doc_path: "manuals/a.pdf", chunks }, 1000),
        at("matched", { merged: [], new: [] }, 2000),
        at("placed", { passages: [], facts: [] }, 3000),
      ],
      begun(0),
    );
    expect(s.chunks.map((c) => c.page)).toEqual([1, 2]);
    expect(s.placed!.facts).toEqual([]);
    expect(s.matched!.new).toEqual([]);
  });

  test("a reset puts the view back to idle, whatever state it was in", () => {
    expect(liveIngest(sample(), { event: "reset", data: null, at: 9 })).toBe(idleRun);
    expect(liveIngest(idleRun, { event: "reset", data: null, at: 9 })).toBe(idleRun);
  });

  test("a new run forgets the one before", () => {
    const s = liveIngest(sample(), { event: "begin", data: { run: 2 }, at: 50 });
    expect(s).toEqual({ ...idleRun, status: "running", run: 2, clock: 50, steps: s.steps });
    expect(status(s)[0]).toBe("active");
  });
});

describe("the recorded runs of the test set", () => {
  const files = Array.from({ length: 10 }, (_, i) => i + 1);
  const whole = (n: number) => play([{ event: "begin", data: { run: 1 }, at: parseAt(recordedFirst(n)) }, ...recorded(n)]);
  const recordedFirst = (n: number) => JSON.parse(readFileSync(join(FIXTURES, `${n}.jsonl`), "utf8").split("\n")[0]!).at as string;

  test("every one but the refused ones goes through all seven steps and ends", () => {
    for (const n of files) {
      const s = whole(n);
      if (s.status === "failed") {
        expect(s.error).toBeTruthy();
        expect(s.failedAt).toBe("read");
        continue;
      }
      expect(s.status).toBe("done");
      expect(status(s)).toEqual(Array(7).fill("done"));
      // a table or a log has no chunks, so there may be nothing to say of them: the events still all come
      expect(s.facts).not.toBeNull();
      expect(s.matched).not.toBeNull();
      expect(s.placed).not.toBeNull();
      expect(s.doc!.path).toBeTruthy();
      for (const step of s.steps) expect(step.seconds).toBeGreaterThanOrEqual(0);
    }
  });

  test("a converted file says so, and exactly the files the report says were converted", () => {
    const report = JSON.parse(readFileSync(join(FIXTURES, "report.json"), "utf8")).files as { number: number; converted_from: string | null }[];
    for (const row of report) {
      const s = whole(row.number);
      expect(s.converted?.from ?? null).toBe(row.converted_from);
    }
  });

  test("the refused file stops at Read with the server's reason", () => {
    const s = whole(10);
    expect(s.status).toBe("failed");
    expect(s.error).toBe("This file is not text, a PDF or a workbook, so it cannot be read.");
    expect(s.steps[0]!.status).toBe("failed");
  });
});

describe("a run that does not end well", () => {
  test("an error mid-run stops the bar at the step it was on, with the server's words", () => {
    let s = begun(0);
    s = play(
      [
        at("extracted", { doc_path: "a.txt", doc_type: "log" }, 1000),
        at("chunked", { doc_path: "a.txt", chunks: [{ id: "C", page: null, ord: 0, text: "x" }] }, 1000),
        at("error", { message: "Something went wrong while ingesting the file. Try again." }, 2000),
      ],
      s,
    );
    expect(s.status).toBe("failed");
    expect(s.error).toBe("Something went wrong while ingesting the file. Try again.");
    expect(s.failedAt).toBe("facts");
    expect(status(s)).toEqual(["done", "done", "pending", "failed", "pending", "pending", "pending"]);
    // what had arrived stays
    expect(s.chunks).toHaveLength(1);
  });

  test("an error with no words gets the plain ones", () => {
    const s = liveIngest(begun(), at("error", {}, 100));
    expect(s.error).toBe("The file was not added. Try again.");
    expect(s.failedAt).toBe("read");
  });

  test("a refusal stops the bar at Read with the server's reason", () => {
    const s = liveIngest(begun(), at("refused", { status: 415, detail: "This file is not text." }, 100));
    expect(s.status).toBe("failed");
    expect(s.error).toBe("This file is not text.");
    expect(s.failedAt).toBe("read");
    expect(s.steps[0]!.status).toBe("failed");
  });

  test("a skipped file ends the run with its reason", () => {
    const s = play([at("skipped", { doc_path: "a.txt", reason: "it is already in the graph" }, 500)], begun());
    expect(s.status).toBe("failed");
    expect(s.error).toBe("Skipped a.txt: it is already in the graph.");
  });

  test("events after the end change nothing", () => {
    const failed = liveIngest(begun(), at("error", { message: "no" }, 100));
    expect(liveIngest(failed, at("extracted", { doc_path: "a", doc_type: "log" }, 200))).toBe(failed);
    const done = sample();
    expect(liveIngest(done, at("error", { message: "late" }, 99000))).toBe(done);
  });

  test("an event of a kind it does not know changes nothing", () => {
    const s = begun();
    expect(liveIngest(s, at("heartbeat", {}, 10))).toBe(s);
    expect(liveIngest(idleRun, at("extracted", { doc_path: "a", doc_type: "log" }, 10))).toBe(idleRun);
  });

  test("the line in the queue is kept, and the clock starts when the turn comes", () => {
    let s = begun(0);
    s = liveIngest(s, at("queued", { position: 2 }, 4000));
    expect(s.queued).toBe(2);
    s = liveIngest(s, at("queued", { position: 0 }, 9000));
    expect(s.queued).toBe(0);
    s = liveIngest(s, at("extracted", { doc_path: "a", doc_type: "log" }, 10000));
    expect(s.steps[0]!.seconds).toBe(1);
  });
});

describe("what the views read", () => {
  const whole = (_n: number) => sample();
  const noFacts = () =>
    play(
      [
        at("extracted", { doc_path: "manuals/a.pdf", doc_type: "manual" }, 1000),
        at("chunked", { doc_path: "manuals/a.pdf", chunks: [{ id: "C1", page: 1, ord: 0, text: "a" }, { id: "C2", page: 2, ord: 1, text: "b" }] }, 1000),
        at("matched", { merged: [], new: [] }, 2000),
        at("placed", { passages: [{ id: "C1", x: 0, y: 0, z: 0 }, { id: "C2", x: 1, y: 1, z: 1 }], facts: [] }, 3000),
      ],
      begun(0),
    );

  test("the graph gains the new nodes and links that were placed", () => {
    const g = runGraph(whole(1));
    expect(g.nodes).toEqual([
      { id: "INC-2026-062", label: "stopped twice with ERR 217, oil on the floor", type: "Incident" },
    ]);
    expect(g.edges.map((e) => [e.source, e.rel, e.target])).toEqual([
      ["INC-2026-062", "OCCURRED_ON", "HP-01"],
      ["INC-2026-062", "ROOT_CAUSE", "FM-MAIN-CYLINDER-SEAL-WEAR"],
      ["INC-2026-062", "TRIGGERED_BY", "E-217"],
      ["INC-2026-062", "USED", "OIL-HLP46-20L"],
      ["INC-2026-062", "USED", "SEAL-KIT-50"],
    ]);
    expect(g.edges[0]!.id).toBe("INC-2026-062|OCCURRED_ON|HP-01");
    expect(runGraph(idleRun)).toEqual({ nodes: [], edges: [] });
  });

  test("the new nodes pulse and the matched ones flash", () => {
    const s = whole(1);
    expect(freshIds(s)).toEqual(["INC-2026-062"]);
    expect(flashIds(s)).toEqual(["HP-01", "E-217", "SEAL-KIT-50", "OIL-HLP46-20L"]);
    expect(freshIds(idleRun)).toEqual([]);
    expect(flashIds(idleRun)).toEqual([]);
  });

  test("a match to itself flashes once, and each existing node once", () => {
    const s = liveIngest(begun(), at("matched", { merged: [{ from: "A", to: "X" }, { from: "B", to: "X" }], new: [] }, 5));
    expect(flashIds(s)).toEqual(["X"]);
  });

  test("the tree centres on the most connected node that is new or matched, the lower id on a tie", () => {
    const s = whole(1);
    const degree = new Map([
      ["HP-01", 60],
      ["E-217", 55],
      ["SEAL-KIT-50", 10],
      ["OIL-HLP46-20L", 12],
      ["INC-2026-062", 5],
      ["OTHER", 999],
    ]);
    expect(runCentre(s, degree)).toBe("HP-01");
    degree.set("E-217", 60);
    expect(runCentre(s, degree)).toBe("E-217");
  });

  test("a new node that is not in the graph yet cannot be the centre", () => {
    const s = whole(1);
    expect(runCentre(s, new Map([["INC-2026-062", 99], ["HP-01", 1]]))).toBe("INC-2026-062");
    expect(runCentre(s, new Map([["HP-01", 1]]))).toBe("HP-01");
    expect(runCentre(s, new Map())).toBeNull();
    expect(runCentre(idleRun, new Map([["HP-01", 1]]))).toBeNull();
  });

  test("the map run holds the placed points, with the chunk text on a passage", () => {
    const run = mapRun(whole(1))!;
    expect(run.key).toBe(7);
    expect(run.passages.map((p) => [p.id, p.kind])).toEqual([[PASSAGES[0]!, "passage"]]);
    expect(run.passages[0]!.label).toStartWith("Shift handover 19 Oct");
    expect(run.facts).toHaveLength(6);
    expect(run.facts[0]).toMatchObject({ id: "INC-2026-062", kind: "node", passages: PASSAGES, docs: [] });
    expect(run.flash).toEqual(["HP-01", "E-217", "SEAL-KIT-50", "OIL-HLP46-20L"]);
  });

  test("the map run is null until a match or a placement has come", () => {
    expect(mapRun(idleRun)).toBeNull();
    expect(mapRun(begun())).toBeNull();
    const matched = liveIngest(begun(), at("matched", { merged: [{ from: "A", to: "X" }], new: [] }, 5));
    expect(mapRun(matched)).toEqual({ key: 1, passages: [], facts: [], flash: ["X"] });
  });

  test("the caption of the map says what flew in", () => {
    expect(runCaption(null)).toBe("Close together means worded alike.");
    const run = mapRun(whole(1))!;
    expect(runCaption(run)).toBe(
      "Close together means worded alike. Black squares: the 1 passage of this file. Blue: the 6 facts placed from it.",
    );
    const two = mapRun(noFacts())!;
    expect(runCaption(two)).toBe(
      "Close together means worded alike. Black squares: the 2 passages of this file. It placed no new facts.",
    );
    expect(runCaption({ key: 1, passages: [], facts: [], flash: ["X"] })).toBe("Close together means worded alike.");
  });
});

describe("times and marks", () => {
  test("a recorded time is read as ms", () => {
    expect(parseAt("2026-01-01T00:00:00Z")).toBe(parseAt("2026-01-01T00:00:00Z"));
    expect(parseAt("2026-01-01T00:00:03Z") - parseAt("2026-01-01T00:00:01Z")).toBe(2000);
    expect(parseAt("not a time")).toBe(0);
  });

  test("marks come one after another, and none wait when the step is 0", () => {
    expect(marked(4, 0, 300)).toBe(1);
    expect(marked(4, 299, 300)).toBe(1);
    expect(marked(4, 300, 300)).toBe(2);
    expect(marked(4, 900, 300)).toBe(4);
    expect(marked(4, 99999, 300)).toBe(4);
    expect(marked(4, 0, 0)).toBe(4);
    expect(marked(0, 0, 300)).toBe(0);
  });

  test("the end line of a recorded run is the sentence the spec gives", () => {
    expect(RECORDED_END).toBe("Recorded run. The graph here is unchanged.");
  });
});
