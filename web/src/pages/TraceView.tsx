import { useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, useApi } from "../api";
import { StatusBadge } from "../components/Badges";
import { ReplayPanel } from "../components/ReplayPanel";
import { ErrorState } from "../components/ErrorState";
import { SpanDetail } from "../components/SpanDetail";
import { Waterfall } from "../components/Waterfall";
import { formatCost, formatDuration, formatNumber, formatTime, parseIsoMs } from "../format";

export function TraceView() {
  const { traceId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const { data, error, loading, reload } = useApi(() => api.trace(traceId), [traceId]);

  const spans = data?.spans ?? [];
  const origin = useMemo(
    () => (spans.length ? Math.min(...spans.map((s) => parseIsoMs(s.start_time))) : 0),
    [spans],
  );

  // Default selection: the requested span, else the first failing span, else the root.
  const requested = params.get("span");
  const firstError = spans.find((s) => s.status === "error" && !spans.some((c) => c.parent_span_id === s.span_id && c.status === "error"));
  const selectedId = requested ?? firstError?.span_id ?? data?.trace.root_span_id ?? spans[0]?.span_id ?? null;
  const selected = spans.find((s) => s.span_id === selectedId) ?? null;

  if (error) {
    return (
      <div className="page">
        <Link to="/" className="back">← Traces</Link>
        <ErrorState error={error} />
      </div>
    );
  }
  if (loading && !data) return <div className="page muted">Loading trace…</div>;
  if (!data) return null;

  const { trace } = data;
  return (
    <div className="page trace-page">
      <Link to="/" className="back">← Traces</Link>
      <div className="page-head">
        <div>
          <h1>
            {trace.name ?? "(root span pending)"} <StatusBadge status={trace.status} />
          </h1>
          <div className="sub">
            <code>{trace.trace_id}</code> · {trace.service_name} · {formatTime(trace.start_time)}
            {trace.prompt_name && (
              <>
                {" "}· prompt <strong>{trace.prompt_name}</strong>
                {trace.prompt_version && ` v${trace.prompt_version}`}
              </>
            )}
          </div>
          {trace.replay_of && (
            <div className="replay-of">
              ↻ <strong>{trace.replay_mode}</strong> replay of{" "}
              <Link to={`/traces/${trace.replay_of}`}>
                <code>{trace.replay_of.slice(0, 12)}</code>
              </Link>
              {trace.replay_label && <span className="muted"> · {trace.replay_label}</span>}
              {" · "}
              <Link to={`/traces/${trace.replay_of}/compare/${trace.trace_id}`}>compare with original</Link>
            </div>
          )}
        </div>
        <button className="button" onClick={reload}>Refresh</button>
      </div>

      <div className="stats">
        <div className="stat"><span>Duration</span><strong>{formatDuration(trace.duration_ms)}</strong></div>
        <div className="stat"><span>Spans</span><strong>{trace.span_count}</strong></div>
        <div className={`stat${trace.error_count ? " stat-error" : ""}`}><span>Errors</span><strong>{trace.error_count}</strong></div>
        <div className={`stat${trace.retry_count ? " stat-warn" : ""}`}><span>Retries</span><strong>{trace.retry_count}</strong></div>
        <div className="stat"><span>Tokens in / out</span><strong>{formatNumber(trace.input_tokens)} / {formatNumber(trace.output_tokens)}</strong></div>
        <div className="stat">
          <span>Est. cost</span>
          <strong>{formatCost(trace.estimated_cost_usd)}</strong>
          {trace.unpriced_llm_calls > 0 && (
            <span className="stat-detail text-warn">⚠ {trace.unpriced_llm_calls} unpriced LLM calls</span>
          )}
        </div>
      </div>

      {firstError && (
        <button
          className="callout"
          onClick={() => setParams({ span: firstError.span_id }, { replace: true })}
        >
          <strong>{trace.status === "error" ? "Root cause candidate:" : "Recovered failure:"}</strong>{" "}
          {firstError.name}
          {firstError.error_type ? ` · ${firstError.error_type}` : ""}
          {firstError.status_message ? ` — ${firstError.status_message}` : ""}
        </button>
      )}

      {!trace.replay_of && <ReplayPanel trace={trace} />}

      <div className="trace-layout">
        <div className="panel">
          <Waterfall
            spans={spans}
            selectedId={selectedId}
            onSelect={(id) => setParams({ span: id }, { replace: true })}
          />
        </div>
        <aside className="panel">
          {selected ? <SpanDetail span={selected} traceStartMs={origin} /> : <p className="muted">Select a span.</p>}
        </aside>
      </div>
    </div>
  );
}
