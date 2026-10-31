import { useMemo, useRef } from "react";
import { columnLetter } from "../lib/sheetGrid";
import { numericColumns, parseCsv, rowByCells } from "../lib/csv";
import { lineMarks, type MarkRange } from "../lib/sourceMarks";
import { markProps, useFactTip } from "./FactTip";
import { useCenterOnCite } from "./useCenterOnCite";

// the frame of the sheet, px: the grey row numbers and a column (a csv file has no widths)
const ROW_NO = 48;
const COL_MIN = 64;
const COL_MAX = 300;

/** The width of a column, px: as wide as its longest cell up to a limit; a longer cell wraps. */
function columnWidth(longest: number): number {
  return Math.min(COL_MAX, Math.max(COL_MIN, Math.round(longest * 7.4 + 22)));
}

/** A csv file as a table: title lines as a caption, a header row that stays in view, row
 * numbers, numbers aligned, and the cited row marked. */
export function CsvView({
  text,
  ranges,
  quotes,
}: {
  text: string;
  ranges: MarkRange[];
  quotes: string[];
}) {
  const root = useRef<HTMLDivElement>(null);
  const { bind, tip } = useFactTip();
  const table = useMemo(() => parseCsv(text), [text]);
  const numeric = useMemo(() => numericColumns(table), [table]);

  // a quote that is not the raw line is found by its cells
  const marked = useMemo(() => {
    if (ranges.some((r) => r.strong)) return ranges;
    for (const quote of quotes) {
      const row = quote.trim() ? rowByCells(table, quote) : null;
      if (row) {
        return [...ranges.filter((r) => r.end <= row.start || r.start >= row.end), { start: row.start, end: row.end, strong: true, facts: [] }].sort(
          (a, b) => a.start - b.start,
        );
      }
    }
    return ranges;
  }, [ranges, quotes, table]);

  const marks = useMemo(() => lineMarks(table.rows, marked), [table, marked]);
  const strongAt = marks.findIndex((m) => m?.strong);
  const entry = strongAt !== -1 ? strongAt : marks.findIndex((m) => m !== null);
  useCenterOnCite(root, `${text.length}:${entry}`);
  const width = Math.max(table.header.length, ...table.rows.map((r) => r.cells.length));

  // a spreadsheet numbers its rows by line: the title lines first, then the header, then each row
  const head = table.caption.length + 1;
  const widths = Array.from({ length: width }, (_, c) =>
    columnWidth(Math.max(table.header[c]?.length ?? 0, ...table.rows.map((r) => Math.max(...(r.cells[c] ?? "").split("\n").map((l) => l.length))))),
  );
  const lineOf = (offset: number) => text.slice(0, offset).split("\n").length;

  return (
    <div ref={root} className="doc doc-table doc-csv" role="region" aria-label="Source table" tabIndex={0} {...bind}>
      <table className="xl xl-csv" style={{ width: ROW_NO + widths.reduce((a, b) => a + b, 0) }}>
        <colgroup>
          <col style={{ width: ROW_NO }} />
          {widths.map((w, c) => (
            <col key={c} style={{ width: w }} />
          ))}
        </colgroup>
        <thead>
          <tr style={{ height: 24 }}>
            <th scope="col" className="xl-corner">
              <span className="sr">Row</span>
            </th>
            {Array.from({ length: width }, (_, c) => (
              <th key={c} scope="col" className="xl-col">
                {columnLetter(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.caption.map((line, i) => (
            <tr key={`c${i}`} style={{ height: 30 }}>
              <th scope="row" className="xl-no">
                {i + 1}
              </th>
              <td colSpan={width} className="xl-title">
                {line}
              </td>
            </tr>
          ))}
          <tr className="xl-head" style={{ height: 24 }}>
            <th scope="row" className="xl-no">
              {head}
            </th>
            {Array.from({ length: width }, (_, c) => (
              <td key={c} className={numeric[c] ? "num" : undefined}>
                {table.header[c] ?? ""}
              </td>
            ))}
          </tr>
          {table.rows.map((row, i) => {
            const m = marks[i];
            return (
              <tr
                key={row.no}
                className={m ? (m.strong ? "strong" : "faint") : undefined}
                style={{ height: 24 }}
                data-cite={i === entry && m ? "" : undefined}
                {...(m ? markProps(m.facts, i === entry) : {})}
              >
                <th scope="row" className="xl-no">
                  {lineOf(row.start)}
                </th>
                {Array.from({ length: width }, (_, c) => (
                  <td key={c} className={numeric[c] ? "num" : undefined}>
                    {row.cells[c] ?? ""}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      {tip}
    </div>
  );
}
