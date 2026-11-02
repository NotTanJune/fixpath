import { PanelLeftClose, PanelLeftOpen, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { downtimeText, seenAt, sourceRequest } from "../lib/activity";
import { sourcesOnce } from "../lib/api";
import { useActivity, useMedia } from "../lib/hooks";
import { savedAt, type SavedRun } from "../lib/history";
import { shortDate } from "../lib/format";
import type { AlarmRow, BreakdownRow } from "../lib/types";
import type { OpenSource } from "./IdLink";
import { trapTab } from "./SourceSheet";

/** From this width the rail stays beside the answer; below it, it is a drawer over it. */
const WIDE = "(min-width: 1280px)";
const PINNED_KEY = "rail";
/** Below this width a question that is asked shuts the wide rail, so the answer has the room. */

/** Whether the wide rail is open: a choice that is kept, open when nothing is kept. */
function usePinned(): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(PINNED_KEY) !== "closed";
    } catch {
      return true;
    }
  });
  const set = (next: boolean) => {
    setOpen(next);
    try {
      localStorage.setItem(PINNED_KEY, next ? "open" : "closed");
    } catch {
      // the choice holds for this visit
    }
  };
  return [open, set];
}

function BreakdownItem({ row, onOpen }: { row: BreakdownRow; onOpen: () => void }) {
  const meta = [
    row.date ? shortDate(row.date, new Date().getFullYear()) : "",
    downtimeText(row.downtime_min),
  ]
    .filter(Boolean)
    .join(", ");
  return (
    <li>
      <button type="button" className="rail-row" onClick={onOpen} disabled={!row.doc_id}>
        <span className="rail-ids">
          <span className="id">{row.machine ?? row.id}</span>
          {row.alarm && <span className="id">{row.alarm}</span>}
        </span>
        {row.cause_name && <span className="rail-what">{row.cause_name}</span>}
        {meta && <span className="rail-meta num">{meta}</span>}
      </button>
    </li>
  );
}

function AlarmItem({ row, onOpen }: { row: AlarmRow; onOpen: () => void }) {
  const meta = seenAt(row.last_seen);
  return (
    <li>
      <button type="button" className="rail-row" onClick={onOpen} disabled={!row.doc_id}>
        <span className="rail-ids">
          <span className="id">{row.machine}</span>
          <span className="id">{row.alarm}</span>
        </span>
        {row.alarm_message && <span className="rail-what">{row.alarm_message}</span>}
        {meta && <span className="rail-meta num">{meta}</span>}
      </button>
    </li>
  );
}

function Panel({
  runs,
  current,
  onReopen,
  onClear,
  onOpen,
  button,
}: {
  runs: SavedRun[];
  current: string;
  onReopen: (run: SavedRun) => void;
  onClear: () => void;
  onOpen: OpenSource;
  button: React.ReactNode;
}) {
  const { activity, failed } = useActivity();

  // a row opens its source in the sheet; the file path comes from the source list
  const open = async (label: string, docId: string | null) => {
    let list: Awaited<ReturnType<typeof sourcesOnce>> = [];
    try {
      list = await sourcesOnce();
    } catch {
      // the sheet still opens by the document ID
    }
    const request = sourceRequest(label, docId, list);
    if (request) onOpen(request.label, request.citations);
  };

  const key = (q: string) => q.trim().replace(/\s+/g, " ").toLowerCase();
  return (
    <>
      <section className="rail-sec rail-activity" aria-labelledby="rail-activity">
        <div className="rail-head">
          <h2 id="rail-activity">Line 3 activity</h2>
          {button}
        </div>
        {failed && !activity && <p className="rail-note">Activity is not available right now.</p>}
        {!failed && !activity && <p className="rail-note">Reading the activity.</p>}
        {activity && (
          <>
            <h3>Breakdowns</h3>
            {activity.breakdowns.length === 0 ? (
              <p className="rail-note">No breakdowns recorded.</p>
            ) : (
              <ul className="rail-list">
                {activity.breakdowns.map((b) => (
                  <BreakdownItem
                    key={b.id}
                    row={b}
                    onOpen={() => open(b.id, b.doc_id)}
                  />
                ))}
              </ul>
            )}
            <h3>Alarms</h3>
            {activity.alarms.length === 0 ? (
              <p className="rail-note">No alarms recorded.</p>
            ) : (
              <ul className="rail-list">
                {activity.alarms.map((a) => (
                  <AlarmItem
                    key={`${a.machine}|${a.alarm}`}
                    row={a}
                    onOpen={() => open(a.alarm, a.doc_id)}
                  />
                ))}
              </ul>
            )}
          </>
        )}
      </section>
      <section className="rail-sec rail-questions" aria-labelledby="rail-questions">
        <div className="rail-head">
          <h2 id="rail-questions">Your questions</h2>
          {runs.length > 0 && (
            <button type="button" className="rail-clear" onClick={onClear}>
              Clear all
            </button>
          )}
        </div>
        {runs.length === 0 ? (
          <p className="rail-note">No questions asked yet</p>
        ) : (
          <ul className="rail-list">
            {runs.map((run) => (
              <li key={run.question}>
                <button
                  type="button"
                  className="rail-row rail-q"
                  aria-current={key(run.question) === key(current) ? "true" : undefined}
                  onClick={() => onReopen(run)}
                >
                  <span className="rail-what">{run.question}</span>
                  <span className="rail-meta num">Asked {savedAt(run.at)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

/** The left rail of the Ask view: what happened lately on the line, and the questions asked in
 * this browser. Beside the answer from 1280 px (and open or shut as the reader left it);
 * below that it starts shut and opens as a drawer over the answer. `asked` counts the
 * questions sent: each one shuts the wide rail to its strip so the answer has the room, and a
 * reopen by the reader keeps it open for that answer. On the idle screen it is open again
 * (unless the reader shut it). */
export function Rail({
  runs,
  current,
  asked,
  idle,
  onReopen,
  onClear,
  onOpen,
}: {
  runs: SavedRun[];
  current: string;
  asked: number;
  idle: boolean;
  onReopen: (run: SavedRun) => void;
  onClear: () => void;
  onOpen: OpenSource;
}) {
  const wide = useMedia(WIDE);
  const [pinned, setPinned] = usePinned();
  const [drawer, setDrawer] = useState(false);
  // shut for this answer only: the kept choice of the reader is not changed by it
  const [tucked, setTucked] = useState(false);
  const seen = useRef(asked);
  useLayoutEffect(() => {
    if (asked === seen.current) return;
    seen.current = asked;
    setTucked(true);
  }, [asked]);
  // the idle screen has its rail back: "New question" returns to it with the questions in reach
  useLayoutEffect(() => {
    if (idle) setTucked(false);
  }, [idle]);
  const open = wide ? pinned && !tucked : drawer;
  const id = useId();
  const strip = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const focusNext = useRef<"strip" | "close" | null>(null);

  const set = (next: boolean) => {
    focusNext.current = next ? "close" : "strip";
    if (next) setTucked(false);
    if (wide) setPinned(next);
    else setDrawer(next);
  };

  // the button that was used goes away with its panel: the keyboard follows to the other one
  useEffect(() => {
    const target = focusNext.current === "close" ? close.current : strip.current;
    if (focusNext.current) target?.focus();
    focusNext.current = null;
  }, [open]);

  useEffect(() => {
    if (wide) setDrawer(false);
  }, [wide]);

  // the drawer: Escape closes it, Tab stays inside it
  const modal = open && !wide;
  useEffect(() => {
    if (!modal) return;
    const onKey = (e: KeyboardEvent) => {
      if (document.querySelector(".sheet")) return; // the source sheet has its own keys
      if (e.key === "Escape") {
        e.stopPropagation();
        set(false);
      } else if (e.key === "Tab" && panel.current) trapTab(e, panel.current);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modal]);

  const reopen = (run: SavedRun) => {
    onReopen(run);
    if (modal) set(false);
  };

  const toggle = (
    <button
      ref={close}
      type="button"
      className="rail-toggle"
      aria-label={modal ? "Close the history" : "Hide the history"}
      aria-controls={id}
      aria-expanded="true"
      onClick={() => set(false)}
    >
      {modal ? (
        <X size={20} aria-hidden="true" />
      ) : (
        <PanelLeftClose size={20} aria-hidden="true" />
      )}
    </button>
  );

  return (
    <>
      {!(open && wide) && (
        <div className="rail-strip">
          <button
            ref={strip}
            type="button"
            className="rail-toggle"
            aria-label="Show the history"
            aria-controls={id}
            aria-expanded={open}
            onClick={() => set(true)}
          >
            <PanelLeftOpen size={20} aria-hidden="true" />
          </button>
        </div>
      )}
      {open && (
        <>
          {modal && <div className="rail-scrim" aria-hidden="true" onClick={() => set(false)} />}
          <aside
            ref={panel}
            id={id}
            className={modal ? "rail drawer" : "rail"}
            aria-label="History"
            role={modal ? "dialog" : undefined}
            aria-modal={modal ? "true" : undefined}
          >
            <Panel
              runs={runs}
              current={current}
              onReopen={reopen}
              onClear={onClear}
              onOpen={onOpen}
              button={toggle}
            />
          </aside>
        </>
      )}
    </>
  );
}
