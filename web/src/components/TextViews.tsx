import { Fragment, useMemo, useRef, type ReactNode } from "react";
import { lineMarks, splitLines, type MarkRange } from "../lib/sourceMarks";
import { markProps, useFactTip } from "./FactTip";
import { useCenterOnCite } from "./useCenterOnCite";

/** A handover or a loose note: the text as it was written, in a page with a comfortable measure,
 * line breaks kept, the cited passage marked. */
export function NoteView({ text, ranges }: { text: string; ranges: MarkRange[] }) {
  const root = useRef<HTMLDivElement>(null);
  const { bind, tip } = useFactTip();
  const entry = Math.max(0, ranges.findIndex((r) => r.strong));
  // the view moves to the cited mark when the text changes and when another mark is the cited one
  useCenterOnCite(root, `${text.length}:${ranges[entry]?.start ?? -1}`);

  const parts: ReactNode[] = [];
  let at = 0;
  ranges.forEach((r, i) => {
    if (r.start > at) parts.push(<Fragment key={`t${i}`}>{text.slice(at, r.start)}</Fragment>);
    parts.push(
      <mark
        key={`m${i}`}
        className={r.strong ? "m strong" : "m faint"}
        data-cite={i === entry ? "" : undefined}
        {...markProps(r.facts, i === entry)}
      >
        {text.slice(r.start, r.end)}
      </mark>,
    );
    at = r.end;
  });
  parts.push(<Fragment key="end">{text.slice(at)}</Fragment>);

  return (
    <div ref={root} className="doc doc-note" role="region" aria-label="Source text" tabIndex={0} {...bind}>
      <div className="note-page">{parts}</div>
      {tip}
    </div>
  );
}

/** A log: numbered lines in a fixed gutter, nothing wrapped (the viewer scrolls sideways), the
 * cited line marked. */
export function LogView({ text, ranges }: { text: string; ranges: MarkRange[] }) {
  const root = useRef<HTMLDivElement>(null);
  const { bind, tip } = useFactTip();
  const lines = useMemo(() => splitLines(text), [text]);
  const marks = useMemo(() => lineMarks(lines, ranges), [lines, ranges]);
  const entry = Math.max(0, marks.findIndex((m) => m?.strong));
  const first = marks.findIndex((m) => m !== null);
  const at = marks.some((m) => m?.strong) ? entry : first;
  useCenterOnCite(root, `${text.length}:${at}`);
  const gutter = `${String(lines.length).length + 1}ch`;

  return (
    <div
      ref={root}
      className="doc doc-log"
      role="region"
      aria-label="Source text, numbered lines"
      tabIndex={0}
      style={{ "--gutter": gutter } as React.CSSProperties}
      {...bind}
    >
      <div className="lines">
        {lines.map((l, i) => {
          const m = marks[i];
          return (
            <div
              key={l.no}
              className={m ? (m.strong ? "ln strong" : "ln faint") : "ln"}
              data-cite={i === at && m ? "" : undefined}
              {...(m ? markProps(m.facts, i === at) : {})}
            >
              <span className="no" aria-hidden="true">
                {l.no}
              </span>
              <span className="tx">{l.text || " "}</span>
            </div>
          );
        })}
      </div>
      {tip}
    </div>
  );
}
