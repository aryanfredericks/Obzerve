import { type ReactNode, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type MetricsWindow, useApi } from "../api";
import { KindTag } from "../components/Badges";
import { BarList, LineChart, StackedColumns } from "../components/charts";
import { ErrorState } from "../components/ErrorState";
import { formatCompact, formatCost, formatDuration, formatNumber, formatPercent } from "../format";
import type { GroupBy } from "../types";

const RANGES = [
  { key: "1h", label: "Last hour", ms: 3_600_000 },
  { key: "24h", label: "Last 24 hours", ms: 86_400_000 },
  { key: "7d", label: "Last 7 days", ms: 7 * 86_400_000 },
  { key: "30d", label: "Last 30 days", ms: 30 * 86_400_000 },
] as const;

const BREAKDOWNS: { key: GroupBy; label: string }[] = [
  { key: "operation", label: "Operations" },
  { key: "model", label: "Models" },
  { key: "workflow", label: "Workflows" },
];

function Stat({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail?: ReactNode;
  tone?: "error" | "warn";
}) {
  return (
    <div className={`stat${tone ? ` stat-${tone}` : ""}`}>
      <span>{label}</span>
      <strong className="stat-value">{value}</strong>
      {detail && <span className="stat-detail">{detail}</span>}
    </div>
  );
}

export function Metrics() {
  const [params, setParams] = useSearchParams();
  const rangeKey = params.get("range") ?? "24h";
  const range = RANGES.find((r) => r.key === rangeKey) ?? RANGES[1];
  const workflow = params.get("workflow") ?? "";
  const groupBy = (params.get("by") as GroupBy) ?? "operation";
  const [refreshed, setRefreshed] = useState(0);

  const window: MetricsWindow = useMemo(() => {
    const end = new Date();
    return {
      start: new Date(end.getTime() - range.ms).toISOString(),
      end: end.toISOString(),
      ...(workflow ? { workflow } : {}),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range.ms, workflow, refreshed]);
  const deps = [window.start, window.workflow];

  const summary = useApi(() => api.metricsSummary(window), deps);
  const series = useApi(() => api.metricsTimeseries(window), deps);
  const breakdown = useApi(() => api.metricsBreakdown(window, groupBy), [...deps, groupBy]);
  // Workflow names for the filter: unfiltered by workflow so every option stays listed.
  const workflows = useApi(
    () => api.metricsBreakdown({ start: window.start, end: window.end }, "workflow"),
    [window.start],
  );

  const set = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const error = summary.error ?? series.error ?? breakdown.error;
  const loading = summary.loading || series.loading || breakdown.loading;
  const s = summary.data;
  const ts = series.data;
  const times = ts?.points.map((p) => p.time) ?? [];

  return (
    <div className="page">
      <div className="page-head">
        <h1>Metrics</h1>
        <button className="button" onClick={() => setRefreshed((n) => n + 1)} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      <div className="filters">
        <div className="segmented" role="radiogroup" aria-label="Time range">
          {RANGES.map((r) => (
            <button
              key={r.key}
              role="radio"
              aria-checked={r.key === range.key}
              className={r.key === range.key ? "active" : ""}
              onClick={() => set("range", r.key === "24h" ? null : r.key)}
            >
              <span className="label-long">{r.label}</span>
              <span className="label-short">{r.key}</span>
            </button>
          ))}
        </div>
        <select value={workflow} onChange={(e) => set("workflow", e.target.value || null)} aria-label="Workflow">
          <option value="">All workflows</option>
          {workflows.data?.rows.map((r) => (
            <option key={r.key} value={r.key}>
              {r.key}
            </option>
          ))}
          {workflow && !workflows.data?.rows.some((r) => r.key === workflow) && <option value={workflow}>{workflow}</option>}
        </select>
      </div>

      {error ? (
        <ErrorState error={error} />
      ) : (
        <div className={loading && s ? "refreshing" : ""}>
          {s && (
            <div className="stats kpis">
              <Stat
                label="Requests"
                value={formatCompact(s.requests)}
                detail={s.retried_requests ? `${formatNumber(s.retried_requests)} with retries` : undefined}
              />
              <Stat
                label="Error rate"
                value={formatPercent(s.error_rate)}
                detail={`${formatNumber(s.errors)} failed`}
                tone={s.errors ? "error" : undefined}
              />
              <Stat
                label="p95 latency"
                value={s.latency.p95_ms === null ? "–" : formatDuration(s.latency.p95_ms)}
                detail={
                  s.latency.p50_ms === null
                    ? undefined
                    : `p50 ${formatDuration(s.latency.p50_ms)} · p99 ${formatDuration(s.latency.p99_ms ?? 0)}`
                }
              />
              <Stat
                label="Estimated cost"
                value={formatCost(s.estimated_cost_usd)}
                detail={
                  s.unpriced_llm_calls ? (
                    <span className="text-warn" title="Add prices with `obzerve-server pricing set`">
                      ⚠ partial: {formatNumber(s.unpriced_llm_calls)} unpriced calls
                    </span>
                  ) : s.estimated_cost_per_request_usd !== null ? (
                    `${formatCost(s.estimated_cost_per_request_usd)} per request`
                  ) : undefined
                }
              />
              <Stat
                label="Tokens in / out"
                value={`${formatCompact(s.input_tokens)} / ${formatCompact(s.output_tokens)}`}
                detail={s.cached_input_tokens ? `${formatCompact(s.cached_input_tokens)} cached` : undefined}
              />
              <Stat
                label="LLM timeouts"
                value={formatPercent(s.llm_timeout_rate)}
                detail={`of ${formatNumber(s.llm_calls)} calls`}
                tone={s.llm_timeout_rate ? "warn" : undefined}
              />
              <Stat
                label="Tool success"
                value={formatPercent(s.tool_success_rate)}
                detail={`of ${formatNumber(s.tool_calls)} calls`}
              />
            </div>
          )}

          {ts && (
            <div className="chart-grid">
              <div className="panel">
                <LineChart
                  title="Request latency"
                  times={times}
                  bucketSeconds={ts.bucket_seconds}
                  format={formatDuration}
                  series={[
                    { key: "p50", label: "p50", color: "var(--series-1)", values: ts.points.map((p) => p.p50_ms) },
                    { key: "p95", label: "p95", color: "var(--series-2)", values: ts.points.map((p) => p.p95_ms) },
                  ]}
                />
              </div>
              <div className="panel">
                <StackedColumns
                  title="Requests"
                  times={times}
                  bucketSeconds={ts.bucket_seconds}
                  format={(v) => formatCompact(v)}
                  series={[
                    { key: "ok", label: "Succeeded", color: "var(--series-1)", values: ts.points.map((p) => p.requests - p.errors) },
                    { key: "error", label: "Failed", color: "var(--status-critical)", values: ts.points.map((p) => p.errors) },
                  ]}
                />
              </div>
              <div className="panel">
                <StackedColumns
                  title="Estimated cost"
                  times={times}
                  bucketSeconds={ts.bucket_seconds}
                  format={formatCost}
                  series={[
                    { key: "cost", label: "Estimated cost", color: "var(--series-1)", values: ts.points.map((p) => p.estimated_cost_usd) },
                  ]}
                />
              </div>
              <div className="panel">
                <BarList
                  title="Time per request by operation type"
                  format={formatDuration}
                  rows={Object.entries(s?.time_per_request_ms ?? {})
                    .sort(([, a], [, b]) => b - a)
                    .map(([kind, ms]) => ({ key: kind, label: kind === "llm" ? "LLM calls" : `${kind[0].toUpperCase()}${kind.slice(1)}`, value: ms }))}
                />
                <p className="chart-note">
                  Mean time each request spends in LLM calls, retrieval, and tools. Concurrent operations overlap, so
                  these can add up to more than the request’s duration.
                </p>
              </div>
            </div>
          )}

          <section className="panel breakdown">
            <div className="breakdown-head">
              <h2>Breakdown</h2>
              <div className="segmented" role="tablist">
                {BREAKDOWNS.map((b) => (
                  <button
                    key={b.key}
                    role="tab"
                    aria-selected={b.key === groupBy}
                    className={b.key === groupBy ? "active" : ""}
                    onClick={() => set("by", b.key === "operation" ? null : b.key)}
                  >
                    {b.label}
                  </button>
                ))}
              </div>
            </div>
            {breakdown.data && breakdown.data.rows.length === 0 ? (
              <p className="muted breakdown-empty">No data in this range.</p>
            ) : (
              <div className="table-wrap flush">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>{BREAKDOWNS.find((b) => b.key === groupBy)?.label.replace(/s$/, "")}</th>
                      <th className="num">Count</th>
                      <th className="num">Error rate</th>
                      <th className="num">Retries</th>
                      <th className="num">p50</th>
                      <th className="num">p95</th>
                      <th className="num">p99</th>
                      <th className="num">Tokens in / out</th>
                      <th className="num">Est. cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {breakdown.data?.rows.map((r) => (
                      <tr key={`${r.key}-${r.kind}-${r.provider}`}>
                        <td>
                          <span className="trace-link plain">
                            {groupBy === "operation" && r.kind && <KindTag kind={r.kind} />}
                            {groupBy === "workflow" ? (
                              <Link to={`/?q=${encodeURIComponent(r.key)}`}>{r.key}</Link>
                            ) : (
                              r.key
                            )}
                          </span>
                          {r.provider && <div className="sub">{r.provider}</div>}
                        </td>
                        <td className="num">{formatNumber(r.count)}</td>
                        <td className={`num${r.errors ? " text-error" : ""}`}>{formatPercent(r.error_rate)}</td>
                        <td className={`num${r.retries ? " text-warn" : ""}`}>{formatNumber(r.retries)}</td>
                        <td className="num">{r.p50_ms === null ? "–" : formatDuration(r.p50_ms)}</td>
                        <td className="num">{r.p95_ms === null ? "–" : formatDuration(r.p95_ms)}</td>
                        <td className="num">{r.p99_ms === null ? "–" : formatDuration(r.p99_ms)}</td>
                        <td className="num">
                          {formatCompact(r.input_tokens)} / {formatCompact(r.output_tokens)}
                        </td>
                        <td className="num" title={r.unpriced_llm_calls ? `${r.unpriced_llm_calls} unpriced calls` : undefined}>
                          {formatCost(r.estimated_cost_usd)}
                          {r.unpriced_llm_calls > 0 && <span className="text-warn"> ⚠</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <p className="chart-note">Costs are estimates from the configured price table, not provider invoices.</p>
        </div>
      )}
    </div>
  );
}
