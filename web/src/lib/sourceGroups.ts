// The groups of the source list on the Pipeline view, apart from the component so that a test can
// read them: which file is in which group, what each group is called, and which are open.

import type { SourceSummary } from "./types";

type Item = Pick<SourceSummary, "id" | "doc_type" | "path"> & { added?: string };

export interface SourceGroup<T extends Item = SourceSummary> {
  key: string;
  label: string;
  items: T[];
}

/** The groups, in the order of the pipeline, and what each is called. */
const GROUPS: [string, string][] = [
  ["manuals", "Machine manuals"],
  ["logs", "Machine logs"],
  ["handovers", "Shift handovers"],
  ["notes", "Maintenance notes"],
  ["export", "Maintenance export"],
  ["stock", "Stock lists"],
];
const OTHER: [string, string] = ["other", "Other files"];

/** The group of the files added by hand, which the server gives a time. */
export const ADDED = "added";
const ADDED_LABEL = "Newly added";

/** What each known group calls one of its files. */
const CATEGORY: Record<string, string> = {
  manuals: "Machine manual",
  logs: "Machine log",
  handovers: "Shift handover",
  notes: "Maintenance note",
  export: "Maintenance export",
  stock: "Stock list",
  other: "Other file",
};

const TABLE = /\.(csv|xlsx?)$/i;
const TYPE_KEY = "type-";

/** A document type in words: "work_order" is "work order". */
const typeWords = (type: string) => type.replace(/_/g, " ").trim();
const upFirst = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** The plural of the last word of a type: procedure, procedures; inquiry, inquiries; box, boxes. */
function plural(text: string): string {
  if (/[^aeiou]y$/.test(text)) return `${text.slice(0, -1)}ies`;
  if (/(s|x|z|ch|sh)$/.test(text)) return `${text}es`;
  return `${text}s`;
}

/** The group of a file. The maintenance export has the same document type as the notes, so the
 * file decides: a table is the export, a text is a note. A type the app does not know has a group
 * of its own. */
export function groupKeyOf(s: Item): string {
  switch (s.doc_type) {
    case "manual":
      return "manuals";
    case "log":
      return "logs";
    case "handover":
      return "handovers";
    case "maintenance_note":
      return TABLE.test(s.path) ? "export" : "notes";
    case "inventory":
      return "stock";
    default:
      return s.doc_type ? `${TYPE_KEY}${s.doc_type}` : OTHER[0];
  }
}

/** The category of a file, as one name: "Shift handover", or the type for one that is not known. */
export function categoryOf(s: Item): string {
  const key = groupKeyOf(s);
  if (key.startsWith(TYPE_KEY)) return upFirst(typeWords(s.doc_type));
  return CATEGORY[key] ?? "";
}

/** The files in their groups. A group with no file is left out. Newly added files come first,
 * newest first, and are in their category too; a type that is not known follows the known ones, in
 * the order the files give it. */
export function groupSources<T extends Item>(sources: T[]): SourceGroup<T>[] {
  const known = GROUPS.flatMap(([key, label]) => {
    const items = sources.filter((s) => groupKeyOf(s) === key);
    return items.length ? [{ key, label, items }] : [];
  });
  const extra: SourceGroup<T>[] = [];
  for (const s of sources) {
    const key = groupKeyOf(s);
    if (!key.startsWith(TYPE_KEY) || extra.some((g) => g.key === key)) continue;
    const label = upFirst(plural(typeWords(s.doc_type)));
    extra.push({ key, label, items: sources.filter((x) => groupKeyOf(x) === key) });
  }
  const rest = sources.filter((s) => groupKeyOf(s) === OTHER[0]);
  const tail = rest.length ? [{ key: OTHER[0], label: OTHER[1], items: rest }] : [];
  const fresh = sources
    .filter((s) => s.added)
    .sort((a, b) => Date.parse(b.added!) - Date.parse(a.added!));
  const head = fresh.length ? [{ key: ADDED, label: ADDED_LABEL, items: fresh }] : [];
  return [...head, ...known, ...extra, ...tail];
}

/** The keys of the groups that are open when a file has just been selected: its own group, and
 * for a newly added file Newly added too; none when there is no such file. */
export function initialOpen(groups: SourceGroup<Item>[], selected: string | undefined): string[] {
  return groups.filter((g) => g.items.some((s) => s.id === selected)).map((g) => g.key);
}

/** What the reader has opened and shut for one selected file. It holds only for that file: a
 * different selection starts again from the group of the new file. */
export interface OpenState {
  at: string | undefined;
  keys: string[];
}

/** The open groups for the file that is selected now. */
export function openKeys(state: OpenState, groups: SourceGroup<Item>[], selected: string | undefined): string[] {
  return state.at === selected ? state.keys : initialOpen(groups, selected);
}

/** The state after the reader opens or shuts a group. */
export function toggleGroup(
  state: OpenState,
  groups: SourceGroup<Item>[],
  selected: string | undefined,
  key: string,
): OpenState {
  const now = openKeys(state, groups, selected);
  const keys = now.includes(key) ? now.filter((k) => k !== key) : [...now, key];
  return { at: selected, keys };
}
