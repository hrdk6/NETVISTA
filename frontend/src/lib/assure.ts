// Assure: intents, failure analysis, plans, autopilot and evidence (backend/netvista/assure).
import { isRouter } from "./format";
import type { ValidationRow, ValidationRun } from "./types";

export type IntentKind = "reach" | "latency" | "loss" | "bandwidth" | "avoid" | "waypoint" | "max_util" | "disjoint";
export type IntentStatus = "ok" | "violated" | "at_risk" | "unknown" | "disabled";
export type Protect = "none" | "link" | "node" | "any";
export type Priority = "critical" | "high" | "normal";

export interface IntentCheck {
  subject: string;
  ok: boolean | null;
  value: number | string | null;
  limit: number | string | null;
  unit: string;
  detail: string;
  at_risk: boolean;
  severity: number;
}

export interface Intent {
  id: string;
  kind: IntentKind;
  flows: string[];
  links: string[];
  params: Record<string, unknown>;
  protect: Protect;
  priority: Priority;
  enabled: boolean;
  label: string;
  note: string;
  source: string;
  created_t: number;
}

export interface IntentResilience {
  intent: string;
  label: string;
  protect: Protect;
  weight: number;
  scenarios: number;
  held: number;
  broken: string[];
  avoidable: string[];
  unavoidable: string[];
  unprotectable: string[];
  transient_only: string[];
}

export interface Compliance {
  window_s: number;
  samples: number;
  violated_s: number;
  compliance_pct: number | null;
}

export interface IntentRow extends Intent {
  status: IntentStatus;
  checks: IntentCheck[];
  violated_since: number | null;
  compliance: Record<"1m" | "5m" | "15m", Compliance>;
  timeline: [number, string][];
  resilience: IntentResilience | null;
}

export interface ScenarioInfo {
  id: string;
  kind: "link" | "node" | "double";
  label: string;
  links: string[];
  nodes: string[];
}

export interface IntentResult {
  intent: string;
  status: IntentStatus;
  severity: number;
  checks: IntentCheck[];
  weight?: number;
}

export interface ResilienceRow {
  scenario: ScenarioInfo;
  affected: string[];
  moved: string[];
  paths: Record<string, string[] | null>;
  planned: boolean;
  rounds: number;
  unreachable: string[];
  intents: IntentResult[];
  violated: string[];
  at_risk: string[];
  unprotectable: string[];
  unavoidable: string[];
  avoidable: string[];
  best: { violated: string[]; severity: number; paths: Record<string, string[] | null>; method: string; combinations: number } | null;
  severity: number;
  max_util: number;
  lost_mbps: number;
  transient: { paths: Record<string, string[] | null>; violated: string[]; max_util: number } | null;
  rtt: Record<string, number | null>;
}

export interface Criticality {
  scenario: string;
  severity: number;
  norm: number;
  violated: string[];
  unprotectable: string[];
  unreachable: string[];
  lost_mbps: number;
}

export interface Resilience {
  id: string;
  t: number | null;
  mode: string;
  plan: string | null;
  engine: string;
  scenarios: ResilienceRow[];
  intents: IntentResilience[];
  criticality: Record<string, Criticality>;
  spofs: { scenario: string; label: string; pairs: string[]; edge: boolean }[];
  now: { intents: IntentResult[]; max_util: number };
  score: number | null;
  score_best: number | null;
  cells: number;
  wall_s: number;
  rtt_margin?: number;
}

export interface ResBrief {
  score: number | null;
  score_best: number | null;
  mode: string;
  intents: { intent: string; scenarios: number; held: number; broken: string[] }[];
  violations: Record<string, string[]>;
}

export interface PlanPair {
  path: string[] | null;
  rtt_p50: number | null;
  rtt_p95: number | null;
  loss_pct: number | null;
  loss_source: string;
  offered_mbps: number;
  rx_mbps: number | null;
}

export interface Verification {
  plan: string;
  label?: string;
  source: string;
  t_apply: number;
  window: [number, number];
  before: Record<string, string>;
  after: Record<string, string>;
  predicted: Record<string, string>;
  regressions: string[];
  surprises: string[];
  intent_predictions_correct: number;
  intent_predictions_judged: number;
  verdict: "verified" | "rolled back" | "not judged";
  reason?: string;
}

export interface LedgerEntry extends Verification {
  id: string;
  t: number;
  rows?: ValidationRow[];
}

export interface Plan {
  id: string;
  t: number;
  source: string;
  status: string;
  label: string;
  mode_at_creation: string;
  primary: Record<string, string[]>;
  protection: Record<string, Record<string, string[]>>;
  moved: string[];
  objective: { v_now: number; v_fail: number; cost: number };
  current: { v_now: number; v_fail: number; cost: number };
  predicted: {
    intents: IntentResult[];
    pairs: Record<string, PlanPair>;
    link_util: Record<string, number>;
    scenarios: Record<string, { label: string; kind: string; severity: number; violated: string[]; unreachable: string[]; moved: string[]; max_util: number }>;
  };
  alternatives: { primary: Record<string, string[]>; v_now: number; v_fail: number; cost: number; is_current: boolean }[];
  search: {
    method: string;
    space: number;
    primaries_ranked: number;
    primaries_kept: number;
    scenarios: number;
    evaluations: number;
    wall_s: number;
    pruned_candidates: Record<string, number>;
  };
  resilience_before: ResBrief;
  resilience_after: ResBrief;
  rtt_margin: number;
  confirmation?: {
    t: number;
    wall_s: number;
    rows: { pair: string; metric: string; fluid: number | null; packet: number | null; diff_pct: number | null }[];
    max_rtt_diff_pct: number;
    max_util_diff_pp: number;
    agree: boolean;
    error?: string;
  };
  verification?: Verification;
  applied_t?: number;
  applied_by?: string;
}

export interface GateCheck {
  check: string;
  ok: boolean;
  detail: string;
}

export interface Decision {
  id: string;
  t: number;
  mode: string;
  trigger: string;
  plan?: string;
  status: string;
  summary?: string;
  gate?: { ok: boolean; reasons: string[]; checks: GateCheck[] };
  objective?: { v_now: number; v_fail: number; cost: number };
  current?: { v_now: number; v_fail: number; cost: number };
  resilience?: { before: number | null; after: number | null };
  verification?: Verification;
  error?: string;
}

export interface AutopilotBrief {
  mode: "off" | "shadow" | "approve" | "auto";
  state: string;
  pending: { id: string; plan: string; summary: string; resilience?: { before: number | null; after: number | null } } | null;
  last: { id: string; t: number; status: string; summary?: string; trigger?: string } | null;
  deployments: number;
  rollbacks: number;
}

export interface AssureSnapshot {
  intents: {
    id: string;
    label: string;
    kind: IntentKind;
    priority: Priority;
    protect: Protect;
    status: IntentStatus;
    worst: IntentCheck | null;
    compliance_5m: number | null;
  }[];
  counts: Record<string, number>;
  resilience: {
    id: string;
    t: number;
    mode: string;
    plan: string | null;
    score: number | null;
    score_best: number | null;
    criticality: Record<string, Criticality>;
    worst: { scenario: string; label: string; violated: string[]; avoidable: string[]; severity: number }[];
  } | null;
  autopilot: AutopilotBrief;
  tick_ms: number;
}

export interface UncertaintyClass {
  engine: string;
  cls: "rtt" | "throughput" | "loss";
  n: number;
  level: number;
  half_width: number | null;
  half_width_source: string;
  unit: string;
  bias: number | null;
  coverage: number | null;
  coverage_tested: number;
}

export interface DrillRun {
  id: string;
  t: number;
  scenario: ScenarioInfo;
  mode: string;
  plan: string | null;
  t_inject: number;
  window: [number, number];
  predicted: { paths_fluid: Record<string, string[] | null>; paths_packet: Record<string, string[] | null>; intents: Record<string, string> };
  measured: { paths: Record<string, string[] | null>; intents: Record<string, string> };
  intents: { intent: string; predicted: string; measured: string; match: boolean | null }[];
  intent_accuracy: number | null;
  paths_match_fluid: boolean;
  paths_match_packet: boolean;
  transient: { timeline: [number, Record<string, string>][]; violation_s: Record<string, number>; settled_after_s: number | null };
  rows_fluid: ValidationRow[];
  summary_fluid: ValidationRun["summary"];
  rows_packet: ValidationRow[];
  summary_packet: ValidationRun["summary"];
}

export interface BenchRow {
  strategy: string;
  scenario: string;
  label: string;
  plan?: string | null;
  baseline_violation_s: number;
  violation_s: number;
  new_violation_s: number;
  time_to_compliance_s: number | null;
  route_changes: number;
  offered_mbit: number;
  received_mbit: number;
  lost_mbit: number;
  measured_broken: string[];
  predicted_broken: string[] | null;
  prediction_match: boolean | null;
  timeline: { t: number; status: Record<string, string> }[];
}

export interface BenchSummary {
  failures: number;
  violation_s: number;
  new_violation_s: number;
  baseline_violation_s: number;
  mean_time_to_compliance_s: number | null;
  never_compliant: number;
  route_changes: number;
  lost_mbit: number;
  delivered_pct: number;
  prediction_accuracy_pct: number | null;
}

export interface BenchRun {
  id: string;
  t: number;
  duration_s: number;
  strategies: string[];
  scenarios: string[];
  rates: Record<string, number>;
  intents: Intent[];
  results: BenchRow[];
  summary: Record<string, BenchSummary>;
}

// ---------------------------------------------------------------- presentation helpers
export const INTENT_STATUS: Record<IntentStatus, { label: string; color: string }> = {
  ok: { label: "Met", color: "#0ca30c" },
  at_risk: { label: "At risk", color: "#fab219" },
  violated: { label: "Violated", color: "#d03b3b" },
  unknown: { label: "No data", color: "#6b7785" },
  disabled: { label: "Off", color: "#4b5866" },
};

export const KIND_LABEL: Record<IntentKind, string> = {
  reach: "Reachability",
  latency: "Latency SLO",
  loss: "Loss SLO",
  bandwidth: "Bandwidth headroom",
  avoid: "Avoid (policy)",
  waypoint: "Waypoint (policy)",
  max_util: "Link headroom",
  disjoint: "Path diversity",
};

export const PROTECT_LABEL: Record<Protect, string> = {
  none: "normal operation only",
  link: "also after any single link failure",
  node: "also after any single router failure",
  any: "also after any single link or router failure",
};

export const STRATEGY_LABEL: Record<string, string> = {
  static: "Static (Dijkstra)",
  "adaptive-raw": "Adaptive, no herd guard",
  adaptive: "Adaptive + herd guard",
  intent: "Intent planner (Assure)",
};

/** The router part of a path, joined: c1-sw1-r1-r2-r5-sw2-srv1 -> r1 r2 r5 */
export function routers(path: string[] | null | undefined, sep = "·"): string {
  if (!path) return "no path";
  return path.filter(isRouter).join(` ${sep} `);
}

export function scenarioElement(id: string): { kind: "link" | "node" | "double"; ref: string } {
  const [kind, ref] = id.split(":", 2) as ["link" | "node" | "double", string];
  return { kind, ref };
}

export function fmtCheck(c: IntentCheck | null | undefined): string {
  if (!c) return "";
  const v = c.value;
  const u = c.unit;
  const num = (x: number | string | null) => (typeof x === "number" ? (u === "ms" ? `${x.toFixed(1)} ms` : u === "%" ? `${x.toFixed(1)}%` : u === "Mbit/s" ? `${x.toFixed(1)} Mbit/s` : x.toFixed(2)) : String(x ?? "–"));
  if (u === "crossed") return `crosses ${v}`;
  if (u === "via") return v === "yes" ? `via ${c.limit}` : `not via ${c.limit}`;
  if (u === "shared") return v === "none" ? "no shared links" : `shares ${v}`;
  if (u === "") return String(v ?? "–");
  return `${num(v)} (limit ${num(c.limit)})`;
}
