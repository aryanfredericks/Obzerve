export type Status = "ok" | "error" | "unset";

export interface TraceSummary extends Partial<ReplayFields> {
  trace_id: string;
  root_span_id: string | null;
  name: string | null;
  service_name: string | null;
  status: Status;
  start_time: string;
  end_time: string;
  duration_ms: number;
  span_count: number;
  error_count: number;
  retry_count: number;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  estimated_cost_usd: number;
  unpriced_llm_calls: number;
}

export interface SpanEvent {
  name: string;
  time: string;
  attributes: Record<string, unknown>;
}

export interface Span {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  service_name: string | null;
  start_time: string;
  end_time: string;
  duration_ms: number;
  status: Status;
  status_message: string | null;
  error_type: string | null;
  model: string | null;
  provider: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cached_input_tokens: number | null;
  estimated_cost_usd: number | null;
  retry_count: number;
  input: string | null;
  output: string | null;
  attributes: Record<string, unknown>;
  events: SpanEvent[];
  resource: Record<string, unknown>;
}

export interface SpanSummary {
  trace_id: string;
  span_id: string;
  name: string;
  kind: string;
  start_time: string;
  duration_ms: number;
  status: Status;
  status_message: string | null;
  error_type: string | null;
  model: string | null;
  retry_count: number;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface TraceDetail {
  trace: TraceSummary;
  spans: Span[];
}

export interface ErrorTypeCount {
  error_type: string;
  count: number;
}

export interface Project {
  id: string;
  name: string;
  created_at: string;
}

export interface MetricsSummary {
  start: string;
  end: string;
  requests: number;
  errors: number;
  error_rate: number | null;
  retried_requests: number;
  retry_rate: number | null;
  latency: { p50_ms: number | null; p95_ms: number | null; p99_ms: number | null };
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  estimated_cost_usd: number;
  estimated_cost_per_request_usd: number | null;
  unpriced_llm_calls: number;
  llm_calls: number;
  llm_error_rate: number | null;
  llm_timeout_rate: number | null;
  tool_calls: number;
  tool_success_rate: number | null;
  time_per_request_ms: Record<string, number>;
}

export interface MetricsPoint {
  time: string;
  requests: number;
  errors: number;
  p50_ms: number | null;
  p95_ms: number | null;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
}

export interface MetricsTimeseries {
  start: string;
  end: string;
  bucket_seconds: number;
  points: MetricsPoint[];
}

export type GroupBy = "workflow" | "service" | "operation" | "kind" | "model";

export interface BreakdownRow {
  key: string;
  kind: string | null;
  provider: string | null;
  count: number;
  errors: number;
  error_rate: number | null;
  retries: number;
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  input_tokens: number;
  output_tokens: number;
  cached_input_tokens: number;
  estimated_cost_usd: number;
  unpriced_llm_calls: number;
}

export type Replayability = "replayable" | "partial" | "non_replayable";

export interface ReplayFields {
  replayability: Replayability;
  replay_reasons: string[];
  replay_points: number;
  replay_points_incomplete: number;
  replay_side_effects: number;
  replay_of: string | null;
  replay_mode: string | null;
  replay_label: string | null;
  prompt_name: string | null;
  prompt_version: string | null;
  eval_run_id: string | null;
}

export interface StepSide {
  span_id: string;
  status: Status;
  duration_ms: number;
  error_type: string | null;
  status_message: string | null;
  source: "recorded" | "live" | null;
  input_matched: boolean | null;
  input: string | null;
  output: string | null;
}

export interface Step {
  key: string;
  name: string;
  kind: string;
  a: StepSide | null;
  b: StepSide | null;
  output_changed: boolean;
  input_changed: boolean;
}

export interface TraceComparison {
  a: TraceSummary;
  b: TraceSummary;
  a_output: string | null;
  b_output: string | null;
  output_changed: boolean;
  steps: Step[];
}

export type AssertionSpec =
  | { type: "no_error" }
  | { type: "contains_all"; values: string[] }
  | { type: "contains_any"; values: string[]; min_matches?: number }
  | { type: "not_contains"; values: string[] }
  | { type: "regex"; pattern: string; must_match?: boolean }
  | { type: "max_latency_ms"; value: number }
  | { type: "max_output_chars"; value: number };

export interface Dataset {
  id: string;
  name: string;
  description: string | null;
  version: number;
  active_cases: number;
  runs: number;
  updated_at: string;
}

export interface EvalCase {
  id: string;
  name: string;
  notes: string | null;
  source_trace_id: string | null;
  assertions: AssertionSpec[];
  replay_policy: string;
  replayability: Replayability;
  replay_reasons: string[];
  recordings: number;
  root_input: unknown;
  added_in_version: number;
  archived_in_version: number | null;
  created_at: string;
}

export interface DatasetDetail {
  dataset: Dataset;
  version: number;
  cases: EvalCase[];
}

export interface PromptVersion {
  id: string;
  name: string;
  version: number;
  config: Record<string, unknown>;
  created_at: string;
}

export interface RunSummary {
  id: string;
  dataset: string;
  dataset_version: number;
  evaluator_version: string;
  label: string;
  replay_policy: string;
  prompt: PromptVersion | null;
  config: Record<string, unknown>;
  started_at: string;
  finished_at: string;
  cases: number;
  passed: number;
  pass_rate: number;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  traces_found: number;
  input_tokens_per_case: number | null;
  output_tokens_per_case: number | null;
  estimated_cost_per_case_usd: number | null;
  unpriced_llm_calls: number;
}

export interface AssertionVerdict {
  type: string;
  passed: boolean;
  detail: string | null;
}

export interface RunResult {
  case_id: string;
  case_name: string;
  trace_id: string | null;
  passed: boolean;
  assertions: AssertionVerdict[];
  output: string | null;
  error: string | null;
  latency_ms: number;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost_usd: number | null;
}

export interface RunDetail {
  run: RunSummary;
  results: RunResult[];
}

export interface CaseDiff {
  case_id: string;
  case_name: string;
  baseline_passed: boolean | null;
  candidate_passed: boolean | null;
  change: "regression" | "fix" | "still_passing" | "still_failing" | "added" | "removed";
  baseline_trace_id: string | null;
  candidate_trace_id: string | null;
  failed_assertions: string[];
}

export interface RunComparison {
  baseline: RunSummary;
  candidate: RunSummary;
  comparable: boolean;
  reasons: string[];
  regressions: number;
  fixes: number;
  pass_rate_delta: number;
  cost_per_case_delta_pct: number | null;
  latency_p95_delta_pct: number | null;
  cases: CaseDiff[];
}

export interface GateResult {
  passed: boolean;
  checks: { name: string; passed: boolean; detail: string }[];
  comparison: RunComparison;
}
