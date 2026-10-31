import { ExternalLink, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { sourceDoc, sourceIdOfPath } from "../lib/api";
import { docIdOfChunk } from "../lib/format";
import { sheetBox, type Box, type Frame } from "../lib/sheetFrame";
import { citedFactLine, documentName } from "../lib/sourceName";
import type { Citation, SourceDoc } from "../lib/types";
import { findQuote } from "../lib/quote";
import { hrefFor } from "../lib/router";
import { SourceView, type SourceFocus } from "./SourceView";

export interface SheetRequest {
  label: string; // the ID that was opened
  citations: Citation[];
}

async function loadDoc(citation: Citation): Promise<SourceDoc> {
  const id = docIdOfChunk(citation.chunk_id) ?? (await sourceIdOfPath(citation.doc_path));
  if (!id) throw new Error(`There is no source file for ${citation.doc_path}.`);
  return sourceDoc(id);
}

function pageOf(doc: SourceDoc, at: number): number | null {
  const pages = doc.pages ?? [];
  let page: number | null = null;
  for (const p of pages) if (p.start <= at) page = p.page;
  return page;
}

function boxOf(selector: string): Box | null {
  const el = document.querySelector(selector);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
}

/** Where the sheet sits now: over the graph pane when the page has one, else over the content
 * area. It is measured, because the reader drags the separator and the pane follows. */
function measureFrame(): Frame {
  const bar = Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--bar")) || 48;
  let content = boxOf(".ask-main") ?? boxOf("main.view");
  const drawer = boxOf(".rail.drawer"); // below 1280 px the open rail lies over the content
  if (content && drawer) content = { ...content, left: Math.max(content.left, drawer.right) };
  return sheetBox(boxOf(".side"), content, { width: window.innerWidth, height: window.innerHeight }, bar);
}

const FOCUSABLE = "a[href], button:not([disabled]), [tabindex]:not([tabindex='-1'])";

/** Keeps Tab and Shift+Tab inside the sheet: the last control wraps to the first and back. */
export function trapTab(e: KeyboardEvent, root: HTMLElement) {
  const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
  const first = items[0];
  const last = items[items.length - 1];
  if (!first || !last) return;
  const active = document.activeElement;
  if (!root.contains(active)) {
    e.preventDefault();
    first.focus();
  } else if (e.shiftKey && active === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && active === last) {
    e.preventDefault();
    first.focus();
  }
}

/** The side sheet: the source document of an ID, with the quoted span highlighted. */
export function SourceSheet({
  request,
  onClose,
}: {
  request: SheetRequest | null;
  onClose: () => void;
}) {
  const [index, setIndex] = useState(0);
  const [doc, setDoc] = useState<SourceDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(null);
  const sheet = useRef<HTMLElement>(null);
  const [frame, setFrame] = useState<Frame | null>(null);
  const shown = request !== null;

  // measured before the first paint, and again when the pane or the window changes size
  useLayoutEffect(() => {
    if (!shown) return;
    const place = () => setFrame(measureFrame());
    place();
    const observer = new ResizeObserver(place);
    for (const sel of [".side", ".ask-main", "main.view"]) {
      const el = document.querySelector(sel);
      if (el) observer.observe(el);
    }
    window.addEventListener("resize", place);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", place);
      setFrame(null);
    };
  }, [shown]);

  useEffect(() => {
    setIndex(0);
  }, [request]);

  useEffect(() => {
    if (!request) return;
    opener.current = document.activeElement;
    closeButton.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "Tab" && sheet.current) trapTab(e, sheet.current);
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
  }, [request, onClose]);

  const citation = request?.citations[index];
  useEffect(() => {
    setDoc(null);
    setError(null);
    if (!citation) return;
    let live = true;
    loadDoc(citation)
      .then((d) => live && setDoc(d))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [citation]);

  const focus = useMemo<SourceFocus>(() => {
    const quotes: string[] = [];
    if (doc && request) {
      // the quote of the open tab leads, then the other quotes cited from the same file
      const lead = request.citations[index];
      for (const c of [lead, ...request.citations]) {
        if (c && c.doc_path === doc.path && c.quote.trim() && !quotes.includes(c.quote)) quotes.push(c.quote);
      }
    }
    return { quotes, fact: request?.label ?? null, page: request?.citations[index]?.page ?? null };
  }, [doc, request, index]);

  if (!request) return null;
  const quoteAt = doc && focus.quotes[0] ? findQuote(doc.text, focus.quotes[0])?.[0] : undefined;
  // the page the citation names; a quote can repeat on several pages, so the text decides only without it
  const page = citation?.page ?? (doc && quoteAt !== undefined ? pageOf(doc, quoteAt) : null);
  const name = documentName(citation?.doc_path ?? "", doc?.doc_type);
  const fact = citedFactLine(request.label, doc?.spans ?? [], citation?.quote ?? "");

  return (
    <aside
      ref={sheet}
      className="sheet"
      role="complementary"
      aria-label={`Source of ${request.label}`}
      style={frame ? { left: frame.left, top: frame.top, width: frame.width, height: frame.height } : undefined}
    >
      <header className="sheet-head">
        <h2>
          {name}
          {page ? `, page ${page}` : ""}
        </h2>
        <button
          ref={closeButton}
          className="icon-btn"
          onClick={onClose}
          aria-label="Close the source"
        >
          <X size={20} aria-hidden="true" />
        </button>
        <p className="sheet-fact" title={fact}>
          {fact}
        </p>
        {doc && (
          <a
            className="sheet-original link"
            href={`/api/sources/${encodeURIComponent(doc.id)}/file`}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open the original file
            <ExternalLink size={18} aria-hidden="true" />
          </a>
        )}
      </header>
      {request.citations.length > 1 && (
        <div className="sheet-tabs" role="tablist" aria-label="Sources">
          {request.citations.map((c, i) => (
            <button
              key={`${c.doc_path}-${i}`}
              role="tab"
              aria-selected={i === index}
              className="sheet-tab"
              onClick={() => setIndex(i)}
            >
              {documentName(c.doc_path)}
              {c.page ? `, page ${c.page}` : ""}
            </button>
          ))}
        </div>
      )}
      <div className="sheet-body">
        {error && <p className="plain-error">{error}</p>}
        {!error && !doc && <p className="quiet">Opening the source.</p>}
        {doc && <SourceView doc={doc} focus={focus} />}
      </div>
      {doc && (
        <footer className="sheet-foot">
          <a className="link" href={hrefFor("ingestion", doc.id)} onClick={onClose}>
            <span className="id">{doc.id}</span> in Ingestion
          </a>
        </footer>
      )}
    </aside>
  );
}
