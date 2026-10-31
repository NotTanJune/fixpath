import { describe, expect, test } from "bun:test";
import { decisionWord, matchName, reportView, REPORT_ARG, reviewRow, runFigures, type PipelineReport } from "../src/lib/runReport";
import { hrefFor, parseHash } from "../src/lib/router";

const report: PipelineReport = {
  files: [],
  run: { loaded: { documents: 107, nodes: 164, edges: 442, evidence: 906 }, mode: "full", ran_at: "2026-10-28T11:27:42+00:00", seconds: 26.53 },
  totals: {
    merges: { rule: 66, merge: 58 },
    review: {},
    flags: { warning: 20, error: 0 },
    cost: { usd: 0.002596, calls: 302, cache_hits: 219, paid_calls: 83 },
  },
  review_queue: [],
};

describe("the Run report is a page of Ingestion", () => {
  test("#/ingestion/report opens the report, and a file id opens a file", () => {
    expect(reportView(parseHash("#/ingestion/report"))).toBe(true);
    expect(reportView(parseHash("#/pipeline/report"))).toBe(true);
    expect(reportView(parseHash("#/ingestion/DOC-5926a330"))).toBe(false);
    expect(reportView(parseHash("#/ingestion"))).toBe(false);
    expect(reportView(parseHash("#/ask/report"))).toBe(false);
  });

  test("its link is built from the same word", () => {
    expect(hrefFor("ingestion", REPORT_ARG)).toBe("#/ingestion/report");
  });
});

describe("the totals row of the Run report", () => {
  test("lists files, nodes, links, errors, warnings, time and cost, in that order", () => {
    expect(runFigures(report)).toEqual([
      { key: "files", value: "107", label: "files" },
      { key: "nodes", value: "164", label: "nodes" },
      { key: "links", value: "442", label: "links" },
      { key: "errors", value: "0", label: "errors" },
      { key: "warnings", value: "20", label: "warnings" },
      { key: "time", value: "26.5 s", label: "time" },
      { key: "cost", value: "$0.0026", label: "cost" },
    ]);
  });

  test("a label is singular for one", () => {
    const one: PipelineReport = {
      ...report,
      run: { ...report.run, loaded: { documents: 1, nodes: 1, edges: 1 } },
      totals: { ...report.totals, flags: { warning: 1, error: 1 } },
    };
    expect(runFigures(one).map((f) => f.label)).toEqual(["file", "node", "link", "error", "warning", "time", "cost"]);
  });

  test("a run that cost nothing says $0.00", () => {
    const free = { ...report, totals: { ...report.totals, cost: { ...report.totals.cost, usd: 0 } } };
    expect(runFigures(free).at(-1)?.value).toBe("$0.00");
  });
});

describe("the words for a merge decision", () => {
  test("say what the decision was", () => {
    expect(decisionWord("merge")).toBe("same");
    expect(decisionWord("new")).toBe("different");
    expect(decisionWord("queue")).toBe("unsure");
    expect(decisionWord("unresolved")).toBe("no match");
    expect(decisionWord("rule")).toBe("by rule");
  });

  test("an unknown decision keeps its own word", () => {
    expect(decisionWord("other")).toBe("other");
  });
});

describe("a review row", () => {
  test("a validator warning reads its detail and subject", () => {
    expect(
      reviewRow({
        id: 19,
        kind: "validator",
        confidence: null,
        payload: { detail: "the incident has no shift", rule: "incident_no_shift", severity: "warning", subject: "INC-2026-002" },
      }),
    ).toEqual({ id: 19, kind: "warning", text: "the incident has no shift", about: "INC-2026-002", paths: [] });
  });

  test("a conflict adds the values that differ", () => {
    const row = reviewRow({
      id: 2,
      kind: "conflict",
      confidence: null,
      payload: {
        differences: { downtime_min: [200, 220] },
        doc_paths: ["maintenance/maint_log_export_2026.csv"],
        key: "WO-3841",
        reason: "a repeated record differs",
      },
    });
    expect(row.kind).toBe("conflict");
    expect(row.text).toBe("a repeated record differs: downtime_min 200 vs 220");
    expect(row.about).toBe("WO-3841");
    expect(row.paths).toEqual(["maintenance/maint_log_export_2026.csv"]);
  });

  test("an unresolved mention names the mention", () => {
    const row = reviewRow({
      id: 1,
      kind: "unresolved",
      confidence: null,
      payload: { mention: "TAPEPTFE12", reason: "no candidate", doc_paths: ["inventory/spares_2026-10.xlsx"] },
    });
    expect(row).toEqual({ id: 1, kind: "unresolved", text: "no candidate", about: "TAPEPTFE12", paths: ["inventory/spares_2026-10.xlsx"] });
  });

  test("a merge names the two records and the probability that they match", () => {
    const row = reviewRow({
      id: 6,
      kind: "merge",
      confidence: 0.52,
      payload: {
        doc_paths: ["maintenance/maint_log_export_2026.csv", "handovers/shift notes 19 Apr C.txt"],
        p: 0.52,
        reason: "unsure match",
        records: [
          ["maintenance/maint_log_export_2026.csv", "WO-1877"],
          ["handovers/shift notes 19 Apr C.txt", "handovers/shift notes 19 Apr C.txt#1"],
        ],
      },
    });
    expect(row.text).toBe("unsure match, probability 0.52");
    expect(row.about).toBe("WO-1877 and shift notes 19 Apr C.txt#1");
  });

  test("an item with nothing to say has empty text and subject", () => {
    expect(reviewRow({ id: 5, kind: "link", confidence: null, payload: {} })).toEqual({ id: 5, kind: "root cause", text: "", about: "", paths: [] });
  });
});

describe("the node a merge matched", () => {
  test("a node id stays as it is", () => {
    expect(matchName("OIL-HLP46-20L")).toBe("OIL-HLP46-20L");
    expect(matchName(null)).toBe("none");
  });

  test("a record of a file is its file name and its number", () => {
    expect(matchName("handovers/handover_2026-04-14_B.txt::handovers/handover_2026-04-14_B.txt#1")).toBe(
      "handover_2026-04-14_B.txt#1",
    );
  });
});
