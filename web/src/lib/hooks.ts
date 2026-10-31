import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  type RefObject,
  useState,
  useSyncExternalStore,
} from "react";
import { getActivity, getGraph, getHealth, onActivityStale, streamAsk } from "./api";
import { alarmFacts } from "./answerText";
import { askReducer, initialAsk, type AskState } from "./askReducer";
import { clearRuns, loadRuns, saveRun, type SavedEvent, type SavedRun } from "./history";
import { parseHash, type Route } from "./router";
import { Follow, followTo, STAGE_SECTIONS, union, type Span } from "./follow";
import { paceFactor } from "./pace";
import { advance, gate, nextDue, skip, START, STAGES, type Reveal } from "./reveal";
import type { Activity, GraphElements, Health } from "./types";

export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function useHealth(): { health: Health | null; failed: boolean; refresh: () => void } {
  const [health, setHealth] = useState<Health | null>(null);
  const [failed, setFailed] = useState(false);
  const refresh = useCallback(() => {
    getHealth()
      .then((h) => {
        setHealth(h);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  }, []);
  useEffect(refresh, [refresh]);
  return { health, failed, refresh };
}

/** One question at a time: the reducer state of the stream, and the calls that drive it.
 * `onRecord` gets the question and the events of a run that reached its answer, to be saved. */
export function useAsk(
  onFinished: () => void,
  onRecord?: (question: string, events: SavedEvent[]) => void,
) {
  const [state, dispatch] = useReducer(askReducer, initialAsk);
  const controller = useRef<AbortController | null>(null);
  const record = useRef(onRecord);
  record.current = onRecord;

  const send = useCallback(
    (question: string) => {
      controller.current?.abort();
      const own = new AbortController();
      controller.current = own;
      const events: SavedEvent[] = [];
      const save = () => {
        // a run that was stopped or replaced, or that never gave an answer, is not kept
        if (!own.signal.aborted && events.some((e) => e.name === "answer")) {
          record.current?.(question, events);
        }
      };
      dispatch({ type: "start", question });
      streamAsk(
        question,
        (name, data) => {
          if (own.signal.aborted) return; // a run that was dropped says nothing more
          events.push({ name, data });
          dispatch({ type: "event", name, data });
        },
        own.signal,
      )
        .then(() => {
          if (!own.signal.aborted) {
            dispatch({
              type: "closed",
              message: "The connection closed before the answer finished.",
            });
            save();
          }
        })
        .catch((error: Error) => {
          dispatch({ type: "failed", message: error.message });
          save();
        })
        .finally(onFinished);
    },
    [onFinished],
  );

  const stop = useCallback(() => {
    controller.current?.abort();
    dispatch({ type: "closed", message: "Stopped before the answer finished." });
  }, []);

  /** Shows a saved answer at once: no request is made. A run in progress is dropped. */
  const restore = useCallback((run: SavedRun) => {
    controller.current?.abort();
    controller.current = null;
    dispatch({ type: "restore", question: run.question, events: run.events, at: run.at });
  }, []);

  /** Back to the idle screen: a run in progress is dropped and its answer is not kept. */
  const reset = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    dispatch({ type: "reset" });
  }, []);

  return { state, send, stop, restore, reset };
}

/** The questions asked in this browser, and the calls that add to the list and clear it. */
export function useHistory() {
  const [runs, setRuns] = useState<SavedRun[]>(() => loadRuns());
  const record = useCallback(
    (question: string, events: SavedEvent[]) => setRuns(saveRun(question, events)),
    [],
  );
  const clear = useCallback(() => {
    clearRuns();
    setRuns([]);
  }, []);
  return { runs, record, clear };
}

/** What happened lately on the line. It is read again after a live drop has been ingested.
 * `failed` is true when the server did not give it. */
export function useActivity(): { activity: Activity | null; failed: boolean } {
  const [activity, setActivity] = useState<Activity | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    const load = () =>
      getActivity(5, 3)
        .then((a) => {
          if (!live) return;
          setActivity(a);
          setFailed(false);
        })
        .catch(() => live && setFailed(true));
    load();
    const stop = onActivityStale(load);
    return () => {
      live = false;
      stop();
    };
  }, []);
  return { activity, failed };
}

/** Whether a media query matches now, and again when it changes. */
export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      // a resize of the window also says so, where a change event of the query does not come
      window.addEventListener("resize", notify);
      return () => {
        list.removeEventListener("change", notify);
        window.removeEventListener("resize", notify);
      };
    },
    () => window.matchMedia(query).matches,
  );
}

const alarmGraphs = new Map<string, Promise<GraphElements>>();

/** What the graph says about an alarm: its message, and the name of its machine. Both are null
 * until the graph has answered, and stay null when it does not. */
export function useAlarmFacts(
  alarm: string | null,
  machine: string | null,
): { message: string | null; machineName: string | null } {
  const [graph, setGraph] = useState<{ alarm: string; elements: GraphElements } | null>(null);
  useEffect(() => {
    if (!alarm) return;
    let live = true;
    let pending = alarmGraphs.get(alarm);
    if (!pending) {
      pending = getGraph(alarm, 1);
      alarmGraphs.set(alarm, pending);
      pending.catch(() => alarmGraphs.delete(alarm));
    }
    pending.then((elements) => live && setGraph({ alarm, elements })).catch(() => {});
    return () => {
      live = false;
    };
  }, [alarm]);
  return graph && graph.alarm === alarm
    ? alarmFacts(graph.elements, alarm, machine)
    : { message: null, machineName: null };
}

export function reducedMotion(): boolean {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Which stages of the answer are on the screen, and the call that shows them all. The clock is the
 * pure one of lib/reveal.ts: a stage shows when its data has arrived and the one before it has had
 * its time (PACE), a late one shows when it comes, and a saved answer, an answer under reduced
 * motion or with ?pace=0, and a run that ended in an error show what they have at once. */
export function useReveal(state: AskState): { shown: number; skipAll: () => void } {
  const [held, setHeld] = useState<{ run: number; reveal: Reveal }>({ run: state.run, reveal: START });
  const current = held.run === state.run ? held.reveal : START;
  const g = gate(state);

  useEffect(() => {
    const tick = () =>
      setHeld((h) => {
        const base = h.run === state.run ? h.reveal : START;
        const next = advance(base, g, performance.now());
        return next === base && h.run === state.run ? h : { run: state.run, reveal: next };
      });
    const now = performance.now();
    const due = nextDue(current, g, now);
    if (due === null) return;
    const id = window.setTimeout(tick, Math.max(0, due - now));
    return () => window.clearTimeout(id);
    // g is rebuilt on every render; what it says is in these
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.run, g.arrived, g.instant, current]);

  const skipAll = useCallback(() => {
    setHeld((h) => ({ run: state.run, reveal: skip(h.run === state.run ? h.reveal : START) }));
  }, [state.run]);

  return { shown: g.instant && current.shown < g.arrived ? g.arrived : current.shown, skipAll };
}

const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);

/** Scroll the work pane so that the section a stage has just brought in is in view. The scroll is
 * smooth, and instant under reduced motion or pace 0. Once the reader scrolls the pane themselves
 * (wheel, touch, scrollbar or a scroll key) it stops following until the next question. A jump over
 * several stages is read from the top and is not followed. */
export function useFollowReveal(pane: RefObject<HTMLElement | null>, shown: number, run: number): void {
  const seen = useRef({ run, shown });
  const follow = useRef(new Follow());

  useEffect(() => {
    const el = pane.current;
    if (!el) return;
    const stop = () => follow.current.stop();
    const key = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (SCROLL_KEYS.has(e.key) && !(t && /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName))) stop();
    };
    el.addEventListener("wheel", stop, { passive: true });
    el.addEventListener("touchstart", stop, { passive: true });
    el.addEventListener("pointerdown", stop);
    el.addEventListener("keydown", key);
    return () => {
      el.removeEventListener("wheel", stop);
      el.removeEventListener("touchstart", stop);
      el.removeEventListener("pointerdown", stop);
      el.removeEventListener("keydown", key);
    };
  });

  useEffect(() => {
    const before = seen.current.run === run ? seen.current.shown : 0;
    seen.current = { run, shown };
    const el = pane.current;
    if (!el || !follow.current.wants(run, before, shown)) return;
    const stage = STAGES[shown - 1];
    const selectors = stage ? STAGE_SECTIONS[stage] : undefined;
    if (!selectors) return;
    const spans: Span[] = [];
    for (const sel of selectors) {
      const found = el.querySelector(sel);
      if (found) {
        const r = found.getBoundingClientRect();
        spans.push({ top: r.top, bottom: r.bottom });
      }
    }
    const region = union(spans);
    if (!region) return;
    const box = el.getBoundingClientRect();
    // the bar that holds "New question" stays at the top of the pane: the region lands under it
    const bar = el.querySelector<HTMLElement>(".new-q-bar")?.offsetHeight ?? 0;
    const top = followTo({ top: box.top + bar, bottom: box.bottom }, region, el.scrollTop, 16);
    if (top === null) return;
    el.scrollTo({ top, behavior: paceFactor() === 0 ? "auto" : "smooth" });
  }, [pane, shown, run]);
}
