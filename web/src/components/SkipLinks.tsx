/** Where a skip link lands: the first control of the target, or the target itself. */
function jump(id: string, inner: string) {
  const root = document.getElementById(id);
  if (!root) return;
  const target = root.querySelector<HTMLElement>(inner) ?? root;
  target.focus();
  target.scrollIntoView({ block: "nearest" });
}

/** The skip links of the Ingestion view. They show only while they have focus. A button and not
 * a link, because the address bar of this app is the route. */
export function SkipLinks() {
  return (
    <div className="skips">
      <button type="button" className="skip" onClick={() => jump("pl-facts", ".fact-pick")}>
        Skip to facts
      </button>
      <button type="button" className="skip" onClick={() => jump("pl-graph", "input")}>
        Skip to the graph
      </button>
    </div>
  );
}
