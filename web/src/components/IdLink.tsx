import type { Citation } from "../lib/types";

export type OpenSource = (label: string, citations: Citation[]) => void;

/** An exact ID as the first label of a fact. It opens the source when there is one. */
export function IdLink({
  id,
  citations,
  onOpen,
  className = "",
  text,
}: {
  id: string;
  citations: Citation[];
  onOpen: OpenSource;
  className?: string;
  text?: string; // what the link says when a name reads better than the id
}) {
  if (!citations.length) return <span className={`id ${className}`}>{text ?? id}</span>;
  return (
    <button
      type="button"
      className={`id idlink ${className}`}
      onClick={() => onOpen(id, citations)}
      aria-label={`${id}, open the source`}
    >
      {text ?? id}
    </button>
  );
}
