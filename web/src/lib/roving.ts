import { useEffect, useRef } from "react";

/** The index to move focus to for a key, or null when the key does not move it. */
export function nextIndex(key: string, i: number, n: number): number | null {
  if (n <= 0) return null;
  switch (key) {
    case "ArrowDown":
    case "ArrowRight":
      return Math.min(n - 1, i + 1);
    case "ArrowUp":
    case "ArrowLeft":
      return Math.max(0, i - 1);
    case "Home":
      return 0;
    case "End":
      return n - 1;
    default:
      return null;
  }
}

/** One tab stop for a chart of many marks: the marks carry data-rv, Tab reaches the chart once,
 * and the arrow keys, Home and End move between its marks. Pass the values the marks depend on
 * so a changed set of marks is picked up. */
export function useRoving<T extends HTMLElement>(...deps: unknown[]) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const items = () => [...root.querySelectorAll<HTMLElement | SVGElement>("[data-rv]")];
    const stop = (el?: Element | null) =>
      items().forEach((it, k) => {
        it.tabIndex = (el ? it === el : k === 0) ? 0 : -1;
      });
    stop();
    const onFocusIn = (e: FocusEvent) => {
      const t = (e.target as Element).closest("[data-rv]");
      if (t && root.contains(t)) stop(t);
    };
    const onKey = (e: KeyboardEvent) => {
      const list = items();
      const i = list.findIndex((it) => it === document.activeElement);
      if (i < 0) return;
      const to = nextIndex(e.key, i, list.length);
      if (to === null) return;
      e.preventDefault();
      list[to]?.focus();
    };
    root.addEventListener("focusin", onFocusIn);
    root.addEventListener("keydown", onKey);
    return () => {
      root.removeEventListener("focusin", onFocusIn);
      root.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return ref;
}
