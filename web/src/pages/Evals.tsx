import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, useApi } from "../api";
import { PassBadge, ReplayBadge } from "../components/Badges";
import { ErrorState } from "../components/ErrorState";
import { formatCost, formatDuration, formatTime, relativeTime } from "../format";
import type { AssertionSpec, RunSummary } from "../types";

export function describeAssertion(a: AssertionSpec): string {
  switch (a.type) {
    case "no_error":
      return "completes without error";
    case "contains_all":
      return `contains all of ${a.values.map((v) => `“${v}”`).join(", ")}`;
    case "contains_any":
      return `contains ${a.min_matches && a.min_matches > 1 ? `at least ${a.min_matches} of` : "any of"} ${a.values
        .map((v) => `“${v}”`)
        .join(", ")}`;
    case "not_contains":
      return `contains none of ${a.values.map((v) => `“${v}”`).join(", ")}`;
    case "regex":
      return `${a.must_match === false ? "does not match" : "matches"} /${a.pattern}/i`;
    case "max_latency_ms":
      return `finishes within ${formatDuration(a.value)}`;
    case "max_output_chars":
      return `output at most ${a.value} characters`;
  }
}

function promptLabel(run: RunSummary): string {
  return run.prompt ? `${run.prompt.name} v${run.prompt.version}` : "–";
}

function RunsTable({
  runs,
  selected,
  onToggle,
}: {
  runs: RunSummary[];
  selected?: string[];
  onToggle?: (id: string) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            {onToggle && <th aria-label="Select" />}
            <th>Run</th>
            <th>Dataset</th>
            <th>Prompt</th>
            <th className="num">Pass rate</th>
            <th className="num">p95 latency</th>
            <th className="num">Output tokens / case</th>
            <th className="num">Est. cost / case</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id}>
              {onToggle && (
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Select ${r.label}`}
                    checked={selected?.includes(r.id) ?? false}
                    onChange={() => onToggle(r.id)}
                  />
                </td>
              )}
              <td>
                <Link to={`/evals/runs/${r.id}`} className="trace-link">
                  {r.label}
                </Link>
                <div className="sub">{r.replay_policy} replay</div>
              </td>
              <td>
                {r.dataset} <span className="muted">v{r.dataset_version}</span>
              </td>
              <td>{promptLabel(r)}</td>
              <td className={`num${r.pass_rate < 1 ? " text-error" : ""}`}>
                {Math.round(r.pass_rate * 100)}% <span className="muted">({r.passed}/{r.cases})</span>
              </td>
              <td className="num">{r.latency_p95_ms === null ? "–" : formatDuration(r.latency_p95_ms)}</td>
              <td className="num">{r.output_tokens_per_case === null ? "–" : Math.round(r.output_tokens_per_case)}</td>
              <td className="num">
                {r.estimated_cost_per_case_usd === null ? "–" : formatCost(r.estimated_cost_per_case_usd)}
              </td>
              <td title={formatTime(r.started_at)}>{relativeTime(r.started_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EvalsHome() {
  const datasets = useApi(() => api.datasets(), []);
  const runs = useApi(() => api.evalRuns(), []);
  const [selected, setSelected] = useState<string[]>([]);
  const navigate = useNavigate();

  const toggle = (id: string) =>
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev.slice(-1), id]));
  // Compare older (baseline) against newer (candidate).
  const ordered = runs.data?.filter((r) => selected.includes(r.id)).sort((x, y) => x.started_at.localeCompare(y.started_at));

  const error = datasets.error ?? runs.error;
  if (error) return <div className="page"><ErrorState error={error} /></div>;

  return (
    <div className="page">
      <div className="page-head">
        <h1>Evaluations</h1>
      </div>
      <p className="intro muted">
        Regression cases are made from traces (open a trace → <em>Turn into a regression case</em>). An eval run
        replays every case of a dataset against a candidate agent, prompt, or model and checks its assertions.
      </p>

      <h2 className="section-title">Datasets</h2>
      {datasets.data?.length === 0 ? (
        <div className="empty">
          <p>No datasets yet.</p>
          <p className="muted">Create one by turning a trace into a regression case.</p>
        </div>
      ) : (
        <div className="card-row">
          {datasets.data?.map((d) => (
            <Link key={d.id} to={`/evals/datasets/${encodeURIComponent(d.name)}`} className="card">
              <strong>{d.name}</strong>
              <span className="muted">
                v{d.version} · {d.active_cases} case{d.active_cases === 1 ? "" : "s"} · {d.runs} run{d.runs === 1 ? "" : "s"}
              </span>
            </Link>
          ))}
        </div>
      )}

      <div className="section-head">
        <h2 className="section-title">Runs</h2>
        <button
          className="button primary"
          disabled={!ordered || ordered.length !== 2}
          onClick={() => ordered && navigate(`/evals/compare/${ordered[0].id}/${ordered[1].id}`)}
        >
          Compare selected ({selected.length}/2)
        </button>
      </div>
      {runs.data?.length === 0 ? (
        <div className="empty">
          <p>No eval runs yet.</p>
          <p className="muted">
            <code>uv run research-agent eval &lt;dataset&gt; --label baseline</code>
          </p>
        </div>
      ) : (
        runs.data && <RunsTable runs={runs.data} selected={selected} onToggle={toggle} />
      )}
    </div>
  );
}

export function DatasetView() {
  const { name = "" } = useParams();
  const detail = useApi(() => api.dataset(name), [name]);
  const runs = useApi(() => api.evalRuns(name), [name]);
  const [archiving, setArchiving] = useState<string | null>(null);

  if (detail.error) return <div className="page"><ErrorState error={detail.error} /></div>;
  if (!detail.data) return <div className="page muted">Loading…</div>;
  const { dataset, cases } = detail.data;

  const archive = async (caseId: string) => {
    if (!window.confirm("Archive this case? Runs made before stay unchanged; the dataset version increases.")) return;
    setArchiving(caseId);
    try {
      await api.archiveCase(name, caseId);
      detail.reload();
    } finally {
      setArchiving(null);
    }
  };

  return (
    <div className="page">
      <Link to="/evals" className="back">← Evaluations</Link>
      <div className="page-head">
        <div>
          <h1>{dataset.name}</h1>
          <div className="sub">
            version {dataset.version} · {cases.length} active case{cases.length === 1 ? "" : "s"}
            {dataset.description && ` · ${dataset.description}`}
          </div>
        </div>
      </div>

      <div className="case-list">
        {cases.map((c) => (
          <article key={c.id} className="panel case">
            <header>
              <h3>{c.name}</h3>
              <span className="mode">{c.replay_policy}</span>
              <ReplayBadge value={c.replayability} />
              <button className="link-button danger" onClick={() => archive(c.id)} disabled={archiving === c.id}>
                Archive
              </button>
            </header>
            {c.notes && <p className="muted">{c.notes}</p>}
            <ul className="assertions">
              {c.assertions.map((a, i) => (
                <li key={i}>{describeAssertion(a)}</li>
              ))}
            </ul>
            <div className="sub">
              {c.recordings} recorded operations · added in v{c.added_in_version}
              {c.source_trace_id && (
                <>
                  {" "}· from trace <Link to={`/traces/${c.source_trace_id}`}><code>{c.source_trace_id.slice(0, 12)}</code></Link>
                </>
              )}
            </div>
          </article>
        ))}
      </div>

      <h2 className="section-title">Runs on this dataset</h2>
      {runs.data && runs.data.length > 0 ? <RunsTable runs={runs.data} /> : <p className="muted">No runs yet.</p>}
    </div>
  );
}

export function RunView() {
  const { id = "" } = useParams();
  const { data, error } = useApi(() => api.evalRun(id), [id]);
  if (error) return <div className="page"><ErrorState error={error} /></div>;
  if (!data) return <div className="page muted">Loading…</div>;
  const { run, results } = data;

  return (
    <div className="page">
      <Link to="/evals" className="back">← Evaluations</Link>
      <div className="page-head">
        <div>
          <h1>{run.label}</h1>
          <div className="sub">
            <Link to={`/evals/datasets/${encodeURIComponent(run.dataset)}`}>{run.dataset}</Link> v{run.dataset_version} ·
            prompt {promptLabel(run)} · {run.replay_policy} replay · {run.evaluator_version} · {formatTime(run.started_at)}
          </div>
        </div>
      </div>
      <div className="stats">
        <div className={`stat${run.pass_rate < 1 ? " stat-error" : ""}`}>
          <span>Pass rate</span>
          <strong>{Math.round(run.pass_rate * 100)}%</strong>
          <span className="stat-detail">{run.passed} of {run.cases} cases</span>
        </div>
        <div className="stat">
          <span>p95 latency</span>
          <strong>{run.latency_p95_ms === null ? "–" : formatDuration(run.latency_p95_ms)}</strong>
        </div>
        <div className="stat">
          <span>Output tokens / case</span>
          <strong>{run.output_tokens_per_case === null ? "–" : Math.round(run.output_tokens_per_case)}</strong>
        </div>
        <div className="stat">
          <span>Est. cost / case</span>
          <strong>{run.estimated_cost_per_case_usd === null ? "–" : formatCost(run.estimated_cost_per_case_usd)}</strong>
        </div>
      </div>
      {Object.keys(run.config).length > 0 && (
        <p className="sub config-line">
          config: {Object.entries(run.config).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" · ")}
        </p>
      )}

      <div className="case-list">
        {results.map((r) => (
          <article key={r.case_id} className={`panel case ${r.passed ? "" : "case-failed"}`}>
            <header>
              <PassBadge passed={r.passed} />
              <h3>{r.case_name}</h3>
              <span className="muted">{formatDuration(r.latency_ms)}</span>
              {r.trace_id && <Link to={`/traces/${r.trace_id}`}>trace</Link>}
            </header>
            <ul className="assertions">
              {r.assertions.map((a, i) => (
                <li key={i} className={a.passed ? "" : "text-error"}>
                  {a.passed ? "✓" : "✗"} {a.type}
                  {a.detail && <span className="muted"> — {a.detail}</span>}
                </li>
              ))}
            </ul>
            {(r.error || r.output) && (
              <details>
                <summary>{r.error ? "Error" : "Output"}</summary>
                <pre className="payload">{r.error ?? r.output}</pre>
              </details>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}
