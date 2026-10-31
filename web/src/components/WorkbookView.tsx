import { useEffect, useMemo, useRef, useState } from "react";
import { columnLetter, titleRows, workbookMarks, type Sheet } from "../lib/sheetGrid";
import {
  borderCss,
  cellRef,
  columnIndex,
  effectiveBorders,
  formatValue,
  freezeCounts,
  mergeMap,
  sizePx,
  type SheetStyle,
} from "../lib/sheetStyle";
import type { Span } from "../lib/types";
import type { RowMark } from "../lib/sheetGrid";
import { markProps, useFactTip } from "./FactTip";
import { useCenterOnCite } from "./useCenterOnCite";

const NUMBER = /^[-+]?\d[\d,]*(\.\d+)?$/;

// the sizes of the sheet frame, px: the grey row numbers, the grey column letters, a row and a column
// that the file does not size
const ROW_NO = 48;
const HEAD_H = 24;
const ROW_H = 22;
const COL_W = 80;

/** One sheet with the styling the file has: its fills, fonts, alignment and borders, merged cells,
 * and the rows and columns of the frozen pane held in place while the rest scrolls. */
function StyledSheet({
  sheet,
  style,
  marks,
  active,
  entry,
}: {
  sheet: Sheet;
  style: SheetStyle;
  marks: Map<string, RowMark>;
  active: number;
  entry: string | undefined;
}) {
  const count = Math.max(1, ...sheet.rows.map((r) => r.length), ...Object.keys(style.columns).map((l) => columnIndex(l) + 1));
  const widths = Array.from({ length: count }, (_, c) => style.columns[columnLetter(c)] ?? COL_W);
  const heights = sheet.rows.map((_, r) => style.rows[String(r + 1)] ?? ROW_H);
  const { rows: frozenRows, cols: frozenCols } = freezeCounts(style.freeze);
  const { anchors, covered } = useMemo(() => mergeMap(style.merged), [style.merged]);
  // where each frozen row and column sticks: after the grey headers and the frozen ones before it
  const top = (r: number) => HEAD_H + heights.slice(0, r).reduce((a, b) => a + b, 0);
  const left = (c: number) => ROW_NO + widths.slice(0, c).reduce((a, b) => a + b, 0);
  const total = ROW_NO + widths.reduce((a, b) => a + b, 0);

  return (
    <table className="xl" style={{ width: total }}>
      <colgroup>
        <col style={{ width: ROW_NO }} />
        {widths.map((w, c) => (
          <col key={c} style={{ width: w }} />
        ))}
      </colgroup>
      <thead>
        <tr style={{ height: HEAD_H }}>
          <th className="xl-corner" scope="col">
            <span className="sr">Row</span>
          </th>
          {widths.map((_, c) => (
            <th key={c} scope="col" className="xl-col" style={c < frozenCols ? { left: left(c), zIndex: 4 } : undefined}>
              {columnLetter(c)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sheet.rows.map((cells, r) => {
          const key = `${active}:${r}`;
          const m = marks.get(key);
          const frozen = r < frozenRows;
          return (
            <tr
              key={r}
              className={[m ? (m.strong ? "strong" : "faint") : "", frozen ? "frozen" : ""].filter(Boolean).join(" ") || undefined}
              style={{ height: heights[r] }}
              data-cite={key === entry && m ? "" : undefined}
              {...(m ? markProps(m.facts, key === entry) : {})}
            >
              <th scope="row" className="xl-no" style={frozen ? { top: top(r), zIndex: 4 } : undefined}>
                {r + 1}
              </th>
              {widths.map((_, c) => {
                if (covered.has(`${r}:${c}`)) return null;
                const cs = style.cells[cellRef(r, c)] ?? {};
                const b = effectiveBorders(style.cells, r, c);
                const span = anchors.get(`${r}:${c}`);
                const sticky = frozen || c < frozenCols;
                return (
                  <td
                    key={c}
                    colSpan={span?.cols}
                    rowSpan={span?.rows}
                    className={sticky ? "xl-stick" : undefined}
                    style={{
                      backgroundColor: cs.fill,
                      color: cs.color,
                      fontWeight: cs.bold ? 700 : undefined,
                      fontStyle: cs.italic ? "italic" : undefined,
                      fontSize: cs.size ? sizePx(cs.size) : undefined,
                      textAlign: cs.align as React.CSSProperties["textAlign"],
                      borderTop: borderCss(b.top),
                      borderRight: borderCss(b.right),
                      borderBottom: borderCss(b.bottom),
                      borderLeft: borderCss(b.left),
                      top: frozen ? top(r) : undefined,
                      left: c < frozenCols ? left(c) : undefined,
                      zIndex: frozen && c < frozenCols ? 3 : sticky ? 2 : undefined,
                    }}
                  >
                    {formatValue(cells[c] ?? "", cs.format)}
                  </td>
                );
              })}
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** A workbook as a spreadsheet: column letters, row numbers, the title rows merged across the
 * width, sheet tabs at the bottom when there are several, the cited row marked. */
export function WorkbookView({
  sheets,
  styles,
  spans,
  quotes,
  fact,
}: {
  sheets: Sheet[];
  /** The look of each sheet by name; a sheet without one is shown as the plain grid. */
  styles?: Record<string, SheetStyle>;
  spans: Span[];
  quotes: string[];
  fact: string | null;
}) {
  const marks = useMemo(() => workbookMarks(sheets, spans, { quotes, fact }), [sheets, spans, quotes, fact]);
  const start = useMemo(() => {
    const at = [...marks].find(([, m]) => m.strong)?.[0] ?? [...marks.keys()][0];
    return at ? Number(at.split(":")[0]) : 0;
  }, [marks]);
  const [chosen, setChosen] = useState<number | null>(null);
  // another cited row (a fact picked elsewhere) brings its own sheet
  useEffect(() => setChosen(null), [start]);
  const active = Math.min(chosen ?? start, sheets.length - 1);
  const sheet = sheets[active];
  const root = useRef<HTMLDivElement>(null);
  const { bind, tip } = useFactTip();
  const here = [...marks].filter(([k]) => k.startsWith(`${active}:`));
  const entry = here.find(([, m]) => m.strong)?.[0] ?? here[0]?.[0];
  useCenterOnCite(root, `${active}:${sheets.length}:${entry}`);
  const titles = useMemo(() => new Set(sheet ? titleRows(sheet.rows) : []), [sheet]);
  if (!sheet) return <p className="plain-error">This workbook has no sheets.</p>;

  const style = styles?.[sheet.name];
  const width = Math.max(1, ...sheet.rows.map((r) => r.length));
  const header = sheet.rows.findIndex((r, i) => !titles.has(i) && r.some((c) => c.trim()));

  return (
    <div className="workbook">
      <div ref={root} className={style ? "doc doc-table doc-xl" : "doc doc-table"} role="region" aria-label={`Sheet ${sheet.name}`} tabIndex={0} {...bind}>
        {style ? (
          <StyledSheet sheet={sheet} style={style} marks={marks} active={active} entry={entry} />
        ) : (
          <table className="grid sheetgrid">
            <thead>
              <tr>
                <th className="no corner" scope="col">
                  <span className="sr">Row</span>
                </th>
                {Array.from({ length: width }, (_, c) => (
                  <th key={c} scope="col" className="col">
                    {columnLetter(c)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sheet.rows.map((cells, r) => {
                const m = marks.get(`${active}:${r}`);
                const key = `${active}:${r}`;
                const isTitle = titles.has(r);
                return (
                  <tr
                    key={r}
                    className={[m ? (m.strong ? "strong" : "faint") : "", r === header ? "head" : "", isTitle ? "title" : ""]
                      .filter(Boolean)
                      .join(" ") || undefined}
                    data-cite={key === entry && m ? "" : undefined}
                    {...(m ? markProps(m.facts, key === entry) : {})}
                  >
                    <th scope="row" className="no">
                      {r + 1}
                    </th>
                    {isTitle ? (
                      <td colSpan={width} className="merged">
                        {cells[0]}
                      </td>
                    ) : (
                      Array.from({ length: width }, (_, c) => {
                        const v = cells[c] ?? "";
                        return (
                          <td key={c} className={[NUMBER.test(v.trim()) ? "num" : "", v.length <= 22 ? "nw" : ""].filter(Boolean).join(" ") || undefined}>
                            {v}
                          </td>
                        );
                      })
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {tip}
      </div>
      {sheets.length > 1 && (
        <div className="sheet-switch" role="tablist" aria-label="Sheets of the workbook">
          {sheets.map((s, i) => (
            <button
              key={s.name}
              type="button"
              role="tab"
              aria-selected={i === active}
              className="sheet-switch-tab"
              onClick={() => setChosen(i)}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
