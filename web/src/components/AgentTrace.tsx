import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import type { AskState } from "../lib/askReducer";
import { claimMark, fixed, money } from "../lib/format";
import type { Claim, NodeEnd } from "../lib/types";

const NODE_LABEL: Record<string, string> = {
  triage: "Triage",
  graph_agent: "Graph agent",
  manual_agent: "Manual agent",
  synthesize: "Synthesize",
  verify: "Verify",
  assemble: "Assemble",
};

export function backendName(backend: string | null): string {
  if (!backend) return "code";
  if (backend.startsWith("jev")) return "JEV";
  if (backend.endsWith("terra")) return "Terra";
  if (backend.endsWith("sol")) return "Sol";
  if (backend.endsWith("luna")) return "Decisions";
  return backend;
}

function seconds(n: number): string {
  return `${n < 10 ? n.toFixed(2) : n.toFixed(1)} s`;
}

const FIELDS: [string, string][] = [
  ["machine", "Machine"],
  ["alarm", "Alarm"],
  ["root_cause", "Root cause"],
  ["intent", "Question"],
  ["safety_critical", "Hazard"],
];

function ClaimRow({ claim, kept }: { claim: Claim; kept: boolean }) {
  const mark = claimMark(claim, kept);
  return (
    <li className={kept ? "claim kept" : "claim unverified"}>
      <span className="claim-mark">{mark.label}</span>
      <span>{claim.text}</span>
      {mark.score !== null && <span className="num quiet"> {fixed(mark.score)}</span>}
    </li>
  );
}

export function AgentTrace({ state }: { state: AskState }) {
  const [open, setOpen] = useState(false);
  const nodes: NodeEnd[] = state.card
    ? state.card.trace.map((t) => ({
        node: t.name,
        seconds: t.seconds,
        backend: t.backend,
        cache_hit: t.cache_hit,
        cost_usd: t.cost_usd,
        notes: t.notes,
      }))
    : state.nodes;
  const conf = state.card?.triage.confidence ?? state.triage?.confidence ?? {};
  const claims = state.card?.claims ?? [];
  const unverified = state.card?.unverified ?? [];
  const calls = state.toolCalls;
  const count = nodes.length;
  const columns = Math.max(1, nodes.length + (state.active ? 1 : 0));
  return (
    <section className={open ? "trace open" : "trace"} aria-label="How this answer was built">
      <button
        type="button"
        className="gh trace-toggle"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="gh-title">
          {open ? (
            <ChevronDown size={16} aria-hidden="true" />
          ) : (
            <ChevronRight size={16} aria-hidden="true" />
          )}
          <h2>How this answer was built</h2>
        </span>
        <span className="num">
          {count} {count === 1 ? "step" : "steps"}
        </span>
      </button>
      {open && (
      <div className="trace-scroll">
        <div
          className="cells"
          style={{
            gridTemplateColumns: `repeat(${columns}, 1fr)`,
          }}
        >
          {nodes.length === 0 && !state.active && (
            <div className="cell quiet">No question yet.</div>
          )}
          {nodes.map((n, i) => (
            <div className="cell" key={`${n.node}-${i}`}>
              <b>{NODE_LABEL[n.node] ?? n.node}</b>
              <span className="num">
                {seconds(n.seconds)} {backendName(n.backend)}
              </span>
            </div>
          ))}
          {state.active && (
            <div className="cell running">
              <b>{NODE_LABEL[state.active] ?? state.active}</b>
              <span>running</span>
            </div>
          )}
        </div>
        <div className="trace-detail">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Node</th>
                  <th className="r-al">Time</th>
                  <th>Backend</th>
                  <th>Cache</th>
                  <th className="r-al">Cost</th>
                </tr>
              </thead>
              <tbody>
                {nodes.map((n, i) => (
                  <tr key={`${n.node}-${i}`}>
                    <td>{NODE_LABEL[n.node] ?? n.node}</td>
                    <td className="r-al num">{seconds(n.seconds)}</td>
                    <td>{n.backend ?? "code"}</td>
                    <td>{n.cache_hit === null ? "n/a" : n.cache_hit ? "hit" : "paid"}</td>
                    <td className="r-al num">{money(n.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {Object.keys(conf).length > 0 && (
              <>
                <h3>Triage confidence</h3>
                <ul className="confs">
                  {FIELDS.filter(([k]) => conf[k] !== undefined).map(([k, label]) => (
                    <li key={k}>
                      {label} <b className="num">{fixed(conf[k])}</b>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {calls.length > 0 && (
              <>
                <h3>Graph agent calls</h3>
                <ul className="calls">
                  {calls.map((c, i) => (
                    <li key={i}>
                      <b className="id">{c.name}</b>
                      <span className="quiet">
                        {" "}
                        {Object.entries(c.args)
                          .map(([k, v]) => `${k}=${String(v)}`)
                          .join(", ")}
                      </span>
                      <span className="num"> &rarr; {c.result_count}</span>
                      {c.filled_by === "code" && <span className="quiet"> (filled by code)</span>}
                    </li>
                  ))}
                </ul>
              </>
            )}
            {(claims.length > 0 || unverified.length > 0) && (
              <>
                <h3>Verifier</h3>
                <ul className="claims">
                  {claims.map((c) => (
                    <ClaimRow key={c.id} claim={c} kept />
                  ))}
                  {unverified.map((c) => (
                    <ClaimRow key={c.id} claim={c} kept={false} />
                  ))}
                </ul>
              </>
            )}
        </div>
      </div>
      )}
    </section>
  );
}
