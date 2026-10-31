/** The number of a step of the Pipeline view, a small flat square before the heading it numbers. */
export function Step({ n }: { n: number }) {
  return <span className="step">{n}</span>;
}
