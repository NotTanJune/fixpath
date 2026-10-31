import { useEffect, useState } from "react";
import { getJson } from "../lib/api";
import { basename, fixed } from "../lib/format";
import {
  decisionWord,
  matchName,
  reviewRow,
  runFigures,
  type FileRun,
  type PipelineReport,
} from "../lib/runReport";

const sum = (o: Record<string, number> | undefined) =>
  Object.values(o ?? {}).reduce((a, b) => a + b, 0);

function FileLink({ path, onPick }: { path: string; onPick: (path: string) => void }) {
  return (
    <button type="button" className="idlink" title={path} onClick={() => onPick(path)}>
      {basename(path)}
    </button>
  );
}

function FilesTable({ files, onPick }: { files: FileRun[]; onPick: (path: string) => void }) {
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>File</th>
          <th>Type</th>
          <th className="r-al">Nodes</th>
          <th className="r-al">Links</th>
          <th className="r-al">Merges</th>
          <th className="r-al">Flags</th>
        </tr>
      </thead>
      <tbody>
        {files.map((f) => (
          <tr key={f.doc_path}>
            <td>
              <FileLink path={f.doc_path} onPick={onPick} />
            </td>
            <td>{f.doc_type.replace(/_/g, " ")}</td>
            <td className="r-al num">{sum(f.candidates.nodes)}</td>
            <td className="r-al num">{sum(f.candidates.edges)}</td>
            <td className="r-al num">{f.merges.length}</td>
            <td className="r-al num">{f.flags.length}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MergesTable({ report, onPick }: { report: PipelineReport; onPick: (path: string) => void }) {
  const { totals, files } = report;
  const open = files.flatMap((f) => f.merges).filter((m) => m.decision !== "rule");
  return (
    <>
      <ul className="rr-counts">
        {Object.entries(totals.merges).map(([k, v]) => (
          <li key={k}>
            <b className="num">{v}</b> {decisionWord(k)}
          </li>
        ))}
      </ul>
      <table className="tbl">
        <thead>
          <tr>
            <th>File</th>
            <th>Type</th>
            <th>Mention</th>
            <th>Decision</th>
            <th>Matched to</th>
            <th className="r-al">Probability</th>
          </tr>
        </thead>
        <tbody>
          {open.map((m, i) => (
            <tr key={i} className={m.decision === "unresolved" ? "dashed-row" : ""}>
              <td>
                <FileLink path={m.doc_path} onPick={onPick} />
              </td>
              <td>{m.type}</td>
              <td className="id">{m.mention}</td>
              <td>{decisionWord(m.decision)}</td>
              <td className="id" title={m.candidate ?? undefined}>
                {matchName(m.candidate)}
              </td>
              <td className="r-al num">{fixed(m.p)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="quiet rr-after">
        {totals.merges["rule"] ?? 0} more matched by a rule, with no model.
      </p>
    </>
  );
}

function ReviewTable({ report, onPick }: { report: PipelineReport; onPick: (path: string) => void }) {
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Kind</th>
          <th>Problem</th>
          <th>About</th>
          <th>Files</th>
        </tr>
      </thead>
      <tbody>
        {report.review_queue.map((item) => {
          const r = reviewRow(item);
          return (
            <tr key={r.id}>
              <td>
                <span className="review-kind">{r.kind}</span>
              </td>
              <td>{r.text}</td>
              <td className="id">{r.about}</td>
              <td>
                {r.paths.map((p) => (
                  <FileLink key={p} path={p} onPick={onPick} />
                ))}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** The last run, filling the page: its totals in a row, then its files, merges and review items. */
export function RunPanel({ onPick }: { onPick: (path: string) => void }) {
  const [report, setReport] = useState<PipelineReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getJson<PipelineReport>("/api/pipeline/latest")
      .then(setReport)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <p className="plain-error pad">{error}</p>;
  if (!report) return <p className="quiet pad">Reading the last run.</p>;

  return (
    <div className="rr">
      <ul className="rr-totals" aria-label="Totals of the last run">
        {runFigures(report).map((f) => (
          <li key={f.key}>
            <b className="num">{f.value}</b>
            <span>{f.label}</span>
          </li>
        ))}
      </ul>
      <section className="rr-sec" aria-labelledby="rr-files">
        <h2 id="rr-files">
          Files <span className="num">{report.files.length}</span>
        </h2>
        <FilesTable files={report.files} onPick={onPick} />
      </section>
      <section className="rr-sec" aria-labelledby="rr-merges">
        <h2 id="rr-merges">Merges</h2>
        <MergesTable report={report} onPick={onPick} />
      </section>
      <section className="rr-sec" aria-labelledby="rr-review">
        <h2 id="rr-review">
          Review <span className="num">{report.review_queue.length}</span>
        </h2>
        <ReviewTable report={report} onPick={onPick} />
      </section>
    </div>
  );
}
