import { useCallback, useState, type KeyboardEvent, type MouseEvent, type FocusEvent, type ReactNode } from "react";
import { factNames, type FactRef } from "../lib/sourceMarks";

interface Tip {
  names: string[];
  x: number;
  y: number;
}

/** The attributes of a mark that names its facts. It is a stop for the keyboard only when it is
 * `entry`: the arrow keys move between the marks of a source, so that a file with hundreds of
 * marks is not hundreds of tab stops. */
export function markProps(facts: FactRef[], entry: boolean) {
  const names = factNames(facts);
  return {
    "data-facts": names.join("\n"),
    "data-spans": facts.flatMap((f) => (f.at === undefined ? [] : [f.at])).join(" "),
    tabIndex: entry ? 0 : -1,
    "aria-description": names.length ? names.join("; ") : undefined,
  };
}

const TIP_WIDTH = 320;

/** Names the facts of a mark when it is hovered, focused or tapped. Spread `bind` on the element
 * that holds the marks; render `tip` anywhere. */
export function useFactTip(): { bind: Record<string, unknown>; tip: ReactNode } {
  const [state, setState] = useState<Tip | null>(null);

  const show = useCallback((el: Element, at?: { x: number; y: number }) => {
    const names = (el.getAttribute("data-facts") ?? "").split("\n").filter(Boolean);
    if (!names.length) return setState(null);
    const rect = el.getClientRects()[0] ?? el.getBoundingClientRect();
    const x = at ? at.x + 12 : rect.left;
    const y = at ? at.y + 18 : rect.bottom + 4;
    setState({
      names,
      x: Math.max(8, Math.min(x, window.innerWidth - TIP_WIDTH - 8)),
      y: y + names.length * 24 + 24 > window.innerHeight ? Math.max(8, (at ? at.y - 12 : rect.top) - names.length * 24 - 24) : y,
    });
  }, []);

  const bind = {
    onMouseOver: (e: MouseEvent) => {
      const el = (e.target as Element).closest("[data-facts]");
      if (el) show(el, { x: e.clientX, y: e.clientY });
      else setState(null);
    },
    onMouseLeave: () => setState(null),
    onFocus: (e: FocusEvent) => {
      const el = (e.target as Element).closest("[data-facts]");
      if (el) show(el);
    },
    onBlur: () => setState(null),
    onClick: (e: MouseEvent) => {
      const el = (e.target as Element).closest("[data-facts]");
      if (el) show(el, { x: e.clientX, y: e.clientY });
    },
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const from = (e.target as Element).closest("[data-facts]");
      if (!from) return;
      const all = Array.from(e.currentTarget.querySelectorAll<HTMLElement>("[data-facts]"));
      const next = all[all.indexOf(from as HTMLElement) + (e.key === "ArrowDown" ? 1 : -1)];
      if (!next) return;
      e.preventDefault();
      next.focus();
      next.scrollIntoView({ block: "nearest", inline: "nearest" });
    },
  };

  const tip = state && (
    <div className="fact-tip" role="tooltip" style={{ left: state.x, top: state.y, width: TIP_WIDTH }}>
      {state.names.map((n, i) => (
        <p key={i}>{n}</p>
      ))}
    </div>
  );
  return { bind, tip };
}
