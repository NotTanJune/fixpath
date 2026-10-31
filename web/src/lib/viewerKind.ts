export type ViewerKind = "pdf" | "workbook" | "csv" | "log" | "note";

/** Which viewer shows a source: the kind the API gives decides first, then the file extension,
 * then the document type. A handover or a loose note is a note. */
export function viewerKind(doc: { path: string; kind?: string; doc_type?: string }): ViewerKind {
  const ext = doc.path.includes(".") ? (doc.path.split(".").pop() ?? "").toLowerCase() : "";
  if (doc.kind === "pdf" || ext === "pdf") return "pdf";
  if (doc.kind === "workbook" || ext === "xlsx" || ext === "xls") return "workbook";
  if (ext === "csv") return "csv";
  if (ext === "log" || doc.doc_type === "log") return "log";
  return "note";
}
