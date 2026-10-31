import brand from "../../../config/brand.json";
import { hrefFor, VIEWS, type View } from "../lib/router";
import type { Health } from "../lib/types";
import { money } from "../lib/format";

function modeOf(health: Health | null, failed: boolean): { label: string; kind: string } {
  if (failed) return { label: "No server", kind: "off" };
  if (!health) return { label: "Connecting", kind: "wait" };
  if (health.decider === "unavailable") return { label: "No decider", kind: "off" };
  return health.replay_mode ? { label: "Replay", kind: "replay" } : { label: "Live", kind: "live" };
}

export function TopBar({
  view,
  health,
  failed,
}: {
  view: View;
  health: Health | null;
  failed: boolean;
}) {
  const mode = modeOf(health, failed);
  return (
    <header className="top">
      <a className="brand" href={hrefFor("ask")} aria-label={`${brand.name}, Ask`}>
        {brand.name}
      </a>
      <nav className="tabs" aria-label="Views">
        {VIEWS.map((v) => (
          <a
            key={v.view}
            href={hrefFor(v.view)}
            className={v.view === view ? "tab on" : "tab"}
            aria-current={v.view === view ? "page" : undefined}
          >
            {v.label}
          </a>
        ))}
      </nav>
      <div className="status">
        <span
          className={`pill ${mode.kind}`}
          title={health ? `Decider: ${health.decider}` : undefined}
        >
          {mode.label}
        </span>
        <span className="spend">
          {health
            ? `${money(health.budget.spent_usd)} of $${Math.round(health.budget.limit_usd)}`
            : ""}
        </span>
      </div>
    </header>
  );
}
