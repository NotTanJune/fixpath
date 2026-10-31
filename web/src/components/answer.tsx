import { ChevronRight } from "lucide-react";
import { useState, type ReactNode } from "react";
import { causeRank, pastStops, shortOnStock } from "../lib/answerText";
import { fixed, sentenceCase, shortDate, sourceLabel } from "../lib/format";
import { bandSteps, lockoutScope } from "../lib/lockoutSteps";
import type {
  AnswerCard,
  Breakdown,
  Cause,
  Chip,
  Citation,
  Fix,
  PartNeed,
  PartStock,
  SafetyBlock,
} from "../lib/types";
import { IdLink, type OpenSource } from "./IdLink";

const FIELD_LABEL: Record<string, string> = {
  machine: "Machine",
  alarm: "Alarm",
  root_cause: "Root cause",
  safety_critical: "Hazard",
  intent: "Question",
};

// --- the main issue and the root cause ----------------------------------------------

/** The machine and the alarm with its message, the first thing of an answer. */
export function MainIssue({
  machine,
  alarm,
  message,
  citations,
  onOpen,
}: {
  machine: string | null;
  alarm: string | null;
  message: string | null;
  citations: Citation[];
  onOpen: OpenSource;
}) {
  if (!machine && !alarm) return null;
  return (
    <h2 className="issue">
      {machine && <span className="id">{machine}</span>}
      {machine && alarm && <i aria-hidden="true">&middot;</i>}
      {alarm && <IdLink id={alarm} citations={citations} onOpen={onOpen} className="strong" />}
      {alarm && message && <span className="issue-msg">{message}</span>}
    </h2>
  );
}

/** The root cause in the largest type of the page, with how likely it is and how often it was
 * the cause before, in plain words. The confirm chips go in as children, right under it. */
export function RootCause({
  cause,
  all,
  name,
  id,
  alarm,
  machine,
  onOpen,
  children,
}: {
  cause: Cause | null;
  all: Cause[];
  name: string;
  id: string | null;
  alarm: string | null;
  machine: string | null;
  onOpen: OpenSource;
  children?: ReactNode;
}) {
  return (
    <section className="cause" aria-label="Root cause">
      <h2 className="cause-name">{name}</h2>
      {cause && (
        <p className="cause-why">
          <span>
            <b>{causeRank(cause, all)}</b>, {pastStops(cause, alarm, machine)}
          </span>
          {id && <IdLink id={id} citations={cause.citations} onOpen={onOpen} />}
        </p>
      )}
      {children}
    </section>
  );
}

// --- the chips ----------------------------------------------------------------------

export function ChipGroups({
  chips,
  current,
  disabled,
  onPick,
}: {
  chips: Chip[];
  current: Record<string, string | null>;
  disabled: boolean;
  onPick: (chip: Chip) => void;
}) {
  const fields = [...new Set(chips.map((c) => c.field))];
  return (
    <div className="chip-groups">
      {fields.map((f) => (
        <div className="chip-group" key={f} role="group" aria-label={FIELD_LABEL[f] ?? f}>
          <span className="chip-name">{FIELD_LABEL[f] ?? f}</span>
          {chips
            .filter((c) => c.field === f)
            .map((c) => (
              <button
                type="button"
                key={`${c.field}-${c.value}`}
                className={`chip ${f}`}
                aria-pressed={current[f] === c.value}
                aria-disabled={disabled}
                onClick={() => !disabled && onPick(c)}
              >
                <span className="chip-label">{sentenceCase(c.label)}</span>
                {c.p !== null && <span className="num conf">{fixed(c.p)}</span>}
              </button>
            ))}
        </div>
      ))}
    </div>
  );
}

// --- the summary --------------------------------------------------------------------

export function Summary({ text }: { text: string }) {
  const at = text.search(/[.!?](\s|$)/);
  const head = at === -1 ? text : text.slice(0, at + 1);
  const rest = at === -1 ? "" : text.slice(at + 1);
  return (
    <p className="sum">
      <b>{head}</b>
      {rest}
    </p>
  );
}

// --- the lockout band ---------------------------------------------------------------

/** The red band: its text as short numbered steps, and the pages it comes from under them. */
export function Lockout({ card, onOpen }: { card: Pick<AnswerCard, "safety">; onOpen: OpenSource }) {
  const safety: SafetyBlock = card.safety;
  const ids = safety.procedure_ids;
  const steps = bandSteps(card);
  const scope = lockoutScope(safety);
  // the same page cited twice is one link
  const pages = safety.citations.filter(
    (c, i, all) => all.findIndex((o) => o.doc_path === c.doc_path && o.page === c.page) === i,
  );
  return (
    <section className="lock" aria-label="Lockout and tagout">
      <span className="tape" aria-hidden="true" />
      <div>
        <h2>Lockout required{ids.length ? ` (${ids.join(", ")})` : ""}</h2>
        {scope && <p className="lock-scope">{scope}</p>}
        <ol className="lock-steps">
          {steps.map((step, i) => (
            <li key={step}>
              <u>{i + 1}</u>
              <span>{step}</span>
            </li>
          ))}
        </ol>
        {pages.length > 0 && (
          <p className="lock-src">
            {pages.map((c, i) => (
              <button
                key={`${c.doc_path}-${c.page}-${i}`}
                type="button"
                className="lock-link"
                onClick={() => onOpen(ids[0] ?? "Lockout", [c])}
              >
                {sourceLabel(c.doc_path, c.page)}
              </button>
            ))}
          </p>
        )}
      </div>
    </section>
  );
}

/** The band for the moments before the card exists, so that no fix shows without one. */
export function ProvisionalLockout() {
  return (
    <section className="lock" aria-label="Lockout and tagout">
      <span className="tape" aria-hidden="true" />
      <div>
        <h2>Apply lockout and tagout before starting.</h2>
        <p>Verify zero energy. The full answer is not here yet.</p>
      </div>
    </section>
  );
}

// --- the fix ------------------------------------------------------------------------

function linkSteps(text: string, ids: Map<string, Citation[]>, onOpen: OpenSource): ReactNode[] {
  if (!ids.size) return [text];
  const escaped = [...ids.keys()].map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const pieces = text.split(new RegExp(`(${escaped.join("|")})`));
  return pieces.map((piece, i) =>
    ids.has(piece) ? (
      <IdLink
        key={i}
        id={piece}
        citations={ids.get(piece) ?? []}
        onOpen={onOpen}
        className="strong"
      />
    ) : (
      piece
    ),
  );
}

/** Every step of a fix, numbered: none is left behind a button. */
function Steps({
  fix,
  ids,
  onOpen,
}: {
  fix: Fix;
  ids: Map<string, Citation[]>;
  onOpen: OpenSource;
}) {
  return (
    <ol className="steps">
      {fix.steps.map((step, i) => (
        <li key={i} className="stp">
          <u>{i + 1}</u>
          <span>{linkSteps(step, ids, onOpen)}</span>
        </li>
      ))}
    </ol>
  );
}

function FixHead({ fix, onOpen, level }: { fix: Fix; onOpen: OpenSource; level: 2 | 3 }) {
  const Tag = level === 2 ? "h2" : "h3";
  return (
    <div className={level === 2 ? "sec" : "fix-sub"}>
      <Tag>
        Fix <IdLink id={fix.procedure_id} citations={fix.citations} onOpen={onOpen} />{" "}
        <span>{fix.title}</span>
      </Tag>
      <small className="num">
        {fix.est_minutes} min{fix.manual_ref ? ` · manual ${fix.manual_ref}` : ""}
      </small>
    </div>
  );
}

/** The fix: its ID, title and time, and all of its steps. `also` are the other fixes of the same
 * root cause; their steps are shown too. */
export function FixSection({
  fix,
  parts,
  also,
  onOpen,
}: {
  fix: Fix;
  parts: PartNeed[];
  also: Fix[];
  onOpen: OpenSource;
}) {
  const ids = new Map(parts.map((p) => [p.part_no, p.citations]));
  return (
    <section aria-label="Fix">
      <FixHead fix={fix} onOpen={onOpen} level={2} />
      <Steps fix={fix} ids={ids} onOpen={onOpen} />
      {also.map((other) => (
        <div className="fix-also" key={other.procedure_id}>
          <FixHead fix={other} onOpen={onOpen} level={3} />
          <Steps fix={other} ids={ids} onOpen={onOpen} />
        </div>
      ))}
    </section>
  );
}

// --- the other causes ---------------------------------------------------------------

/** The cause that each other fix belongs to: the start of its RESOLVED_BY step. */
export function causeOfFix(fix: Fix): string | null {
  return fix.path.find((p) => p.rel === "RESOLVED_BY" && p.dst === fix.procedure_id)?.src ?? null;
}

/** The cause that the answer picked, and the others. */
export function splitCauses(card: AnswerCard): { picked: Cause | null; others: Cause[] } {
  const id = card.triage.failure_mode_id;
  const picked = card.causes.find((c) => c.failure_mode_id === id) ?? card.causes[0] ?? null;
  return { picked, others: card.causes.filter((c) => c !== picked) };
}

/** The other fixes that sit under the main one: those of the picked cause, and those whose
 * cause the card does not list. The fixes of every other cause go with that cause. */
export function fixesAlongside(card: AnswerCard): Fix[] {
  const { picked, others } = splitCauses(card);
  const listed = new Set(others.map((c) => c.failure_mode_id));
  return card.other_fixes.filter((f) => {
    const cause = causeOfFix(f);
    return !cause || cause === picked?.failure_mode_id || !listed.has(cause);
  });
}

export function OtherCauses({ card, onOpen }: { card: AnswerCard; onOpen: OpenSource }) {
  const { others } = splitCauses(card);
  if (!others.length) return null;
  const alarm = card.triage.alarm_code;
  const machine = card.triage.asset_id;
  const ids = new Map(card.parts.map((p) => [p.part_no, p.citations]));
  return (
    <section aria-label="Other causes">
      <div className="sec">
        <h2>Other causes</h2>
      </div>
      {others.map((c) => {
        const fixes = card.other_fixes.filter((f) => causeOfFix(f) === c.failure_mode_id);
        return (
          <article className="alt" key={c.failure_mode_id}>
            <h3 className="alt-name">{sentenceCase(c.name)}</h3>
            <p className="alt-why">
              <span>
                <b>{causeRank(c, card.causes)}</b>, {pastStops(c, alarm, machine)}
              </span>
              <IdLink id={c.failure_mode_id} citations={c.citations} onOpen={onOpen} />
            </p>
            {fixes.length === 0 && <p className="quiet">No fix on record for this cause.</p>}
            {fixes.map((f) => (
              <div className="fix-also" key={f.procedure_id}>
                <FixHead fix={f} onOpen={onOpen} level={3} />
                <Steps fix={f} ids={ids} onOpen={onOpen} />
              </div>
            ))}
          </article>
        );
      })}
    </section>
  );
}

// --- the parts ----------------------------------------------------------------------

function Facts({
  qty,
  onHand,
  bin,
  asOf,
  extra,
}: {
  qty?: number;
  onHand: number | null;
  bin: string | null;
  asOf: string | null;
  extra?: ReactNode;
}) {
  return (
    <p className="facts num">
      {qty !== undefined && (
        <span>
          <b>{qty}</b> needed
        </span>
      )}
      <span>{bin ? <>Bin <b>{bin}</b></> : "No bin on record"}</span>
      <span>{onHand === null ? "Stock unknown" : <><b>{onHand}</b> on hand</>}</span>
      {asOf && <span className="quiet">stock of {shortDate(asOf)}</span>}
      {extra}
    </p>
  );
}

export function PartsList({ parts, onOpen }: { parts: PartNeed[]; onOpen: OpenSource }) {
  return (
    <section aria-label="Parts">
      <div className="sec">
        <h2>Parts</h2>
      </div>
      {parts.map((p) => {
        const short = shortOnStock(p);
        return (
          <div className={short ? "part short" : "part"} key={p.part_no}>
            <span className="c">
              <IdLink id={p.part_no} citations={p.citations} onOpen={onOpen} className="onchip" />
            </span>
            <div className="part-body">
              <span className="part-desc">{p.description}</span>
              <Facts qty={p.qty} onHand={p.qty_on_hand} bin={p.bin} asOf={p.stock_as_of} />
              {short && <p className="part-short">{short}</p>}
            </div>
          </div>
        );
      })}
    </section>
  );
}

export function StockList({ stock, onOpen }: { stock: PartStock[]; onOpen: OpenSource }) {
  return (
    <section aria-label="Stock">
      <div className="sec">
        <h2>Stock</h2>
      </div>
      {stock.map((p) => (
        <div className="part" key={p.part_no}>
          <span className="c">
            <IdLink id={p.part_no} citations={p.citations} onOpen={onOpen} className="onchip" />
          </span>
          <div className="part-body">
            <span className="part-desc">{p.description}</span>
            <Facts
              onHand={p.qty_on_hand}
              bin={p.bin}
              asOf={p.stock_as_of}
              extra={
                <>
                  {p.lead_time_days !== null && <span>Lead time {p.lead_time_days} days</span>}
                  {p.fits.length > 0 && <span>Fits {p.fits.join(", ")}</span>}
                </>
              }
            />
          </div>
        </div>
      ))}
    </section>
  );
}

// --- past breakdowns on one baseline ------------------------------------------------

const YEAR = 2026;
const T0 = Date.UTC(YEAR, 0, 1);
const T1 = Date.UTC(YEAR + 1, 0, 1);
const BASE_W = 360;
const MIN_PX = 0.05; // one minute of downtime is 0.05 px, on every card
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function Baseline({ items, hot }: { items: Breakdown[]; hot: string | null }) {
  const x = (iso: string) => ((Date.parse(iso) - T0) / (T1 - T0)) * BASE_W;
  return (
    <svg
      className="baseline"
      viewBox={`0 0 ${BASE_W} 58`}
      role="img"
      aria-label={`Past breakdowns on a ${YEAR} time line, each bar as long as its downtime`}
    >
      <line x1="0" x2={BASE_W} y1="14" y2="14" className="bl-axis" />
      {MONTHS.map((m, i) => {
        const at = ((Date.UTC(YEAR, i, 1) - T0) / (T1 - T0)) * BASE_W;
        return (
          <g key={i}>
            <line x1={at} x2={at} y1="14" y2="19" className="bl-tick" />
            <text x={at + 3} y="56" className="bl-month">
              {m}
            </text>
          </g>
        );
      })}
      {items.map((b) => {
        const cx = x(b.date);
        const h = Math.max(3, (b.downtime_min ?? 0) * MIN_PX);
        return (
          <g key={b.incident_id} className={hot === b.incident_id ? "bl-mark hot" : "bl-mark"}>
            <rect x={cx - 3} y="14" width="6" height={h} className="bl-bar" />
            <rect x={cx - 5} y="4" width="10" height="10" className="bl-sq" />
          </g>
        );
      })}
    </svg>
  );
}

/** Past breakdowns, shut until the reader opens them: the count is in the summary. */
export function PastBreakdowns({
  items,
  history,
  onOpen,
}: {
  items: Breakdown[];
  history: AnswerCard["history"];
  onOpen: OpenSource;
}) {
  const [hot, setHot] = useState<string | null>(null);
  return (
    <details className="past">
      <summary>
        <ChevronRight size={18} aria-hidden="true" />
        <h2>Past breakdowns ({items.length})</h2>
      </summary>
      {history && (
        <p className="hist">
          <span className="id">{history.code}</span> on{" "}
          <span className="id">{history.asset_id}</span>:{" "}
          <span className="num">{history.count}</span> times,{" "}
          <span className="num">{history.incidents}</span> breakdowns started with it.
        </p>
      )}
      {items.length > 0 && <Baseline items={items} hot={hot} />}
      {items.map((b) => (
        <div
          className="r vi"
          key={b.incident_id}
          onMouseEnter={() => setHot(b.incident_id)}
          onMouseLeave={() => setHot(null)}
        >
          <span className="c">
            <IdLink id={b.incident_id} citations={b.citations} onOpen={onOpen} className="onchip" />
          </span>
          <span className="num">
            {shortDate(b.date, YEAR)}
            {b.downtime_min !== null && <> &middot; {b.downtime_min} min</>}
            {b.technician && <> &middot; {b.technician}</>}
          </span>
        </div>
      ))}
    </details>
  );
}

// --- flags --------------------------------------------------------------------------

export function Flags({ notes }: { notes: string[] }) {
  if (!notes.length) return null;
  return (
    <section aria-label="Flags">
      <div className="sec">
        <h2>Check these</h2>
      </div>
      <ul className="flags">
        {notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
    </section>
  );
}
