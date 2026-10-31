export type View = "ask" | "ingestion";

export const VIEWS: { view: View; label: string }[] = [
  { view: "ask", label: "Ask" },
  { view: "ingestion", label: "Ingestion" },
];

/** Old names of a view. A link saved under one of them opens the view that took its place. */
const OLD_NAMES: Record<string, View> = { pipeline: "ingestion" };

export interface Route {
  view: View;
  arg?: string;
}

/** The route of a location hash such as "#/ingestion/DOC-55c94b21". An old "#/pipeline/..." link
 * opens the same file in Ingestion. Anything else, "#/evals" among it, is Ask. */
export function parseHash(hash: string): Route {
  const [first = "", ...rest] = hash.replace(/^#\/?/, "").split("/");
  const view = VIEWS.find((v) => v.view === first)?.view ?? OLD_NAMES[first];
  if (!view) return { view: "ask" };
  const arg = rest.join("/");
  return arg ? { view, arg } : { view };
}

export function hrefFor(view: View, arg?: string): string {
  return arg ? `#/${view}/${arg}` : `#/${view}`;
}
