import { ChevronRight, FileUp } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { getRecorded } from "../lib/api";
import type { RecordedRow } from "../lib/playback";

/** The drop zone for adding a file: one file at a time, which the view beside it then reads stage
 * by stage. On a server with no keys it also offers the recorded ingests of the test set to watch. */
export function DropZone({
  busy,
  replay,
  onFile,
  onWatch,
}: {
  /** A file is being read. */
  busy: boolean;
  /** The server has no keys: it cannot read a new file, but it can play a recording. */
  replay: boolean;
  onFile: (file: File) => void;
  onWatch: (row: RecordedRow) => void;
}) {
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<RecordedRow[] | null>(null);
  const [failed, setFailed] = useState(false);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const file = e.dataTransfer.files[0];
    if (file && !busy) onFile(file);
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !rows) {
      setFailed(false);
      getRecorded()
        .then(setRows)
        .catch(() => setFailed(true));
    }
  };

  return (
    <section className="drop-wrap" aria-label="Add a file">
      <div
        className={`drop${over ? " over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={onDrop}
      >
        <button
          type="button"
          className="drop-btn"
          disabled={busy}
          title="Any text file, a PDF or a workbook, up to 1 MB"
          onClick={() => input.current?.click()}
        >
          <FileUp size={18} aria-hidden="true" />
          {busy ? "Reading the file" : "Add a file"}
        </button>
        <input
          ref={input}
          type="file"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onFile(file);
            e.target.value = "";
          }}
        />
        <p className="quiet">Drop it here or choose one, and it joins the graph.</p>
      </div>
      {replay && (
        <div className="watch">
          <button type="button" className="watch-btn" aria-expanded={open} aria-controls="watch-list" onClick={toggle}>
            <ChevronRight className="chev" size={18} aria-hidden="true" />
            Watch a recorded ingest
          </button>
          {open && (
            <div id="watch-list">
              {failed && <p className="plain-error">The recordings could not be loaded.</p>}
              {!failed && !rows && <p className="quiet">Reading the list.</p>}
              {rows && (
                <ul className="watch-list">
                  {rows.map((row) => (
                    <li key={row.number}>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => {
                          setOpen(false);
                          onWatch(row);
                        }}
                      >
                        {row.file}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
