import {
  Archive,
  Bolt,
  Clock,
  Cog,
  Factory,
  FileText,
  History,
  Package,
  Siren,
  Unlink,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { createContext, useContext } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { Ontology } from "./types";

/** The Lucide glyph for each glyph name of config/ontology.json. */
export const GLYPHS: Record<string, LucideIcon> = {
  factory: Factory,
  cog: Cog,
  bolt: Bolt,
  siren: Siren,
  unlink: Unlink,
  wrench: Wrench,
  package: Package,
  history: History,
  clock: Clock,
  "file-text": FileText,
  archive: Archive,
};

/** The magnet colours: one flat fill per family, and red for an alarm. */
export type Tone = "sl" | "am" | "rd" | "gr" | "vi" | "so";

export const TONES: Record<Tone, { fill: string; text: string }> = {
  sl: { fill: "#3E5C76", text: "#FFFFFF" },
  am: { fill: "#D9921A", text: "#1D2023" },
  rd: { fill: "#C2362B", text: "#FFFFFF" },
  gr: { fill: "#2D8653", text: "#FFFFFF" },
  vi: { fill: "#6A4C9C", text: "#FFFFFF" },
  so: { fill: "#8A8F96", text: "#1D2023" },
};

const FAMILY_TONE: Record<string, Tone> = {
  where: "sl",
  problem: "am",
  fix: "gr",
  history: "vi",
  source: "so",
};

/** The tone of a node type. The family decides, except that an alarm is red. */
export function toneOf(type: string, ontology: Ontology | null): Tone {
  if (type === "AlarmCode") return "rd";
  const family = ontology?.node_types[type]?.family ?? FALLBACK_FAMILY[type] ?? "source";
  return FAMILY_TONE[family] ?? "so";
}

const FALLBACK_FAMILY: Record<string, string> = {
  Line: "where",
  Asset: "where",
  Component: "where",
  AlarmCode: "problem",
  FailureMode: "problem",
  Procedure: "fix",
  Part: "fix",
  Bin: "fix",
  Incident: "history",
  Document: "source",
};

const FALLBACK_GLYPH: Record<string, string> = {
  Line: "factory",
  Asset: "cog",
  Component: "bolt",
  AlarmCode: "siren",
  FailureMode: "unlink",
  Procedure: "wrench",
  Part: "package",
  Bin: "archive",
  Incident: "history",
  Document: "file-text",
};

export function glyphOf(type: string, ontology: Ontology | null): string {
  return ontology?.node_types[type]?.glyph ?? FALLBACK_GLYPH[type] ?? "file-text";
}

// The plain words of each node type and relation, for the moments before config/ontology.json has
// loaded and for the ones the answer graph draws that the ontology does not have: the bin a part is
// kept in, and the page a fact is cited in.
const FALLBACK_LABEL: Record<string, string> = {
  Line: "Production line",
  Asset: "Machine",
  Component: "Component",
  AlarmCode: "Alarm",
  FailureMode: "Root cause",
  Procedure: "Fix",
  Part: "Spare part",
  Bin: "Bin",
  Incident: "Past breakdown",
  Document: "Source",
};

const FALLBACK_RELATION: Record<string, string> = {
  HAS_MACHINE: "has machine",
  HAS_COMPONENT: "is built from",
  RAISED: "raised",
  INDICATES: "usually means",
  AFFECTS: "damages",
  RESOLVED_BY: "fixed by",
  REQUIRES: "needs",
  FITS: "fits",
  OCCURRED_ON: "happened on",
  TRIGGERED_BY: "started with",
  ROOT_CAUSE: "was caused by",
  USED: "used",
  STORED_IN: "kept in",
  CITED_IN: "cited in",
};

export function labelOf(type: string, ontology: Ontology | null): string {
  return ontology?.node_types[type]?.label ?? FALLBACK_LABEL[type] ?? type;
}

/** The plain words of a relation: "usually means", "fixed by". */
export function relationLabel(rel: string, ontology: Ontology | null): string {
  return ontology?.relations?.[rel]?.label ?? FALLBACK_RELATION[rel] ?? rel.toLowerCase().replace(/_/g, " ");
}

export const OntologyContext = createContext<Ontology | null>(null);
export const useOntology = () => useContext(OntologyContext);

export function Glyph({
  name,
  size = 14,
  ...rest
}: { name: string; size?: number } & Record<string, unknown>) {
  const Icon = GLYPHS[name] ?? FileText;
  return <Icon size={size} strokeWidth={2.25} aria-hidden="true" focusable="false" {...rest} />;
}

const uris = new Map<string, string>();

/** A glyph as a data URI, for the background of a graph node. */
export function glyphUri(name: string, color: string): string {
  const key = `${name}|${color}`;
  let uri = uris.get(key);
  if (!uri) {
    const Icon = GLYPHS[name] ?? FileText;
    const svg = renderToStaticMarkup(<Icon size={24} strokeWidth={2.25} color={color} />);
    uri = `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
    uris.set(key, uri);
  }
  return uri;
}
