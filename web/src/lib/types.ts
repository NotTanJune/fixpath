import type { SheetStyle } from "./sheetStyle";
// The shapes of the API, from the real responses in data/recordings/api/.

export interface Citation {
  doc_path: string;
  page: number | null;
  chunk_id: string | null;
  quote: string;
}

export interface PathStep {
  src: string;
  rel: string;
  dst: string;
}

export interface Chip {
  field: string;
  value: string;
  label: string;
  p: number | null;
}

export type TriageStatus = "act" | "confirm" | "clarify" | "out_of_scope";

export interface Triage {
  asset_id: string | null;
  alarm_code: string | null;
  failure_mode_id: string | null;
  intent: string;
  safety_critical: number | null;
  confidence: Record<string, number | null>;
  status: TriageStatus;
  chips: Chip[];
  reason: string | null;
}

export interface Cause {
  asset_id: string;
  failure_mode_id: string;
  name: string;
  likelihood: number;
  support: number;
  of_total: number;
  citations: Citation[];
  path: PathStep[];
}

export interface Fix {
  procedure_id: string;
  title: string;
  steps: string[];
  requires_loto: boolean;
  est_minutes: number;
  manual_ref: string | null;
  citations: Citation[];
  path: PathStep[];
}

export interface PartNeed {
  part_no: string;
  description: string;
  qty: number;
  qty_on_hand: number | null;
  bin: string | null;
  lead_time_days: number | null;
  stock_as_of: string | null;
  enough: boolean;
  citations: Citation[];
  path: PathStep[];
}

export interface PartStock {
  part_no: string;
  description: string;
  qty_on_hand: number | null;
  bin: string | null;
  lead_time_days: number | null;
  stock_as_of: string | null;
  fits: string[];
  citations: Citation[];
}

export interface Breakdown {
  asset_id: string;
  incident_id: string;
  date: string;
  shift: string | null;
  downtime_min: number | null;
  technician: string | null;
  symptom_text: string;
  resolution_text: string | null;
  root_cause_id: string | null;
  parts_used: Record<string, number>;
  citations: Citation[];
  path: PathStep[];
}

export interface AlarmHistory {
  asset_id: string;
  code: string;
  count: number;
  first_seen: string | null;
  last_seen: string | null;
  incidents: number;
  citations: Citation[];
}

export interface SafetyBlock {
  required: boolean;
  reason: string | null;
  procedure_ids: string[];
  text: string;
  citations: Citation[];
}

export interface Claim {
  id: string;
  text: string;
  citation_ids: string[];
  verdict: "supported" | "partly" | "unsupported" | null;
  p: number | null;
  /** P(supported) + P(partly): the verifier keeps a claim when this reaches 0.5. */
  support?: number | null;
  reason: string | null;
}

export interface NodeTrace {
  name: string;
  started_at: string;
  ended_at: string;
  seconds: number;
  backend: string | null;
  cache_hit: boolean | null;
  cost_usd: number;
  notes: string[];
}

export interface AnswerCard {
  question: string;
  triage: Triage;
  summary: string | null;
  causes: Cause[];
  fix: Fix | null;
  other_fixes: Fix[];
  parts: PartNeed[];
  breakdowns: Breakdown[];
  history: AlarmHistory | null;
  stock: PartStock[];
  safety: SafetyBlock;
  claims: Claim[];
  unverified: Claim[];
  citations: Record<string, Citation>;
  path: PathStep[];
  notes: string[];
  trace: NodeTrace[];
}

// The events of POST /api/ask.

export interface NodeEnd {
  node: string;
  seconds: number;
  backend: string | null;
  cache_hit: boolean | null;
  cost_usd: number;
  notes: string[];
  status?: TriageStatus;
  decisions?: {
    machine: string | null;
    alarm: string | null;
    root_cause: string | null;
    intent: string;
    safety_critical: number | null;
  };
  confidence?: Record<string, number | null>;
}

export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  filled_by: "agent" | "code";
  result_count: number;
}

export interface Health {
  keys: { openai: boolean; typesafe: boolean; langfuse: boolean };
  decider: "jev" | "decisions" | "cache" | "unavailable";
  replay_mode: boolean;
  ingest_enabled: boolean;
  budget: { spent_usd: number; limit_usd: number };
}

export interface Suggestion {
  question: string;
  replayable: boolean;
}

export interface Ontology {
  families: Record<string, { label: string; description: string }>;
  node_types: Record<string, { label: string; glyph: string; family: string; id_pattern: string }>;
  relations?: Record<string, { label: string; from: string; to: string }>;
}

export interface SourceSummary {
  id: string;
  path: string;
  doc_type: string;
  bytes: number;
  /** When the file was added by hand (ISO 8601); only files that came in through the drop zone have it. */
  added?: string;
}

export interface Span {
  quote: string;
  start: number;
  end: number;
  page: number | null;
  fact_kind: "node" | "edge";
  fact_id: string;
  type: string;
  label: string;
  extractor: string;
  confidence: number | null;
}

export interface SourceDoc extends SourceSummary {
  kind: string;
  text: string;
  spans: Span[];
  pages?: { page: number; text: string; start: number }[];
  sheets?: { name: string; rows: string[][] }[];
  /** The look of a workbook's sheets, keyed by sheet name; only workbooks have it. */
  styles?: Record<string, SheetStyle>;
}

export interface GraphElements {
  nodes: { data: { id: string; label: string; type: string; family: string; glyph: string } }[];
  edges: { data: { id: string; source: string; target: string; rel: string; label: string } }[];
}

/** One past breakdown as `/api/activity` lists it; a value the graph lacks is null. */
export interface BreakdownRow {
  id: string;
  date: string | null;
  shift: string | null;
  downtime_min: number | null;
  machine: string | null;
  machine_name: string | null;
  alarm: string | null;
  alarm_message: string | null;
  cause_id: string | null;
  cause_name: string | null;
  symptom: string | null;
  doc_id: string | null;
}

/** One machine and alarm pair, with when the alarm was last seen. */
export interface AlarmRow {
  machine: string;
  machine_name: string | null;
  alarm: string;
  alarm_message: string | null;
  severity: string | null;
  last_seen: string | null;
  count: number | null;
  doc_id: string | null;
}

export interface Activity {
  breakdowns: BreakdownRow[];
  alarms: AlarmRow[];
}

export interface UpkeepBreakdown {
  id: string;
  date: string;
  downtime_min: number | null;
  citations: Citation[];
}

export type UpkeepStatus = "overdue" | "due_soon" | "later" | "not_enough_history";

export interface UpkeepItem {
  machine: string;
  machine_label: string;
  cause: string;
  cause_label: string;
  episodes: number;
  last: string;
  status: UpkeepStatus;
  gap_days: number | null;
  due: string | null;
  breakdowns: UpkeepBreakdown[];
}

export interface UpkeepTerm {
  machine: string;
  response_hours: number;
  uptime_target: number;
  covers: string;
  measured_uptime: number;
  downtime_min: number;
}

export interface Upkeep {
  as_of: string | null;
  items: UpkeepItem[];
  terms: UpkeepTerm[];
}
