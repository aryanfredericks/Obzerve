import { Fragment, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, useApi } from "../api";
import { KindTag, StatusBadge } from "../components/Badges";
import { ErrorState } from "../components/ErrorState";
import { formatDuration, formatNumber, prettyPayload } from "../format";
import type { StepSide, TraceSummary } from "../types";

function Side({ title, trace }: { title: string; trace: TraceSummary }) {
  return (
    <div className="compare-side">
      <div className="compare-side-head">
        <span className="muted">{title}</span>
        <StatusBadge status={trace.status} />
      </div>
      <Link to={`/traces/${trace.trace_id}`} className="trace-link">
        {trace.name} <code>{trace.trace_id.slice(0, 12)}</code>
      </Link>
      <div className="sub">
        {trace.replay_mode ? `${trace.replay_mode} replay` : "original run"}
        {trace.replay_label && ` · ${trace.replay_label}`}
        {trace.prompt_version && ` · prompt v${trace.prompt_version}`}
      </div>
      <dl className="fields compact">
        <dt>Duration</dt>
        <dd>{formatDuration(trace.duration_ms)}</dd>
        <dt>Errors</dt>
        <dd>{trace.error_count}</dd>
        <dt>Tokens in / out</dt>
        <dd>
          {formatNumber(trace.input_tokens)} / {formatNumber(trace.output_tokens)}
        </dd>
      </dl>
    </div>
  );
}

function StepCell({ side }: { side: StepSide | null }) {
  if (!side) return <td className="muted">not called</td>;
  return (
    <td>
      <StatusBadge status={side.status} /> <span className="num-inline">{formatDuration(side.duration_ms)}</span>
      {side.source && (
        <span className={`source source-${side.source}`}>{side.source === "recorded" ? "recorded" : "live"}</span>
      )}
      {side.input_matched === false && <span className="text-warn"> input diverged</span>}
      {side.error_type && <div className="sub text-error">{side.error_type}</div>}
    </td>
  );
}

function Payload({ side }: { side: StepSide | null }) {
  if (!side) return <div className="muted">—</div>;
  const text = side.status === "error" ? `${side.error_type}: ${side.status_message ?? ""}` : side.output;
  return <pre className="payload">{text ? prettyPayload(text) : "(no output recorded)"}</pre>;
}

export function TraceCompare() {
  const { a = "", b = "" } = useParams();
  const { data, error, loading } = useApi(() => api.traceComparison(a, b), [a, b]);
  const [open, setOpen] = useState<string | null>(null);

  if (error) return <div className="page"><ErrorState error={error} /></div>;
  if (loading && !data) return <div className="page muted">Loading comparison…</div>;
  if (!data) return null;

  const changed = data.steps.filter((s) => s.output_changed).length;
  return (
    <div className="page">
      <Link to={`/traces/${a}`} className="back">← Original trace</Link>
      <div className="page-head">
        <h1>Compare runs</h1>
      </div>

      <div className="compare-grid">
        <Side title="A" trace={data.a} />
        <Side title="B" trace={data.b} />
      </div>

      <section className="panel compare-outcome">
        <div className="replay-head">
          <h2>Outcome</h2>
          <span className={`badge ${data.output_changed ? "status-unset changed" : "status-ok"}`}>
            {data.output_changed ? "changed" : "identical"}
          </span>
          <span className="muted">
            {changed} of {data.steps.length} recorded steps changed output
          </span>
        </div>
        <div className="compare-grid">
          <pre className="payload tall">{data.a_output ? prettyPayload(data.a_output) : data.a.status === "error" ? "(failed: no output)" : "(no output)"}</pre>
          <pre className="payload tall">{data.b_output ? prettyPayload(data.b_output) : data.b.status === "error" ? "(failed: no output)" : "(no output)"}</pre>
        </div>
      </section>

      <section className="panel">
        <div className="table-wrap flush no-top">
          <table className="data-table">
            <thead>
              <tr>
                <th>Step</th>
                <th>A</th>
                <th>B</th>
                <th>Output</th>
              </tr>
            </thead>
            <tbody>
              {data.steps.map((s) => (
                <Fragment key={s.key}>
                  <tr className="clickable" onClick={() => setOpen(open === s.key ? null : s.key)} aria-expanded={open === s.key}>
                    <td>
                      <span className="trace-link plain">
                        <KindTag kind={s.kind} /> <code>{s.key}</code>
                      </span>
                    </td>
                    <StepCell side={s.a} />
                    <StepCell side={s.b} />
                    <td>
                      {!s.a || !s.b ? (
                        <span className="text-warn">only in {s.a ? "A" : "B"}</span>
                      ) : s.output_changed ? (
                        <span className="text-warn">changed</span>
                      ) : (
                        <span className="muted">same</span>
                      )}
                      <span className="muted"> {open === s.key ? "▾" : "▸"}</span>
                    </td>
                  </tr>
                  {open === s.key && (
                    <tr className="expanded">
                      <td colSpan={4}>
                        <div className="compare-grid">
                          <Payload side={s.a} />
                          <Payload side={s.b} />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
