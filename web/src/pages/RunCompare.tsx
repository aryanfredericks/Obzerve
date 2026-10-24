import { type FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, useApi } from "../api";
import { ErrorState } from "../components/ErrorState";
import { formatCost, formatDuration } from "../format";
import type { GateResult, RunSummary } from "../types";

const CHANGE_LABEL = {
  regression: "regression",
  fix: "fixed",
  still_passing: "passing",
  still_failing: "still failing",
  added: "new case",
  removed: "removed",
} as const;

function pct(n: number | null): string {
  return n === null ? "–" : `${n > 0 ? "+" : ""}${n.toFixed(1)}%`;
}

function Metric({
  label,
  base,
  cand,
  delta,
  better,
}: {
  label: string;
  base: string;
  cand: string;
  delta?: string;
  better?: boolean | null;
}) {
  return (
    <tr>
      <th>{label}</th>
      <td className="num">{base}</td>
      <td className="num">{cand}</td>
      <td className={`num ${better === true ? "text-ok" : better === false ? "text-error" : "muted"}`}>
        {delta ?? ""}
      </td>
    </tr>
  );
}

function prompt(run: RunSummary) {
  return run.prompt ? `${run.prompt.name} v${run.prompt.version}` : "–";
}

export function RunCompare() {
  const { baseline = "", candidate = "" } = useParams();
  const { data: cmp, error } = useApi(() => api.compareRuns(baseline, candidate), [baseline, candidate]);
  const [criteria, setCriteria] = useState({ max_regressions: "0", min_pass_rate: "", max_pass_rate_drop: "0" });
  const [gate, setGate] = useState<GateResult | null>(null);
  const [gateError, setGateError] = useState<string | null>(null);

  if (error) return <div className="page"><ErrorState error={error} /></div>;
  if (!cmp) return <div className="page muted">Loading comparison…</div>;
  const b = cmp.baseline;
  const c = cmp.candidate;

  const runGate = async (e: FormEvent) => {
    e.preventDefault();
    setGateError(null);
    try {
      setGate(
        await api.gate(baseline, candidate, {
          max_regressions: criteria.max_regressions || undefined,
          min_pass_rate: criteria.min_pass_rate ? Number(criteria.min_pass_rate) / 100 : undefined,
          max_pass_rate_drop: criteria.max_pass_rate_drop ? Number(criteria.max_pass_rate_drop) / 100 : undefined,
        }),
      );
    } catch (err) {
      setGateError((err as Error).message);
    }
  };

  const tokenDelta =
    b.output_tokens_per_case && c.output_tokens_per_case !== null
      ? ((c.output_tokens_per_case - b.output_tokens_per_case) / b.output_tokens_per_case) * 100
      : null;

  return (
    <div className="page">
      <Link to="/evals" className="back">← Evaluations</Link>
      <div className="page-head">
        <h1>
          {b.label} → {c.label}
        </h1>
      </div>

      {!cmp.comparable && (
        <div className="callout warn-callout">
          <strong>Not directly comparable:</strong> {cmp.reasons.join("; ")}. Differences may come from the setup, not
          the change.
        </div>
      )}

      <div className="compare-layout">
        <section className="panel">
          <div className="metric-wrap">
          <table className="metric-table">
            <thead>
              <tr>
                <th />
                <th className="num">
                  Baseline
                  <div className="sub">{b.label}</div>
                </th>
                <th className="num">
                  Candidate
                  <div className="sub">{c.label}</div>
                </th>
                <th className="num">Change</th>
              </tr>
            </thead>
            <tbody>
              <Metric label="Prompt" base={prompt(b)} cand={prompt(c)} />
              <Metric
                label="Pass rate"
                base={`${Math.round(b.pass_rate * 100)}% (${b.passed}/${b.cases})`}
                cand={`${Math.round(c.pass_rate * 100)}% (${c.passed}/${c.cases})`}
                delta={`${cmp.pass_rate_delta > 0 ? "+" : ""}${Math.round(cmp.pass_rate_delta * 100)} pts`}
                better={cmp.pass_rate_delta === 0 ? null : cmp.pass_rate_delta > 0}
              />
              <Metric
                label="Est. cost / case"
                base={b.estimated_cost_per_case_usd === null ? "–" : formatCost(b.estimated_cost_per_case_usd)}
                cand={c.estimated_cost_per_case_usd === null ? "–" : formatCost(c.estimated_cost_per_case_usd)}
                delta={pct(cmp.cost_per_case_delta_pct)}
                better={cmp.cost_per_case_delta_pct ? cmp.cost_per_case_delta_pct < 0 : null}
              />
              <Metric
                label="Output tokens / case"
                base={b.output_tokens_per_case === null ? "–" : String(Math.round(b.output_tokens_per_case))}
                cand={c.output_tokens_per_case === null ? "–" : String(Math.round(c.output_tokens_per_case))}
                delta={pct(tokenDelta)}
                better={tokenDelta ? tokenDelta < 0 : null}
              />
              <Metric
                label="p95 latency"
                base={b.latency_p95_ms === null ? "–" : formatDuration(b.latency_p95_ms)}
                cand={c.latency_p95_ms === null ? "–" : formatDuration(c.latency_p95_ms)}
                delta={pct(cmp.latency_p95_delta_pct)}
                better={cmp.latency_p95_delta_pct ? cmp.latency_p95_delta_pct < 0 : null}
              />
            </tbody>
          </table>
          </div>
          <p className="chart-note">
            Same dataset <strong>{b.dataset}</strong> v{b.dataset_version} · {b.evaluator_version}. Costs are estimates.
          </p>
        </section>

        <section className="panel gate-panel">
          <h2>Release gate</h2>
          <form onSubmit={runGate} className="gate-form">
            <label>
              Max regressions
              <input type="number" min={0} value={criteria.max_regressions} onChange={(e) => setCriteria({ ...criteria, max_regressions: e.target.value })} />
            </label>
            <label>
              Min pass rate (%)
              <input type="number" min={0} max={100} value={criteria.min_pass_rate} onChange={(e) => setCriteria({ ...criteria, min_pass_rate: e.target.value })} />
            </label>
            <label>
              Max pass-rate drop (pts)
              <input type="number" min={0} max={100} value={criteria.max_pass_rate_drop} onChange={(e) => setCriteria({ ...criteria, max_pass_rate_drop: e.target.value })} />
            </label>
            <button className="button primary" type="submit">Evaluate gate</button>
          </form>
          {gateError && <p className="text-error">{gateError}</p>}
          {gate && (
            <div className={`gate-result ${gate.passed ? "gate-pass" : "gate-fail"}`}>
              <strong>{gate.passed ? "✓ Gate passed" : "✗ Gate failed"}</strong>
              <ul>
                {gate.checks.map((check) => (
                  <li key={check.name}>
                    <span className={check.passed ? "text-ok" : "text-error"}>{check.passed ? "✓" : "✗"}</span>{" "}
                    <strong>{check.name.replaceAll("_", " ")}</strong> — {check.detail}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="chart-note">
            In CI: <code>obzerve eval gate {baseline.slice(0, 8)}… {candidate.slice(0, 8)}…</code> exits 1 when the gate fails.
          </p>
        </section>
      </div>

      <h2 className="section-title">
        Cases <span className="muted">· {cmp.regressions} regression{cmp.regressions === 1 ? "" : "s"}, {cmp.fixes} fixed</span>
      </h2>
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Case</th>
              <th>Change</th>
              <th>Why the candidate failed</th>
              <th>Traces</th>
            </tr>
          </thead>
          <tbody>
            {cmp.cases.map((d) => (
              <tr key={d.case_id} className={`change-${d.change}`}>
                <td>{d.case_name}</td>
                <td>
                  <span className={`badge change-badge change-${d.change}`}>{CHANGE_LABEL[d.change]}</span>
                </td>
                <td className="wrap">{d.failed_assertions.join("; ") || <span className="muted">—</span>}</td>
                <td>
                  {d.baseline_trace_id && d.candidate_trace_id ? (
                    <Link to={`/traces/${d.baseline_trace_id}/compare/${d.candidate_trace_id}`}>compare</Link>
                  ) : (
                    "–"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
