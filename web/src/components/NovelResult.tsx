import type { ReactNode } from "react";
import type { NovelCase } from "../lib/novel";

/** What asking an issue the graph has never seen shows in place of an answer: how unsure the
 * system is, what comes closest, and the way to report it. The capture flow goes in as children. */
export function NovelResult({
  novel,
  merged,
  onReport,
  reporting,
  children,
}: {
  novel: NovelCase;
  merged: string | null;
  onReport: () => void;
  reporting: boolean;
  children?: ReactNode;
}) {
  return (
    <section className="novel" aria-label="Not in the graph">
      <h2 className="novel-q">{novel.question}</h2>
      <p className="novel-status">
        <span className="novel-conf">Low confidence ({novel.confidence.toFixed(2)}).</span> This
        looks like a new issue.
      </p>
      {merged && (
        <p className="novel-merged">
          Reported as {merged} and merged. In the live system this question now gets an answer.
        </p>
      )}
      <h3 className="novel-sub">What the graph has that comes closest:</h3>
      {novel.nearest.length > 0 ? (
        <ul className="novel-near">
          {novel.nearest.map((n) => (
            <li key={n.label}>
              <strong>{n.label}</strong>: {n.why}
            </li>
          ))}
        </ul>
      ) : (
        <p className="novel-none">Nothing close. {novel.record.machineLabel.replace(/ \(not in the graph\)$/, "")} is not in the graph.</p>
      )}
      {!reporting && !merged && (
        <button type="button" className="novel-report" onClick={onReport}>
          Report a new issue
        </button>
      )}
      {children}
    </section>
  );
}
