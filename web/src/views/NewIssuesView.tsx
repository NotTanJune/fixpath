import type { Report } from "../lib/reports";
import { STAGES } from "../lib/reports";

function Card({ r }: { r: Report }) {
  const merged = r.stage === "merged";
  const label = STAGES.find((s) => s.stage === r.stage)?.label ?? r.stage;
  const rec = r.record;
  return (
    <li className="rep" data-stage={r.stage}>
      <p className="rep-top">
        <span className="id">{r.id}</span>
        <span className="rep-machine">
          {rec.machine && <span className="id">{rec.machine} </span>}
          {rec.machineLabel}
        </span>
        <span className="upkeep-tag rep-tag">{label}</span>
      </p>
      <h2 className="rep-q">{r.question}</h2>
      <p className="rep-cause">{rec.cause}</p>
      <h3>Fix steps</h3>
      <ol>
        {rec.fixSteps.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ol>
      <h3>Parts used</h3>
      <ul className="rep-parts">
        {rec.parts.map((p) => (
          <li key={p.id}>
            <span className="id">{p.id}</span> {p.name}, {p.qty}
          </li>
        ))}
      </ul>
      <h3>{merged ? "Added to the graph" : "Will be added to the graph"}</h3>
      <ul className="adds">
        {r.adds.map((a) => (
          <li key={a.id}>
            <span className="adds-type">{a.type}</span> <span className="id">{a.id}</span> {a.label}
          </li>
        ))}
      </ul>
    </li>
  );
}

/** The issues technicians reported in this session, newest first. Replay mode only. */
export function NewIssuesView({ issues, replay }: { issues: Report[]; replay: boolean }) {
  if (!replay) return null;
  const list = [...issues].reverse();
  return (
    <div className="issues">
      <h1>New issues</h1>
      <p className="quiet">
        Issues technicians reported that the graph did not know. Demo data, held for this session.
      </p>
      {list.length === 0 ? (
        <p className="issues-empty">No new issues yet. Ask one of the issues the graph has not seen.</p>
      ) : (
        <ul className="rep-list">
          {list.map((r) => (
            <Card key={r.id} r={r} />
          ))}
        </ul>
      )}
    </div>
  );
}
