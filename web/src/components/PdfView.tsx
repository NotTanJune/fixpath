import { ChevronDown, ChevronUp, Minus, Plus } from "lucide-react";
import type { PDFDocumentLoadingTask, PDFPageProxy } from "pdfjs-dist";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { loadPdfjs } from "../lib/pdfjs";
import { pageInView, parsePage, stepPage } from "../lib/pdfPages";
import { matchQuote, pdfMarks, segmentRects, type ItemBox, type PageMark, type Rect } from "../lib/pdfQuote";
import {
  anchorShift,
  canvasScale,
  centreScroll,
  clampZoom,
  commonWidth,
  fitZoom,
  focusRect,
  neighbourRects,
  pageZoom,
  readingZoom,
  stepZoom,
  wheelZoom,
  ZOOM_MAX,
  ZOOM_MIN,
} from "../lib/pdfZoom";
import type { SourceDoc } from "../lib/types";
import { markProps, useFactTip } from "./FactTip";
import type { SourceFocus } from "./SourceView";

const GUTTER = 24; // the space left and right of a page, in px
const RENDER_DELAY = 160; // ms a zoom must rest before the pages are drawn again at the new size

interface TextItemLike {
  str?: string;
  transform?: number[];
  width?: number;
  height?: number;
}

/** The text of a page as its items and their boxes, in page units (px at zoom 1). Read once. */
interface PageText {
  items: TextItemLike[];
  boxes: ItemBox[];
}

interface PageInfo {
  proxy: PDFPageProxy;
  width: number; // at zoom 1
  height: number;
  text?: Promise<PageText>;
}

/** What is drawn on one page, in page units: the rectangles of each mark that was found, or, for
 * the cited quote that the text of the page does not hold, a band over the page. */
interface Drawn {
  marks: { mark: PageMark; rects: Rect[] }[];
  missing: PageMark | null;
}

function textOf(info: PageInfo): Promise<PageText> {
  info.text ??= info.proxy.getTextContent().then((content) => {
    const viewport = info.proxy.getViewport({ scale: 1 });
    const items = (content.items as TextItemLike[]).filter((i) => typeof i.str === "string");
    const boxes: ItemBox[] = items.map((i) => {
      const t = i.transform ?? [1, 0, 0, 1, 0, 0];
      const w = i.width ?? 0;
      const h = i.height ?? 0;
      // the item's box: from a little below its baseline to a little below its top
      const [x1, y1] = viewport.convertToViewportPoint(t[4]!, t[5]! - h * 0.2) as [number, number];
      const [x2, y2] = viewport.convertToViewportPoint(t[4]! + w, t[5]! + h * 0.88) as [number, number];
      return { str: i.str ?? "", x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1) };
    });
    return { items, boxes };
  });
  return info.text;
}

/** The marks of a page found in its text. */
function drawn(text: PageText, marks: PageMark[]): Drawn {
  const found: Drawn = { marks: [], missing: null };
  for (const mark of marks) {
    const segments = matchQuote(text.items as { str: string }[], mark.quote);
    const rects = segments ? segmentRects(segments, text.boxes) : [];
    if (rects.length) found.marks.push({ mark, rects });
    else if (mark.strong && !found.missing) found.missing = mark;
  }
  return found;
}

function PdfPage({
  info,
  number,
  total,
  zoom,
  marks,
  root,
  entry,
}: {
  info: PageInfo;
  number: number;
  total: number;
  zoom: number;
  marks: PageMark[];
  root: HTMLElement | null;
  entry: PageMark | null;
}) {
  const box = useRef<HTMLElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [near, setNear] = useState(false);
  const [found, setFound] = useState<Drawn | null>(null);
  const [sharp, setSharp] = useState(zoom); // the zoom the canvas is drawn at: it follows `zoom` once that rests
  const width = info.width * zoom;
  const height = info.height * zoom;

  // a page is drawn while it is near the viewport, and its canvas is let go when it is far
  useEffect(() => {
    const el = box.current;
    if (!el || !root) return;
    const watch = new IntersectionObserver((hits) => hits.forEach((h) => setNear(h.isIntersecting)), {
      root,
      rootMargin: "700px 200px",
    });
    watch.observe(el);
    return () => watch.disconnect();
  }, [root]);

  // the canvas follows a zoom after it has rested, so that a wheel turning does not draw every step
  useEffect(() => {
    if (zoom === sharp) return;
    const t = window.setTimeout(() => setSharp(zoom), RENDER_DELAY);
    return () => window.clearTimeout(t);
  }, [zoom, sharp]);

  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    if (!near) {
      el.width = 0;
      el.height = 0;
      return;
    }
    // the page is drawn aside and then put on the canvas, so that the old picture stays until the new one is ready
    const aside = document.createElement("canvas");
    const k = canvasScale(sharp, window.devicePixelRatio || 1, info.width, info.height);
    const viewport = info.proxy.getViewport({ scale: sharp });
    aside.width = Math.floor(viewport.width * k);
    aside.height = Math.floor(viewport.height * k);
    const task = info.proxy.render({
      canvas: aside,
      viewport,
      transform: k === 1 ? undefined : [k, 0, 0, k, 0, 0],
    });
    task.promise
      .then(() => {
        el.width = aside.width;
        el.height = aside.height;
        el.getContext("2d")?.drawImage(aside, 0, 0);
      })
      .catch((e: Error) => {
        if (e?.name !== "RenderingCancelledException") console.error(e);
      });
    return () => task.cancel();
  }, [near, info, sharp]);

  // the marks are found in the text of the page once; they are drawn at any zoom from page units
  useEffect(() => {
    if (!near) return;
    if (!marks.length) {
      setFound(null);
      return;
    }
    let live = true;
    void textOf(info).then((text) => live && setFound(drawn(text, marks)));
    return () => {
      live = false;
    };
  }, [near, info, marks]);

  return (
    <section ref={box} className={found?.missing ? "pdf-page band" : "pdf-page"} data-page={number} style={{ width }} aria-label={`Page ${number} of ${total}`}>
      <h3 className="pdf-no">
        Page {number} <span className="quiet">of {total}</span>
      </h3>
      {found?.missing && <p className="pdf-band">The quote is on this page.</p>}
      <div className="pdf-sheet" style={{ width, height }}>
        <canvas ref={canvas} style={{ width, height }} role="img" aria-label={`Page ${number} as printed`} />
        {found?.marks.map(({ mark, rects }, mi) =>
          rects.map((r, ri) => (
            <div
              key={`${mi}-${ri}`}
              className={mark.strong ? "pdf-mark strong" : "pdf-mark faint"}
              data-cite={mark === entry && ri === 0 ? "" : undefined}
              style={{ left: r.x * zoom - 2, top: r.y * zoom - 1, width: r.w * zoom + 4, height: r.h * zoom + 2 }}
              {...markProps(mark.facts, mark === entry && ri === 0)}
            />
          )),
        )}
      </div>
    </section>
  );
}

/** Where a quote is: its page and its rectangles in page units. */
interface Located {
  page: number;
  rects: Rect[];
  /** The lines of text around it, so that they are held in view whole with it. */
  around: Rect[];
}

/** A manual as the PDF it is: its pages drawn from the original file, one below the other,
 * numbered, the cited quote marked. It opens zoomed so that the cited quote is read at about 15 px,
 * with the quote in the middle of the view; a bar at the top sets the zoom (Fit width, minus, plus;
 * Ctrl or Cmd with the wheel, or a pinch, zooms around the pointer). A page is drawn only when it
 * is near the viewport, at the device's pixel ratio times the zoom. */
export default function PdfView({ doc, focus }: { doc: SourceDoc; focus: SourceFocus }) {
  const [pages, setPages] = useState<PageInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pane, setPane] = useState(0);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);
  const { bind, tip } = useFactTip();

  // the zoom: the fit of the pane until the reader or the cited quote sets one
  const [mode, setMode] = useState<"fit" | "set">("fit");
  const [set, setSet] = useState(1);
  const [ready, setReady] = useState(false);
  const paneReady = pane > 0;
  // the pages of a manual are not all one size (landscape pages among portrait ones): the fit is of
  // the width most pages have, and in fit mode a wider page is fitted to the pane on its own
  const common = useMemo(() => Math.max(1, commonWidth((pages ?? []).map((p) => p.width))), [pages]);
  const fit = fitZoom(pane, common);
  const zoom = mode === "fit" ? fit : set;
  const zoomNow = useRef(zoom);
  zoomNow.current = zoom;
  /** The zoom page `n` (1-based) is drawn at now. */
  const zoomOf = (n: number) => pageZoom(zoom, mode === "fit", pane, pages?.[n - 1]?.width ?? common, common);
  const zoomFor = useRef(zoomOf);
  zoomFor.current = zoomOf;
  const auto = useRef(false); // the zoom of the quote is applied once for a document
  const pending = useRef<Located | null>(null); // the quote to bring to the middle
  const pagesNow = useRef<PageInfo[] | null>(null);
  pagesNow.current = pages;
  const anchor = useRef<{ page: number; fx: number; fy: number; x: number; y: number } | null>(null);
  const [nudge, setNudge] = useState(0);
  const [current, setCurrent] = useState(1); // the page that is read now
  const [draft, setDraft] = useState<string | null>(null); // what the reader is typing into the page indicator

  useEffect(() => {
    let live = true;
    let task: PDFDocumentLoadingTask | null = null;
    setPages(null);
    setError(null);
    setReady(false);
    setMode("fit");
    setCurrent(1);
    setDraft(null);
    auto.current = false;
    pending.current = null;
    anchor.current = null;
    void (async () => {
      try {
        const lib = await loadPdfjs();
        task = lib.getDocument({ url: `/api/sources/${encodeURIComponent(doc.id)}/file` });
        const loaded = await task.promise;
        const all = await Promise.all(
          Array.from({ length: loaded.numPages }, async (_, i) => {
            const proxy = await loaded.getPage(i + 1);
            const v = proxy.getViewport({ scale: 1 });
            return { proxy, width: v.width, height: v.height };
          }),
        );
        if (live) setPages(all);
      } catch {
        if (live) setError("The PDF could not be shown. Open the original file.");
      }
    })();
    return () => {
      live = false;
      void task?.destroy();
    };
  }, [doc.id]);

  useEffect(() => {
    if (!root) return;
    const read = () => setPane(root.clientWidth - 2 * GUTTER);
    read();
    const observer = new ResizeObserver(read);
    observer.observe(root);
    return () => observer.disconnect();
  }, [root]);

  const marks = useMemo(() => pdfMarks(doc, focus), [doc, focus]);
  const entry = useMemo(() => marks.find((m) => m.strong) ?? null, [marks]);
  const byPage = useMemo(() => {
    const map = new Map<number, PageMark[]>();
    for (const m of marks) map.set(m.page, [...(map.get(m.page) ?? []), m]);
    return map;
  }, [marks]);

  // find the cited quote on its page; the first time for a document, open at the zoom that reads it
  useEffect(() => {
    if (!pages || !paneReady) return;
    let live = true;
    const info = entry ? pages[entry.page - 1] : undefined;
    const done = (found: Located | null) => {
      if (!live) return;
      if (found && !auto.current) {
        auto.current = true;
        setSet(readingZoom(found.rects, fit, root?.clientWidth ?? 0, undefined, found.around));
        setMode("set");
      }
      pending.current = found;
      setReady(true);
      setNudge((n) => n + 1);
    };
    if (!entry || !info) {
      done(null);
      return;
    }
    void textOf(info).then((text) => {
      const hit = drawn(text, [entry]).marks[0];
      done(hit ? { page: entry.page, rects: hit.rects, around: neighbourRects(text.boxes, hit.rects) } : null);
    });
    return () => {
      live = false;
    };
    // the pane width is read once for the first zoom: a drag of the separator does not reopen the quote
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pages, entry, paneReady, common, root]);

  const centre = useCallback(() => {
    const found = pending.current;
    if (!root || !found) return;
    const sheet = root.querySelector<HTMLElement>(`[data-page="${found.page}"] .pdf-sheet`);
    if (!sheet) return;
    const z = zoomFor.current(found.page);
    const rr = root.getBoundingClientRect();
    const sr = sheet.getBoundingClientRect();
    const page = { x: sr.left - rr.left + root.scrollLeft, y: sr.top - rr.top + root.scrollTop };
    const rect = focusRect(found.rects, (root.clientHeight * 0.8) / z);
    if (!rect) return;
    const at = centreScroll(rect, z, page, { w: root.clientWidth, h: root.clientHeight }, { w: root.scrollWidth, h: root.scrollHeight }, found.around);
    root.scrollLeft = at.left;
    root.scrollTop = at.top;
  }, [root]);

  // after the page has its new size: the quote to the middle, or the point under the pointer kept in place
  useLayoutEffect(() => {
    if (!root) return;
    const held = anchor.current;
    if (held) {
      anchor.current = null;
      const sheet = root.querySelector<HTMLElement>(`[data-page="${held.page}"] .pdf-sheet`);
      if (sheet) {
        const r = sheet.getBoundingClientRect();
        const { dx, dy } = anchorShift(held, zoomFor.current(held.page), { x: r.left, y: r.top }, { x: held.x, y: held.y });
        root.scrollLeft += dx;
        root.scrollTop += dy;
      }
    }
    if (!pending.current) return;
    centre();
    const frame = requestAnimationFrame(() => {
      centre();
      pending.current = null;
    });
    return () => cancelAnimationFrame(frame);
  }, [root, zoom, nudge, ready, centre]);

  // the page indicator follows the scroll: the page with the most of its height in the view
  const reading = useCallback(() => {
    if (!root) return;
    const rr = root.getBoundingClientRect();
    const sheets = Array.from(root.querySelectorAll<HTMLElement>(".pdf-sheet"));
    if (!sheets.length) return;
    const spans = sheets.map((s) => {
      const r = s.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom };
    });
    setCurrent(pageInView(spans, rr.top, rr.bottom));
  }, [root]);

  useEffect(() => {
    if (!root) return;
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(reading);
    };
    root.addEventListener("scroll", onScroll, { passive: true });
    reading();
    return () => {
      cancelAnimationFrame(frame);
      root.removeEventListener("scroll", onScroll);
    };
  }, [root, reading, ready, pages, zoom]);

  /** Scroll so that page `n` starts at the top of the view, with its number above it. */
  const goTo = useCallback(
    (n: number) => {
      if (!root) return;
      const section = root.querySelector<HTMLElement>(`[data-page="${n}"]`);
      if (!section) return;
      pending.current = null; // the quote is not brought back to the middle after the reader has moved on
      anchor.current = null;
      const rr = root.getBoundingClientRect();
      root.scrollTop += section.getBoundingClientRect().top - rr.top - 12;
      setCurrent(n);
    },
    [root],
  );

  const total = pages?.length ?? 0;
  const commitDraft = () => {
    const n = draft === null ? null : parsePage(draft, total);
    setDraft(null);
    if (n !== null) goTo(n);
  };

  /** Zoom to `next`, keeping the point of the page under (x, y) of the screen where it is. */
  const zoomTo = useCallback(
    (next: number, point?: { x: number; y: number }, fitToo = false) => {
      if (!root) return;
      const prev = zoomNow.current;
      const target = clampZoom(next);
      auto.current = true; // a reader's zoom is not replaced by the quote's
      const rr = root.getBoundingClientRect();
      const at = point ?? { x: rr.left + root.clientWidth / 2, y: rr.top + root.clientHeight / 2 };
      // the page nearest to the pointer, and the point of it that is there, in page units
      let best: { page: number; r: DOMRect } | null = null;
      let gap = Infinity;
      root.querySelectorAll<HTMLElement>(".pdf-sheet").forEach((sheet) => {
        const r = sheet.getBoundingClientRect();
        const d = at.y < r.top ? r.top - at.y : at.y > r.bottom ? at.y - r.bottom : 0;
        if (d < gap) {
          gap = d;
          best = { page: Number(sheet.parentElement?.dataset.page), r };
        }
      });
      const hit = best as { page: number; r: DOMRect } | null;
      pending.current = null;
      // the page's own zoom: its drawn width over its width at zoom 1
      const own = hit ? hit.r.width / (pagesNow.current?.[hit.page - 1]?.width ?? 1) || prev : prev;
      anchor.current = hit ? { page: hit.page, fx: (at.x - hit.r.left) / own, fy: (at.y - hit.r.top) / own, x: at.x, y: at.y } : null;
      zoomNow.current = target;
      if (fitToo) setMode("fit");
      else {
        setSet(target);
        setMode("set");
      }
      if (target === prev) setNudge((n) => n + 1);
    },
    [root],
  );

  // Ctrl or Cmd with the wheel, and a pinch on a trackpad (which arrives as the same), zoom around the pointer
  useEffect(() => {
    if (!root) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      zoomTo(wheelZoom(zoomNow.current, e.deltaY), { x: e.clientX, y: e.clientY });
    };
    root.addEventListener("wheel", onWheel, { passive: false });
    return () => root.removeEventListener("wheel", onWheel);
  }, [root, zoomTo]);

  const fitNow = () => zoomTo(fit, undefined, true);
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    (bind.onKeyDown as (e: React.KeyboardEvent<HTMLDivElement>) => void)(e);
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "+" || e.key === "=") zoomTo(stepZoom(zoomNow.current, 1));
    else if (e.key === "-" || e.key === "_") zoomTo(stepZoom(zoomNow.current, -1));
    else if (e.key === "0") fitNow();
    else return;
    e.preventDefault();
  };

  const percent = Math.round(zoom * 100);
  return (
    <div className="pdfview">
      <div className="pdf-bar" role="toolbar" aria-label="Pages and zoom">
        <div className="pdf-pager">
          <button
            type="button"
            className="pdf-step"
            aria-label="Previous page"
            disabled={!pages || current <= 1}
            onClick={() => goTo(stepPage(current, -1, total))}
          >
            <ChevronUp size={20} aria-hidden="true" />
          </button>
          <input
            className="pdf-goto num"
            type="text"
            inputMode="numeric"
            aria-label={`Page, of ${total}`}
            disabled={!pages}
            value={draft ?? String(current)}
            onFocus={(e) => e.currentTarget.select()}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={commitDraft}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                commitDraft();
                e.currentTarget.blur();
              } else if (e.key === "Escape") {
                setDraft(null);
                e.currentTarget.blur();
              }
            }}
          />
          <span className="pdf-of num" aria-hidden="true">
            / {total}
          </span>
          <button
            type="button"
            className="pdf-step"
            aria-label="Next page"
            disabled={!pages || current >= total}
            onClick={() => goTo(stepPage(current, 1, total))}
          >
            <ChevronDown size={20} aria-hidden="true" />
          </button>
        </div>
        <span className="pdf-rule" aria-hidden="true" />
        <button type="button" className="pdf-fit" aria-pressed={mode === "fit"} onClick={fitNow} disabled={!pages}>
          Fit width
        </button>
        <button
          type="button"
          className="pdf-step"
          aria-label="Zoom out"
          disabled={!pages || zoom <= ZOOM_MIN + 0.001}
          onClick={() => zoomTo(stepZoom(zoom, -1))}
        >
          <Minus size={20} aria-hidden="true" />
        </button>
        <output className="pdf-percent num" aria-live="polite" aria-label="Zoom">
          {percent}%
        </output>
        <button
          type="button"
          className="pdf-step"
          aria-label="Zoom in"
          disabled={!pages || zoom >= ZOOM_MAX - 0.001}
          onClick={() => zoomTo(stepZoom(zoom, 1))}
        >
          <Plus size={20} aria-hidden="true" />
        </button>
      </div>
      <div
        ref={setRoot}
        className="doc doc-pdf"
        role="region"
        aria-label="Source document, PDF pages. Plus and minus zoom, zero fits the width."
        tabIndex={0}
        {...bind}
        onKeyDown={onKeyDown}
      >
        {error && <p className="plain-error">{error}</p>}
        {!error && !ready && <p className="quiet pdf-wait">Opening the PDF.</p>}
        {ready && pages && (
          <div className="pdf-pages">
            {pages.map((info, i) => (
              <PdfPage
                key={i}
                info={info}
                number={i + 1}
                total={pages.length}
                zoom={zoomOf(i + 1)}
                marks={byPage.get(i + 1) ?? []}
                root={root}
                entry={entry}
              />
            ))}
          </div>
        )}
        {tip}
      </div>
    </div>
  );
}
