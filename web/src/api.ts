import { useCallback, useEffect, useState } from "react";
import type {
  AssertionSpec,
  BreakdownRow,
  DatasetDetail,
  Dataset,
  EvalCase,
  GateResult,
  RunComparison,
  RunDetail,
  RunSummary,
  TraceComparison,
  ErrorTypeCount,
  GroupBy,
  MetricsSummary,
  MetricsTimeseries,
  Page,
  Project,
  SpanSummary,
  TraceDetail,
  TraceSummary,
} from "./types";

const KEY_STORAGE = "obzerve.apiKey";

export function getApiKey(): string {
  try {
    const stored = localStorage.getItem(KEY_STORAGE);
    if (stored) return stored;
  } catch {
    // Storage can be unavailable (private mode); fall through to the build-time default.
  }
  return import.meta.env.VITE_OBZERVE_API_KEY ?? "";
}

export function setApiKey(key: string): void {
  try {
    localStorage.setItem(KEY_STORAGE, key);
  } catch {
    // Ignore: the key then lasts only for this page load.
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Params = Record<string, string | number | boolean | null | undefined>;

function describe(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors
    return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d))).join("; ");
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    const d = detail as { message: string; reasons?: string[] };
    return [d.message, ...(d.reasons ?? [])].join(": ");
  }
  return null;
}

async function request<T>(method: string, path: string, params: Params = {}, body?: unknown): Promise<T> {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") query.set(k, String(v));
  }
  const qs = query.toString();
  const resp = await fetch(`${path}${qs ? `?${qs}` : ""}`, {
    method,
    headers: {
      Authorization: `Bearer ${getApiKey()}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = describe((await resp.json()).detail) ?? detail;
    } catch {
      // Non-JSON error body.
    }
    throw new ApiError(resp.status, detail);
  }
  return resp.json() as Promise<T>;
}

const get = <T,>(path: string, params: Params = {}) => request<T>("GET", path, params);
const post = <T,>(path: string, body: unknown) => request<T>("POST", path, {}, body);

export interface TraceFilters {
  q?: string;
  status?: string;
  has_errors?: boolean;
  has_retries?: boolean;
  min_duration_ms?: number;
}

export const api = {
  project: () => get<Project>("/api/v1/project"),
  traces: (filters: TraceFilters, cursor?: string | null) =>
    get<Page<TraceSummary>>("/api/v1/traces", { ...filters, cursor, limit: 50 }),
  trace: (traceId: string) => get<TraceDetail>(`/api/v1/traces/${traceId}`),
  spans: (params: Params, cursor?: string | null) =>
    get<Page<SpanSummary>>("/api/v1/spans", { ...params, cursor, limit: 50 }),
  errorTypes: () => get<ErrorTypeCount[]>("/api/v1/error-types"),
  metricsSummary: (window: MetricsWindow) => get<MetricsSummary>("/api/v1/metrics/summary", { ...window }),
  metricsTimeseries: (window: MetricsWindow) =>
    get<MetricsTimeseries>("/api/v1/metrics/timeseries", { ...window }),
  metricsBreakdown: (window: MetricsWindow, groupBy: GroupBy) =>
    get<{ group_by: GroupBy; rows: BreakdownRow[] }>("/api/v1/metrics/breakdown", {
      ...window,
      group_by: groupBy,
    }),
  replays: (traceId: string) => get<TraceSummary[]>(`/api/v1/traces/${traceId}/replays`),
  traceComparison: (a: string, b: string) => get<TraceComparison>("/api/v1/trace-comparison", { a, b }),
  datasets: () => get<Dataset[]>("/api/v1/datasets"),
  dataset: (name: string) => get<DatasetDetail>(`/api/v1/datasets/${encodeURIComponent(name)}`),
  createCase: (dataset: string, body: CaseRequest) =>
    post<EvalCase>(`/api/v1/datasets/${encodeURIComponent(dataset)}/cases`, body),
  archiveCase: (dataset: string, caseId: string) =>
    post<Dataset>(`/api/v1/datasets/${encodeURIComponent(dataset)}/cases/${caseId}/archive`, {}),
  evalRuns: (dataset?: string) => get<RunSummary[]>("/api/v1/eval-runs", { dataset }),
  evalRun: (id: string) => get<RunDetail>(`/api/v1/eval-runs/${id}`),
  compareRuns: (baseline: string, candidate: string) =>
    get<RunComparison>(`/api/v1/eval-runs/compare/${baseline}/${candidate}`),
  gate: (baseline: string, candidate: string, criteria: Params) =>
    get<GateResult>(`/api/v1/eval-runs/gate/${baseline}/${candidate}`, criteria),
};

export interface CaseRequest {
  trace_id: string;
  name: string;
  replay_policy: string;
  assertions: AssertionSpec[];
  notes?: string;
}

export interface MetricsWindow {
  start: string;
  end: string;
  workflow?: string;
}

/** Fetch on mount and whenever `deps` change; `reload` refetches. */
export function useApi<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    load()
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => !cancelled && setError(e))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, setData, error, loading, reload };
}
