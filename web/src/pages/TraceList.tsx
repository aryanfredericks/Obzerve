import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, useApi, type TraceFilters } from "../api";
import { StatusBadge } from "../components/Badges";
import { ErrorState } from "../components/ErrorState";
import { formatCost, formatDuration, formatNumber, formatTime, relativeTime } from "../format";
import type { TraceSummary } from "../types";

function readFilters(params: URLSearchParams): TraceFilters {
  const minDuration = params.get("min_duration_ms");
  return {
    q: params.get("q") ?? undefined,
    status: params.get("status") ?? undefined,
    has_errors: params.get("has_errors") === "true" ? true : undefined,
    has_retries: params.get("has_retries") === "true" ? true : undefined,
    min_duration_ms: minDuration ? Number(minDuration) : undefined,
  };
}

export function TraceList() {
  const [params, setParams] = useSearchParams();
  const filters = readFilters(params);
  const [search, setSearch] = useState(filters.q ?? "");
  const [extra, setExtra] = useState<TraceSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const { data, error, loading, reload } = useApi(async () => {
    const page = await api.traces(filters);
    setExtra([]);
    setCursor(page.next_cursor);
    return page;
  }, [params.toString()]);

  const update = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const loadMore = async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await api.traces(filters, cursor);
      setExtra((prev) => [...prev, ...page.items]);
      setCursor(page.next_cursor);
    } finally {
      setLoadingMore(false);
    }
  };

  const traces = [...(data?.items ?? []), ...extra];
  const filtered = params.toString() !== "";

  return (
    <div className="page">
      <div className="page-head">
        <h1>Traces</h1>
        <button className="button" onClick={reload} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          update("q", search.trim() || null);
        }}
      >
        <input
          type="search"
          placeholder="Search trace name"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onBlur={() => update("q", search.trim() || null)}
          aria-label="Search trace name"
        />
        <select
          value={filters.status ?? ""}
          onChange={(e) => update("status", e.target.value || null)}
          aria-label="Status"
        >
          <option value="">Any status</option>
          <option value="ok">OK</option>
          <option value="error">Error</option>
          <option value="unset">Pending</option>
        </select>
        <select
          value={params.get("min_duration_ms") ?? ""}
          onChange={(e) => update("min_duration_ms", e.target.value || null)}
          aria-label="Minimum duration"
        >
          <option value="">Any duration</option>
          <option value="1000">≥ 1 s</option>
          <option value="3000">≥ 3 s</option>
          <option value="10000">≥ 10 s</option>
        </select>
        <label className="check">
          <input
            type="checkbox"
            checked={!!filters.has_errors}
            onChange={(e) => update("has_errors", e.target.checked ? "true" : null)}
          />
          Contains errors
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={!!filters.has_retries}
            onChange={(e) => update("has_retries", e.target.checked ? "true" : null)}
          />
          Has retries
        </label>
        {filtered && (
          <button
            type="button"
            className="link-button"
            onClick={() => {
              setSearch("");
              setParams(new URLSearchParams(), { replace: true });
            }}
          >
            Clear filters
          </button>
        )}
      </form>

      {error ? (
        <ErrorState error={error} />
      ) : !loading && traces.length === 0 ? (
        <div className="empty">
          {filtered ? (
            <p>No traces match these filters.</p>
          ) : (
            <>
              <p>No traces yet.</p>
              <p className="muted">
                Run the demo agent: <code>make demo</code>
              </p>
            </>
          )}
        </div>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Trace</th>
                <th>Status</th>
                <th>Started</th>
                <th className="num">Duration</th>
                <th className="num">Spans</th>
                <th className="num">Errors</th>
                <th className="num">Retries</th>
                <th className="num">Tokens in / out</th>
                <th className="num">Est. cost</th>
              </tr>
            </thead>
            <tbody>
              {traces.map((t) => (
                <tr key={t.trace_id}>
                  <td>
                    <Link to={`/traces/${t.trace_id}`} className="trace-link">
                      {t.name ?? <span className="muted">(root span pending)</span>}
                    </Link>
                    <div className="sub">
                      {t.service_name} · <code>{t.trace_id.slice(0, 12)}</code>
                    </div>
                  </td>
                  <td>
                    <StatusBadge status={t.status} />
                  </td>
                  <td title={formatTime(t.start_time)}>{relativeTime(t.start_time)}</td>
                  <td className="num">{formatDuration(t.duration_ms)}</td>
                  <td className="num">{t.span_count}</td>
                  <td className={`num${t.error_count ? " text-error" : ""}`}>{t.error_count}</td>
                  <td className={`num${t.retry_count ? " text-warn" : ""}`}>{t.retry_count}</td>
                  <td className="num">
                    {formatNumber(t.input_tokens)} / {formatNumber(t.output_tokens)}
                  </td>
                  <td className="num" title={t.unpriced_llm_calls ? `${t.unpriced_llm_calls} LLM calls without a price` : undefined}>
                    {formatCost(t.estimated_cost_usd)}
                    {t.unpriced_llm_calls > 0 && <span className="text-warn"> ⚠</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {cursor && !error && (
        <div className="load-more">
          <button className="button" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </div>
  );
}
