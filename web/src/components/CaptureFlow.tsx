import { useState } from "react";
import type { NovelCase, NovelRecord } from "../lib/novel";
import { STAGES, type Report } from "../lib/reports";
import { hrefFor } from "../lib/router";

const STEPS = ["What happened", "What fixed it", "Check and send"];

/** The mock of a technician reporting an issue the graph has not seen: three steps, every field
 * pre-filled, then a simulated review and merge. Nothing is sent or saved. */
export function CaptureFlow({
  novel,
  report,
  onSend,
}: {
  novel: NovelCase;
  /** The report this flow created, once it is sent. */
  report: Report | null;
  onSend: (record: NovelRecord) => void;
}) {
  const r = novel.record;
  const [step, setStep] = useState(0);
  const [symptom, setSymptom] = useState(r.symptom);
  const [downtime, setDowntime] = useState(String(r.downtimeMin));
  const [cause, setCause] = useState(r.cause);
  const [steps, setSteps] = useState(r.fixSteps.join("\n"));
  const [parts, setParts] = useState(r.parts);

  const record = (): NovelRecord => ({
    ...r,
    symptom: symptom.trim(),
    downtimeMin: Number(downtime) || 0,
    cause: cause.trim(),
    fixSteps: steps
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean),
    parts: parts.filter((p) => p.id.trim()),
  });

  if (report) return <Merge report={report} />;

  const setPart = (i: number, patch: Partial<NovelRecord["parts"][number]>) =>
    setParts((all) => all.map((p, j) => (j === i ? { ...p, ...patch } : p)));

  const sum = record();
  const machine = (
    <>
      {r.machine && <span className="id">{r.machine} </span>}
      {r.machineLabel}
    </>
  );
  return (
    <section className="capture" aria-label="Report a new issue">
      <ol className="cap-steps" aria-label="Steps">
        {STEPS.map((label, i) => (
          <li
            key={label}
            className={i === step ? "on" : i < step ? "done" : undefined}
            aria-current={i === step ? "step" : undefined}
          >
            <span className="cap-n">{i + 1}</span> {label}
          </li>
        ))}
      </ol>

      {step === 0 && (
        <div className="cap-form">
          <p className="cap-ro">
            <span className="cap-k">Machine</span>
            <span>{machine}</span>
          </p>
          <label>
            What you saw
            <textarea value={symptom} onChange={(e) => setSymptom(e.target.value)} rows={3} />
          </label>
          <label>
            Downtime in minutes
            <input
              type="number"
              min={0}
              value={downtime}
              onChange={(e) => setDowntime(e.target.value)}
            />
          </label>
        </div>
      )}

      {step === 1 && (
        <div className="cap-form">
          <label>
            Cause you found
            <input value={cause} onChange={(e) => setCause(e.target.value)} autoComplete="off" />
          </label>
          <label>
            Fix steps, one per line
            <textarea value={steps} onChange={(e) => setSteps(e.target.value)} rows={4} />
          </label>
          <fieldset className="cap-parts">
            <legend>Parts used</legend>
            {parts.map((p, i) => (
              <div className="cap-part" key={i}>
                <input
                  aria-label="Part id"
                  value={p.id}
                  onChange={(e) => setPart(i, { id: e.target.value })}
                  autoComplete="off"
                />
                <input
                  aria-label="Part name"
                  value={p.name}
                  onChange={(e) => setPart(i, { name: e.target.value })}
                  autoComplete="off"
                />
                <input
                  aria-label="Quantity"
                  type="number"
                  min={1}
                  value={p.qty}
                  onChange={(e) => setPart(i, { qty: Number(e.target.value) || 1 })}
                />
              </div>
            ))}
          </fieldset>
        </div>
      )}

      {step === 2 && (
        <dl className="cap-sum">
          <dt>Machine</dt>
          <dd>{machine}</dd>
          <dt>What you saw</dt>
          <dd>{sum.symptom}</dd>
          <dt>Downtime</dt>
          <dd>{sum.downtimeMin} min</dd>
          <dt>Cause</dt>
          <dd>{sum.cause}</dd>
          <dt>Fix steps</dt>
          <dd>
            <ol>
              {sum.fixSteps.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ol>
          </dd>
          <dt>Parts used</dt>
          <dd>
            {sum.parts.map((p) => (
              <span className="cap-line" key={p.id}>
                <span className="id">{p.id}</span> {p.name}, {p.qty}
              </span>
            ))}
          </dd>
          <dt>Reported by</dt>
          <dd>{r.technician}</dd>
        </dl>
      )}

      <div className="wb-actions cap-actions">
        {step > 0 && (
          <button type="button" className="wb-btn" onClick={() => setStep(step - 1)}>
            Back
          </button>
        )}
        {step < 2 ? (
          <button type="button" className="wb-submit" onClick={() => setStep(step + 1)}>
            Next
          </button>
        ) : (
          <button type="button" className="wb-submit" onClick={() => onSend(record())}>
            Send report
          </button>
        )}
      </div>
    </section>
  );
}

function Merge({ report }: { report: Report }) {
  const at = STAGES.findIndex((s) => s.stage === report.stage);
  const merged = report.stage === "merged";
  return (
    <section className="capture" aria-label="Report progress">
      <h3 className="cap-h">
        <span className="id">{report.id}</span> sent
      </h3>
      <ol className="merge" aria-live="polite">
        {STAGES.map((s, i) => (
          <li key={s.stage} className={i <= at ? "done" : "wait"}>
            <span className="merge-mark" aria-hidden="true" />
            <span className="merge-label">{s.label}</span>
            {i <= at && <span className="merge-detail">{s.detail}</span>}
          </li>
        ))}
      </ol>
      {merged && (
        <>
          <ul className="adds" aria-label="Added to the graph">
            {report.adds.map((a) => (
              <li key={a.id}>
                <span className="adds-type">{a.type}</span> <span className="id">{a.id}</span>{" "}
                {a.label}
              </li>
            ))}
          </ul>
          <p>
            <a className="link" href={hrefFor("issues")}>
              See it in New issues
            </a>
          </p>
          <p className="cap-demo">Demo only. Nothing is written to the database.</p>
        </>
      )}
    </section>
  );
}
