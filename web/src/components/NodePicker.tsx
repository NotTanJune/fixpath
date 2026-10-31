import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { PickerGroup } from "../lib/explorerFocus";

/** A searchable list of the nodes of the graph, grouped by the type of node, to pick the one the
 * explorer centres on. A field that filters as it is typed: the arrow keys move through the
 * options, Enter picks, Escape shuts the list. */
export function NodePicker({
  groups,
  valueText,
  labelledBy,
  onQuery,
  onPick,
}: {
  groups: PickerGroup[];
  valueText: string;
  labelledBy: string;
  onQuery: (query: string) => void;
  onPick: (id: string) => void;
}) {
  const list = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [at, setAt] = useState(0);
  const box = useRef<HTMLUListElement>(null);
  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);

  const shut = () => {
    setOpen(false);
    setQuery("");
    onQuery("");
  };
  const choose = (id: string) => {
    shut();
    onPick(id);
  };

  // the option in use is in view
  useEffect(() => {
    if (!open) return;
    box.current?.querySelector<HTMLElement>("[aria-selected='true']")?.scrollIntoView({ block: "nearest" });
  }, [open, at]);

  const move = (to: number) => setAt(Math.max(0, Math.min(flat.length - 1, to)));

  return (
    <div className="picker">
      <input
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={list}
        aria-autocomplete="list"
        aria-labelledby={labelledBy}
        aria-activedescendant={open && flat[at] ? `${list}-${at}` : undefined}
        autoComplete="off"
        spellCheck={false}
        value={open ? query : valueText}
        placeholder="Search by ID, name or type"
        onFocus={(e) => {
          setOpen(true);
          e.currentTarget.select();
        }}
        onClick={() => setOpen(true)}
        onBlur={shut}
        onChange={(e) => {
          setQuery(e.target.value);
          onQuery(e.target.value);
          setOpen(true);
          setAt(0);
        }}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            if (!open) setOpen(true);
            else move(at + 1);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            move(at - 1);
          } else if (e.key === "Enter") {
            if (open && flat[at]) {
              e.preventDefault();
              choose(flat[at]!.id);
            }
          } else if (e.key === "Escape" && open) {
            e.preventDefault();
            e.stopPropagation();
            shut();
          }
        }}
      />
      {open && (
        <ul ref={box} id={list} role="listbox" aria-label="Nodes of the graph" className="picker-list">
          {flat.length === 0 && (
            <li role="presentation" className="picker-none">
              Nothing in the graph matches
            </li>
          )}
          {groups.map((g) => (
            <li key={g.type} role="presentation">
              <ul role="group" aria-label={g.heading}>
                <li role="presentation" className="picker-head" aria-hidden="true">
                  {g.heading}
                </li>
                {g.items.map((item) => {
                  const i = flat.indexOf(item);
                  return (
                    <li
                      key={item.id}
                      id={`${list}-${i}`}
                      role="option"
                      aria-selected={i === at}
                      className="picker-opt"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => choose(item.id)}
                      onMouseMove={() => i !== at && setAt(i)}
                    >
                      {item.text}
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
