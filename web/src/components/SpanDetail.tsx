import { type ReactNode, useState } from "react";
import { formatCost, formatDuration, formatNumber, offsetMs, prettyPayload } from "../format";
import type { Span } from "../types";
import { KindTag, StatusBadge } from "./Badges";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

function Payload({ title, text }: { title: string; text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <section className="detail-section">
      <div className="section-head">
        <h4>{title}</h4>
        <button
          className="link-button"
          onClick={() => {
            navigator.clipboard?.writeText(text).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1200);
            });
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="payload">{prettyPayload(text)}</pre>
    </section>
  );
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export function SpanDetail({ span, traceStartMs }: { span: Span; traceStartMs: number }) {
  const exception = span.events.find((e) => e.name === "exception");
  const retries = span.events.filter((e) => e.name === "retry");
  const otherEvents = span.events.filter((e) => e.name !== "exception" && e.name !== "retry");
  const attributes = Object.entries(span.attributes).sort(([a], [b]) => a.localeCompare(b));
  const isLlm = span.model || span.input_tokens != null || span.output_tokens != null;

  return (
    <div className="span-detail">
      <header className="span-detail-head">
        <div className="span-title">
          <KindTag kind={span.kind} />
          <h3>{span.name}</h3>
        </div>
        <StatusBadge status={span.status} />
      </header>

      <dl className="fields">
        <Field label="Duration">{formatDuration(span.duration_ms)}</Field>
        <Field label="Starts at">+{formatDuration(offsetMs(span.start_time, traceStartMs))}</Field>
        {span.service_name && <Field label="Service">{span.service_name}</Field>}
        <Field label="Span ID">
          <code>{span.span_id}</code>
        </Field>
        {span.retry_count > 0 && <Field label="Retries">{span.retry_count}</Field>}
      </dl>

      {span.status === "error" && (
        <section className="detail-section error-box">
          <h4>{span.error_type ?? "Error"}</h4>
          {span.status_message && <p>{span.status_message}</p>}
          {typeof exception?.attributes["exception.stacktrace"] === "string" && (
            <details>
              <summary>Stack trace</summary>
              <pre className="payload">{exception.attributes["exception.stacktrace"] as string}</pre>
            </details>
          )}
        </section>
      )}

      {isLlm && (
        <section className="detail-section">
          <h4>Model call</h4>
          <dl className="fields">
            {span.model && <Field label="Model">{span.model}</Field>}
            {span.provider && <Field label="Provider">{span.provider}</Field>}
            {span.input_tokens != null && <Field label="Input tokens">{formatNumber(span.input_tokens)}</Field>}
            {span.output_tokens != null && <Field label="Output tokens">{formatNumber(span.output_tokens)}</Field>}
            {!!span.cached_input_tokens && <Field label="Cached input">{formatNumber(span.cached_input_tokens)}</Field>}
            {(span.input_tokens != null || span.output_tokens != null) && (
              <Field label="Est. cost">
                {span.estimated_cost_usd != null ? (
                  formatCost(span.estimated_cost_usd)
                ) : (
                  <span className="muted">no price configured</span>
                )}
              </Field>
            )}
          </dl>
        </section>
      )}

      {typeof span.attributes["obzerve.replay.key"] === "string" && (
        <section className="detail-section">
          <h4>Replay</h4>
          <dl className="fields">
            <Field label="Key">
              <code>{String(span.attributes["obzerve.replay.key"])}</code>
            </Field>
            {span.attributes["obzerve.replay.source"] !== undefined && (
              <Field label="Source">
                {span.attributes["obzerve.replay.source"] === "recorded" ? "served from recording" : "ran live"}
              </Field>
            )}
            {span.attributes["obzerve.replay.input_matched"] === false && (
              <Field label="Input">
                <span className="text-warn">⚠ differs from the recording (replay diverged)</span>
              </Field>
            )}
            {span.attributes["obzerve.replay.complete"] === false && (
              <Field label="Recording">
                <span className="text-warn">
                  incomplete: {String(span.attributes["obzerve.replay.incomplete_reason"] ?? "unknown")}
                </span>
              </Field>
            )}
            {span.attributes["obzerve.replay.side_effect"] === true && (
              <Field label="Side effect">yes: never re-run live by a replay</Field>
            )}
          </dl>
        </section>
      )}

      {retries.length > 0 && (
        <section className="detail-section">
          <h4>Retries</h4>
          <ol className="event-list">
            {retries.map((e, i) => (
              <li key={i}>
                Attempt {String(e.attributes["obzerve.retry.attempt"] ?? i + 1)} failed
                {e.attributes["error.type"] ? `: ${String(e.attributes["error.type"])}` : ""}
                {e.attributes["exception.message"] ? (
                  <span className="muted"> ({String(e.attributes["exception.message"])})</span>
                ) : null}
              </li>
            ))}
          </ol>
        </section>
      )}

      {span.input && <Payload title="Input" text={span.input} />}
      {span.output && <Payload title="Output" text={span.output} />}

      {otherEvents.length > 0 && (
        <section className="detail-section">
          <h4>Events</h4>
          <ul className="event-list">
            {otherEvents.map((e, i) => (
              <li key={i}>
                <strong>{e.name}</strong>{" "}
                <span className="muted">+{formatDuration(offsetMs(e.time, traceStartMs))}</span>
                {Object.keys(e.attributes).length > 0 && (
                  <code className="event-attrs">{JSON.stringify(e.attributes)}</code>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {attributes.length > 0 && (
        <section className="detail-section">
          <h4>Attributes</h4>
          <table className="attr-table">
            <tbody>
              {attributes.map(([k, v]) => (
                <tr key={k}>
                  <th>{k}</th>
                  <td>{formatValue(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
