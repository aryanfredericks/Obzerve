import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, useApi } from "../api";
import { KindTag } from "../components/Badges";
import { ErrorState } from "../components/ErrorState";
import { formatDuration, formatTime, relativeTime } from "../format";
import type { SpanSummary } from "../types";

export function Errors() {
  const [params, setParams] = useSearchParams();
  const errorType = params.get("error_type");
  const view = params.get("view") === "retries" ? "retries" : "errors";
  const [extra, setExtra] = useState<SpanSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);

  const types = useApi(() => api.errorTypes(), []);
  const query = view === "retries" ? { has_retries: true } : { status: "error", error_type: errorType };
  const spans = useApi(async () => {
    const page = await api.spans(query);
    setExtra([]);
    setCursor(page.next_cursor);
    return page;
  }, [view, errorType]);

  const items = [...(spans.data?.items ?? []), ...extra];
  const select = (next: Record<string, string>) => setParams(next, { replace: true });

  return (
    <div className="page">
      <div className="page-head">
        <h1>Errors &amp; retries</h1>
      </div>

      <div className="chips">
        <button className={`chip${view === "errors" && !errorType ? " active" : ""}`} onClick={() => select({})}>
          All errors
        </button>
        {types.data?.map((t) => (
          <button
            key={t.error_type}
            className={`chip${errorType === t.error_type ? " active" : ""}`}
            onClick={() => select({ error_type: t.error_type })}
          >
            {t.error_type} <span className="chip-count">{t.count}</span>
          </button>
        ))}
        <button className={`chip${view === "retries" ? " active" : ""}`} onClick={() => select({ view: "retries" })}>
          Retried operations
        </button>
      </div>

      {spans.error ? (
        <ErrorState error={spans.error} />
      ) : !spans.loading && items.length === 0 ? (
        <div className="empty"><p>{view === "retries" ? "No retried operations." : "No errors recorded."}</p></div>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Operation</th>
                <th>{view === "retries" ? "Retries" : "Error"}</th>
                <th>When</th>
                <th className="num">Duration</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.trace_id + s.span_id}>
                  <td>
                    <Link to={`/traces/${s.trace_id}?span=${s.span_id}`} className="trace-link">
                      <KindTag kind={s.kind} /> {s.name}
                    </Link>
                    <div className="sub">
                      {s.model && <>{s.model} · </>}
                      <code>{s.trace_id.slice(0, 12)}</code>
                    </div>
                  </td>
                  <td>
                    {view === "retries" ? (
                      <span className="text-warn">{s.retry_count}</span>
                    ) : (
                      <>
                        <span className="text-error">{s.error_type ?? "error"}</span>
                        {s.status_message && <div className="sub">{s.status_message}</div>}
                      </>
                    )}
                  </td>
                  <td title={formatTime(s.start_time)}>{relativeTime(s.start_time)}</td>
                  <td className="num">{formatDuration(s.duration_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {cursor && (
        <div className="load-more">
          <button
            className="button"
            onClick={async () => {
              const page = await api.spans(query, cursor);
              setExtra((prev) => [...prev, ...page.items]);
              setCursor(page.next_cursor);
            }}
          >
            Load more
          </button>
        </div>
      )}
    </div>
  );
}
