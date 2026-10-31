import { lazy, Suspense, useMemo } from "react";
import { markRanges } from "../lib/sourceMarks";
import { viewerKind } from "../lib/viewerKind";
import type { SourceDoc } from "../lib/types";
import { CsvView } from "./CsvView";
import { LogView, NoteView } from "./TextViews";
import { WorkbookView } from "./WorkbookView";

// the PDF viewer and the PDF library behind it are a chunk of their own, loaded on the first PDF
const PdfView = lazy(() => import("./PdfView"));

/** What the reader came for: the quotes cited from this file (the first is the one to scroll
 * to), the ID of the fact that was opened, and the page of a cited manual. */
export interface SourceFocus {
  quotes: string[];
  fact: string | null;
  page: number | null;
  /** The exact stretch of the text to mark strong, when a reader picked one span of the file. */
  range?: { start: number; end: number } | null;
}

/** One source shown as the document it is. It has no chrome of its own, so the source sheet
 * and the Pipeline view can both hold it. */
export function SourceView({ doc, focus }: { doc: SourceDoc; focus: SourceFocus }) {
  const kind = viewerKind(doc);
  const ranges = useMemo(() => markRanges(doc, focus), [doc, focus]);
  switch (kind) {
    case "pdf":
      return (
        <Suspense fallback={<p className="quiet">Opening the PDF.</p>}>
          <PdfView doc={doc} focus={focus} />
        </Suspense>
      );
    case "workbook":
      if (doc.sheets?.length) {
        return <WorkbookView sheets={doc.sheets} styles={doc.styles} spans={doc.spans} quotes={focus.quotes} fact={focus.fact} />;
      }
      return <NoteView text={doc.text} ranges={ranges} />;
    case "csv":
      return <CsvView text={doc.text} ranges={ranges} quotes={focus.quotes} />;
    case "log":
      return <LogView text={doc.text} ranges={ranges} />;
    default:
      return <NoteView text={doc.text} ranges={ranges} />;
  }
}
