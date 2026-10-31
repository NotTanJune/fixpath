import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  BAR,
  clampFirst,
  keyFirst,
  loadFraction,
  saveFraction,
  separatorValues,
  type Bounds,
} from "../lib/split";

/** Two panes side by side with a separator the reader drags or moves with the keyboard.
 * The size is kept as a fraction of the container under `split:<id>`, so it survives a
 * resize of the window and a reload. `onResize` runs after the panes have their new sizes
 * (in px), which is when a graph in a pane has to fit itself again. */
export function Split({
  id,
  first,
  second,
  initial,
  minFirst,
  minSecond,
  onResize,
}: {
  id: string;
  first: ReactNode;
  second: ReactNode;
  initial: number; // the first pane's share of the width, 0 to 1
  minFirst: number;
  minSecond: number;
  onResize?: (first: number, second: number) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [fraction, setFraction] = useState(() => loadFraction(id, initial));
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ x: number; first: number } | null>(null);
  const notify = useRef(onResize);
  notify.current = onResize;

  const bounds: Bounds = { width, minFirst, minSecond, bar: BAR };
  const px = width > 0 ? clampFirst(fraction * width, bounds) : 0;

  useLayoutEffect(() => {
    const el = host.current;
    if (!el) return;
    setWidth(el.clientWidth);
    const observer = new ResizeObserver(() => setWidth(el.clientWidth));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // tell the owner when the panes change size, but not for the first layout
  const laidOut = useRef(false);
  useEffect(() => {
    if (width <= 0) return;
    if (laidOut.current) notify.current?.(px, width - BAR - px);
    laidOut.current = true;
  }, [px, width]);

  const move = useCallback(
    (next: number, save: boolean) => {
      if (width <= 0) return;
      const value = Math.round((clampFirst(next, { width, minFirst, minSecond, bar: BAR }) / width) * 10000) / 10000;
      setFraction(value);
      if (save) saveFraction(id, value);
    },
    [id, width, minFirst, minSecond],
  );

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault(); // no text selection starts, and focus is taken below
    e.currentTarget.focus();
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, first: px };
    setDragging(true);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (drag.current) move(drag.current.first + e.clientX - drag.current.x, false);
  };
  const endDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    drag.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId);
    saveFraction(id, fraction);
  };
  const reset = () => {
    setFraction(initial);
    saveFraction(id, initial);
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      reset();
      return;
    }
    const next = keyFirst(e.key, px, bounds);
    if (next === null) return;
    e.preventDefault();
    move(next, true);
  };

  const v = separatorValues(px, bounds);
  return (
    <div
      ref={host}
      className={dragging ? "split dragging" : "split"}
      style={{
        gridTemplateColumns: width > 0 ? `${px}px ${BAR}px minmax(0, 1fr)` : `${fraction * 100}% ${BAR}px minmax(0, 1fr)`,
      }}
    >
      <div className="split-pane">{first}</div>
      <div
        className="split-bar"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize panes"
        aria-valuenow={v.now}
        aria-valuemin={v.min}
        aria-valuemax={v.max}
        tabIndex={0}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={reset}
        onKeyDown={onKeyDown}
      />
      <div className="split-pane">{second}</div>
    </div>
  );
}
