import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { shortDate } from "../lib/format";
import type { Citation, Upkeep, UpkeepItem, UpkeepStatus, UpkeepTerm } from "../lib/types";
import { dueLine } from "../lib/upkeep";
import { trapTab } from "./SourceSheet";

const GROUPS: { status: UpkeepStatus; heading: string }[] = [
  { status: "overdue", heading: "Past due" },
  { status: "due_soon", heading: "Due soon" },
  { status: "later", heading: "Later" },
  { status: "not_enough_history", heading: "Not enough history" },
];

/** The due line, with its last sentence (the due date) in bold so it stands out. */
function DueLine({ text }: { text: string }) {
  const cut = text.lastIndexOf(". ", text.length - 2);
  if (cut < 0) return <p className="upkeep-line">{text}</p>;
  return (
    <p className="upkeep-line">
      {text.slice(0, cut + 1)} <strong>{text.slice(cut + 2)}</strong>
    </p>
  );
}

function Row({ item, onOpen }: { item: UpkeepItem; onOpen: (label: string, citations: Citation[]) => void }) {
  const [shown, setShown] = useState(false);
  const n = item.breakdowns.length;
  return (
    <li className="upkeep-row">
      <p className="upkeep-what">
        <span className="id">{item.machine}</span> {item.machine_label}
      </p>
      <p className="upkeep-cause">{item.cause_label}</p>
      <DueLine text={dueLine(item)} />
      <button
        className="link upkeep-based"
        aria-expanded={shown}
        onClick={() => setShown(!shown)}
      >
        Based on {n} {n === 1 ? "breakdown" : "breakdowns"}
      </button>
      {shown && (
        <ul className="upkeep-breakdowns">
          {item.breakdowns.map((b) => (
            <li key={b.id}>
              <button
                className="upkeep-bd"
                onClick={() => onOpen(`${b.id}, ${b.date}`, b.citations)}
              >
                <span className="id">{b.id}</span>
                <span>{shortDate(b.date)}</span>
                <span className="num">{b.downtime_min === null ? "" : `${b.downtime_min} min`}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function pct(n: number): string {
  return `${Number((n * 100).toFixed(1))}%`;
}

function Terms({ terms }: { terms: UpkeepTerm[] }) {
  return (
    <section className="upkeep-terms" aria-labelledby="upkeep-terms-h">
      <h3 id="upkeep-terms-h">Service terms (example)</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">Machine</th>
            <th scope="col">Response</th>
            <th scope="col">Target</th>
            <th scope="col">Measured</th>
            <th scope="col">Covers</th>
          </tr>
        </thead>
        <tbody>
          {terms.map((t) => (
            <tr key={t.machine}>
              <th scope="row" className="id">
                {t.machine}
              </th>
              <td className="num">{t.response_hours} h</td>
              <td className="num">{pct(t.uptime_target)}</td>
              <td className={t.measured_uptime < t.uptime_target ? "num below" : "num"}>
                {pct(t.measured_uptime)}
              </td>
              <td>{t.covers}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="quiet">Example terms for the demo. Measured uptime comes from recorded downtime.</p>
    </section>
  );
}

/** The upkeep side sheet: parts due to fail, each with the breakdowns it rests on. */
export function UpkeepPanel({
  open,
  upkeep,
  failed,
  onClose,
  onOpen,
}: {
  open: boolean;
  upkeep: Upkeep | null;
  failed: boolean;
  onClose: () => void;
  onOpen: (label: string, citations: Citation[]) => void;
}) {
  const root = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement;
    closeButton.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "Tab" && root.current) trapTab(e, root.current);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <aside ref={root} className="upkeep" role="complementary" aria-label="Upkeep">
      <header className="upkeep-head">
        <h2>Upkeep</h2>
        <button ref={closeButton} className="icon-btn" onClick={onClose} aria-label="Close the upkeep">
          <X size={20} aria-hidden="true" />
        </button>
        {upkeep?.as_of && (
          <p className="quiet">
            As of {shortDate(upkeep.as_of)} {upkeep.as_of.slice(0, 4)}, from past breakdowns. No model involved.
          </p>
        )}
      </header>
      <div className="upkeep-body">
        {failed && <p className="plain-error">Upkeep could not be loaded.</p>}
        {!failed && !upkeep && <p className="quiet">Loading.</p>}
        {upkeep &&
          GROUPS.map(({ status, heading }) => {
            const items = upkeep.items.filter((i) => i.status === status);
            return items.length ? (
              <section key={status} aria-label={heading} className="upkeep-group" data-status={status}>
                <h3>
                  <span className="upkeep-tag">{heading}</span>
                  <span className="upkeep-count">{items.length}</span>
                </h3>
                <ul className="upkeep-list">
                  {items.map((i) => (
                    <Row key={`${i.machine}-${i.cause}`} item={i} onOpen={onOpen} />
                  ))}
                </ul>
              </section>
            ) : null;
          })}
        {upkeep && <Terms terms={upkeep.terms} />}
      </div>
    </aside>
  );
}
