import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

let loaded: Promise<typeof import("pdfjs-dist")> | null = null;

/** The PDF library, loaded the first time a PDF is opened: it is a chunk of its own, and so is
 * its worker, so the app's main bundle does not carry either. */
export function loadPdfjs(): Promise<typeof import("pdfjs-dist")> {
  loaded ??= import("pdfjs-dist").then((lib) => {
    lib.GlobalWorkerOptions.workerSrc = workerUrl;
    return lib;
  });
  return loaded;
}
