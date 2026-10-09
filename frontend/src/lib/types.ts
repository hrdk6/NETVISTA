// Shapes of the data the backend sends (see backend/netvista/runtime.py and api/app.py).

export type NodeType = "router" | "switch" | "client" | "server";
export type Health = "ok" | "degraded" | "down" | "unknown";

export interface NodeSpec {
  id: string;
  type: NodeType;
  label: string;
  x: number;
  y: number;
}

export interface LinkSpec {
  id: string;
  a: string;
  b: string;
  bw_mbps: number;
  delay_ms: number;
  jitter_ms: number;
  loss_pct: number;
  queue_pkts: number;
}

export interface Topology {
  name: string;
  description: string;
  nodes: NodeSpec[];
  links: LinkSpec[];
  traffic: { src: string; dst: string; rate_mbps: number }[];
}

export interface PlanIntf {
  name: string;
  ip: string | null;
  prefixlen: number | null;
}

export interface AddressPlan {
  links: Record<string, Record<string, PlanIntf>>;
  subnets: { cidr: string; kind: string; routers: Record<string, string>; hosts: Record<string, string>; links: string[] }[];
  hosts: Record<string, { ip: string; gateway: string; gateway_ip: string; subnet: string }>;
}

export interface LinkParams {
  bw_mbps: number;
  delay_ms: number;
  jitter_ms: number;
  loss_pct: number;
  queue_pkts: number;
}

export interface DirStats {
  bps: number;
  pps: number;
  util: number;
  drops_ps: number;
  drops_total: number;
  backlog_pkts: number;
  tx_bytes_total: number;
}

export interface LinkView {
  id: string;
  a: string;
  b: string;
  health: Health;
  alive: boolean | null;
  admin_up: boolean;
  probe_span: string;
  probe_streams: string[];
  rtt_ms: number | null;
  rtt_p50: number | null;
  rtt_p95: number | null;
  rtt_p99: number | null;
  expected_rtt_ms: number;
  loss_pct: number | null;
  ab: DirStats;
  ba: DirStats;
  util: number;
  cfg: LinkParams;
  base: LinkParams;
}

export interface IntfView {
  name: string;
  ip: string | null;
  link: string;
  rx_bps: number;
  tx_bps: number;
  rx_pps: number;
  tx_pps: number;
  drops_total: number;
  backlog_pkts: number;
}

export interface NodeView {
  id: string;
  type: NodeType;
  health: Health;
  rx_bps: number;
  tx_bps: number;
  drops_ps: number;
  interfaces: IntfView[];
}

export interface FlowView {
  pair: string;
  src: string;
  dst: string;
  path: string[] | null;
  offered_mbps: number;
  probe: {
    alive: boolean | null;
    rtt_ms: number | null;
    rtt_p50: number | null;
    rtt_p95: number | null;
    rtt_p99: number | null;
    probe_loss_pct: number | null;
  };
  traffic: { rx_mbps: number; loss_pct: number; jitter_ms: number; samples: number } | null;
}

export interface CandidateEval {
  path: string[];
  links: string[];
  latency_ms: number;
  loss_pct: number;
  util_pct: number;
  feasible: boolean;
  score: number | null;
  bottleneck: string | null;
  dead_links: string[];
  chosen?: boolean;
}

export interface RoutingFlow {
  pair: string;
  src: string;
  dst: string;
  path: string[] | null;
  static_path: string[];
  since: number;
  changes: number;
  last_reason: string;
  offered_mbps: number;
  candidates: CandidateEval[];
}

export interface Incident {
  id: number;
  kind: "failure" | "degradation";
  pair: string;
  link: string | null;
  cause: string | null;
  mode: string;
  status: "open" | "recovered" | "unrecovered";
  note: string;
  t_inject: number | null;
  t_detect: number;
  t_reroute: number | null;
  t_recover: number | null;
  from_path: string[] | null;
  to_path: string[] | null;
  detection_ms: number | null;
  reroute_ms: number | null;
  install_ms: number | null;
  recovery_ms: number | null;
}

export interface RoutingState {
  mode: "static" | "adaptive" | "intent";
  weights: { latency: number; loss: number; util: number };
  hysteresis: number;
  hold_down_s: number;
  dead_interval_s: number;
  probe_interval_s: number;
  links_alive: Record<string, boolean>;
  links_suspect: string[];
  flows: Record<string, RoutingFlow>;
  incidents: Incident[];
  tick_ms: number;
  herd_guard?: boolean;
  wtr_s?: number;
  scenario?: string | null;
  plan?: { id: string; label: string; primary: Record<string, string[]>; protected: string[]; applied_t: number; applied_by: string } | null;
}

export interface Injection {
  id: string;
  kind: string;
  target: string;
  params: Record<string, number | string>;
  t: number;
  label: string;
  state: "active" | "reverted" | "superseded" | "expired";
  t_end: number | null;
}

export interface TrafficFlow {
  id: string;
  src: string;
  dst: string;
  pair: string;
  rate_mbps: number;
  kind: "background" | "burst";
  port: number;
  state: string;
  started_at: number;
  ended_at: number | null;
  duration_s: number | null;
  latest: { t: number; rx_mbps: number; loss_pct: number; jitter_ms: number } | null;
}

/** Moments when every probe stream went mute together: the host's packet path froze (DESIGN.md 11.10). */
export interface HostStalls {
  count: number;
  total_s: number;
  longest_s: number | null;
  median_period_s: number | null;
  excluded_samples: number;
  active: { start: number; streams: number; mute: number } | null;
}

export interface Snapshot {
  t: number;
  boot_id?: string;
  uptime_s: number;
  links: Record<string, LinkView>;
  nodes: Record<string, NodeView>;
  flows: Record<string, FlowView>;
  routing: RoutingState;
  chaos: {
    active: Injection[];
    history: Injection[];
    links: Record<string, { params: LinkParams; admin_up: boolean }>;
    nodes_down: string[];
  };
  traffic: TrafficFlow[];
  agents: Record<string, boolean>;
  host_stalls?: HostStalls;
  jobs?: Record<string, Record<string, unknown>>;
  ai?: AISnapshot;
  assure?: import("./assure").AssureSnapshot;
}

export interface NvEvent {
  seq: number;
  t: number;
  kind: string;
  message: string;
  severity: "info" | "warn" | "error" | "success";
  data: Record<string, unknown>;
}

export interface HistorySample {
  t: number;
  flows: Record<
    string,
    {
      rtt_ms: number | null;
      rtt_p50: number | null;
      rtt_p95: number | null;
      rtt_p99: number | null;
      probe_loss_pct: number | null;
      rx_mbps: number | null;
      iperf_loss_pct: number | null;
      offered_mbps: number;
      path: string | null;
    }
  >;
  links: Record<string, { util: number; rtt_ms: number | null; loss_pct: number | null; health: Health }>;
}

export interface Change {
  kind: string;
  target?: string | null;
  params?: Record<string, number | string>;
}

export interface SimPair {
  path: string[];
  start_path: string[];
  rerouted: boolean;
  reason: string | null;
  offered_mbps: number;
  rx_mbps: number | null;
  loss_pct: number | null;
  owd_p50_ms: number | null;
  rtt_mean: number | null;
  rtt_p50: number | null;
  rtt_p95: number | null;
  rtt_p99: number | null;
  probe_loss_pct: number | null;
}

export interface SimLink {
  up: boolean;
  util: number;
  latency_ms: number;
  loss_pct: number;
  cfg: LinkParams;
}

export interface SimResult {
  mode: string;
  duration_s: number;
  warmup_s: number;
  pairs: Record<string, SimPair>;
  links: Record<string, SimLink>;
  routing_rounds: Record<string, { reason: string; chosen: string[] | null; candidates: CandidateEval[] }>[];
  wall_s: number;
}

export interface Prediction {
  id: string;
  t: number;
  label: string;
  changes: Change[];
  mode: string;
  weights: { latency: number; loss: number; util: number };
  duration_s: number;
  calibration_t: number | null;
  result: SimResult;
  baseline: SimResult | null;
  live: {
    t: number;
    pairs: Record<string, { path: string[] | null; offered_mbps: number; rtt_p50: number | null; rtt_p95: number | null; rtt_p99: number | null; probe_loss_pct: number | null; rx_mbps: number | null; loss_pct: number | null }>;
    links: Record<string, { util: number; latency_ms: number | null; loss_pct: number | null; health: Health }>;
  };
  wall_s: number;
}

export interface Calibration {
  t: number | null;
  method?: string;
  endpoint_ms?: number;
  per_hop_ms?: number;
  fit_rms_ms?: number;
  n_streams?: number;
  noise_samples?: number;
  noise_p05_ms?: number | null;
  noise_p95_ms?: number | null;
  conditions?: { max_link_util: number; offered_mbps: number; active_injections: number; calm: boolean };
  observations?: { stream: string; n_links: number; measured_p50: number; structural_p50: number; residual: number }[];
}

export interface ValidationRow {
  pair: string;
  metric: string;
  unit: string;
  kind: "relative" | "absolute" | "match";
  predicted: number | string | null;
  measured: number | string | null;
  error: number | null;
}

export interface ValidationRun {
  id: string;
  t: number;
  label: string;
  changes: Change[];
  mode: string;
  prediction_id: string;
  settle_s: number;
  measure_window: [number, number];
  sim_duration_s: number;
  sim_wall_s: number;
  calibration_t: number | null;
  rows: ValidationRow[];
  /** the fluid model's prediction of the same scenario, scored against the same measurement */
  rows_fluid?: ValidationRow[];
  summary_fluid?: Partial<ValidationRun["summary"]>;
  fluid_wall_s?: number | null;
  summary: {
    latency_mape: number | null;
    latency_p50_mape: number | null;
    latency_p95_mape: number | null;
    throughput_mape: number | null;
    loss_mae_pp: number | null;
    probe_loss_mae_pp: number | null;
    path_match_pct: number | null;
    aggregate_throughput_err?: number | null;
    aggregate_loss_err_pp?: number | null;
    n_rows: number;
  };
}

export interface JourneyHop {
  node: string;
  type: NodeType;
  label: string;
  in?: { link: string; intf: string; ip: string | null };
  out?: {
    link: string;
    intf: string;
    ip: string | null;
    health: Health;
    rtt_ms: number | null;
    loss_pct: number | null;
    util: number;
    bps: number;
    pps: number;
    drops_ps: number;
    cfg: LinkParams;
    probe_span: string;
  };
  kernel_decision?: string;
  table_routes?: string[];
}

export interface Journey {
  src: string;
  dst: string;
  src_ip: string;
  dst_ip: string;
  path: string[];
  core_links: string[];
  table: number | string;
  route_source: string;
  hops: JourneyHop[];
  t: number;
}

export interface ScenarioSummary {
  name: string;
  file: string;
  recorded_at: number;
  duration_s: number;
  actions: number;
  topology: string;
}

// ---------------------------------------------------------------- AIOps (backend/netvista/ai)
export interface Anomaly {
  id: number;
  signal: string;
  kind: "link" | "access" | "flow";
  entity: string;
  metric: "rtt" | "loss" | "util" | "data_loss";
  unit: "ms" | "%" | "ratio";
  label: string;
  t_start: number;
  t_raised: number;
  t_end: number | null;
  active: boolean;
  value: number;
  z: number;
  peak_value: number;
  peak_z: number;
  normal_mean: number;
  normal_scale: number;
  normal_upper: number;
  severity: "minor" | "major";
}

export interface Cause {
  id: string;
  type: "link_down" | "node_down" | "node_degraded" | "latency" | "packet_loss" | "congestion" | "traffic_surge";
  element: string;
  element_kind: string;
  title: string;
  confidence: "high" | "medium" | "low";
  since: number | null;
  evidence: string[];
  explains: number;
  contradicted_by: string[];
  alternatives: string[];
  affected_flows: string[];
  rerouted_flows: { pair: string; from: string[] | null; to: string[] | null }[];
  surge: { pair: string; rate_mbps: number } | null;
}

export interface Diagnosis {
  t: number | null;
  status: "learning" | "normal" | "degraded";
  causes: Cause[];
  consequences: string[];
  unexplained: string[];
  observed: { bad?: number; good?: number };
}

export interface DetectorSummary {
  signals: number;
  learning: number;
  normal: number;
  anomalous: number;
  warmup_s: number;
  warmup_progress: number | null;
  learned_since: number | null;
  params: { alpha: number; z_on: number; z_strong: number; z_off: number; n_on: number; n_off: number };
}

export interface CopilotBrief {
  available: boolean;
  provider: "anthropic" | "gemini" | "groq" | "ollama" | "none";
  label: string;
  local: boolean;
  busy: boolean;
}

export interface AISnapshot {
  anomalies: Anomaly[];
  diagnosis: Diagnosis;
  detector: DetectorSummary;
  copilot: CopilotBrief;
}

export interface CopilotStatus extends CopilotBrief {
  model: string | null;
  reason: string;
  checked_at: number;
  transport: string | null;
  backup?: string | null;
  conversations: number;
  tools: { name: string; kind: "read" | "simulate" | "propose"; local: boolean }[];
}

export interface SignalRow {
  signal: string;
  kind: Anomaly["kind"];
  entity: string;
  metric: Anomaly["metric"];
  unit: Anomaly["unit"];
  label: string;
  state: "learning" | "normal" | "anomalous";
  samples: number;
  value: number | null;
  normal_mean: number | null;
  normal_scale: number | null;
  normal_upper: number | null;
  z: number | null;
}

export interface SignalPoint {
  t: number;
  value: number | null;
  normal_mean: number | null;
  normal_upper: number | null;
  anomalous: boolean;
}

export interface Proposal {
  id: string;
  conversation_id: string | null;
  action: "chaos_inject" | "chaos_revert" | "routing" | "traffic" | "intent_add" | "plan_apply";
  spec: Record<string, unknown>;
  label: string;
  reason: string | null;
  t: number;
  status: "pending" | "applied" | "dismissed" | "failed";
  result: unknown;
  t_decided: number | null;
}

export interface EvalRow {
  label: string;
  kind: string;
  target: string;
  expect_element: string;
  expect_type: string[];
  subtle: boolean;
  ai_detected: boolean;
  ai_detect_s: number | null;
  ai_first_signal: string | null;
  ai_anomalies: number;
  threshold_detected: boolean;
  threshold_detect_s: number | null;
  threshold_flaps: number;
  diagnosis: { title: string; type: string; element: string; confidence: string; alternatives: string[] } | null;
  diagnosis_other_causes: string[];
  diagnosis_location_ok: boolean;
  diagnosis_type_ok: boolean;
  diagnosis_correct: boolean;
  diagnosis_correct_after_s: number | null;
}

export interface EvalSummary {
  faults: number;
  ai_detected: number;
  threshold_detected: number;
  ai_mean_detect_s: number | null;
  threshold_mean_detect_s: number | null;
  subtle_faults: number;
  subtle_ai_detected: number;
  subtle_threshold_detected: number;
  diagnosis_correct: number;
  diagnosis_location_ok: number;
  diagnosis_accuracy_pct: number | null;
  false_alarms: number | null;
  false_alarms_per_min: number;
  threshold_false_alarms: number | null;
  threshold_flaps_during_faults: number;
}

export interface EvalRun {
  id: string;
  t: number;
  duration_s: number;
  quiet: { duration_s: number; ai_false_alarms: number; ai_false_alarm_signals: string[]; threshold_false_alarms: number; diagnosis_false_s: number } | null;
  scenarios: EvalRow[];
  summary: EvalSummary;
}
