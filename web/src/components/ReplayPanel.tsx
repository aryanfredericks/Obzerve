import { type FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, useApi } from "../api";
import { formatDuration, relativeTime } from "../format";
import type { AssertionSpec, TraceSummary } from "../types";
import { ReplayBadge, StatusBadge } from "./Badges";

const POLICY_HELP: Record<string, string> = {
  recorded: "Re-run the recorded execution exactly, including its failures. Nothing external is called.",
  "llm-live": "Run the model live against the recorded tool and retrieval results: tests prompt/model changes.",
  live: "Run everything live (side-effecting operations stay blocked).",
};

function splitTerms(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

function CopyBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="copy-block">
      <pre>{text}</pre>
      <button
        className="link-button"
        onClick={() =>
          navigator.clipboard?.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          })
        }
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function CaseForm({ trace }: { trace: TraceSummary }) {
  const datasets = useApi(() => api.datasets(), []);
  const [dataset, setDataset] = useState("regressions");
  const [name, setName] = useState(trace.status === "error" ? `${trace.name} failure` : `${trace.name}`);
  const [policy, setPolicy] = useState(trace.status === "error" ? "recorded" : "llm-live");
  const [noError, setNoError] = useState(true);
  const [containsAny, setContainsAny] = useState("");
  const [notContains, setNotContains] = useState("");
  const [regex, setRegex] = useState("");
  const [maxLatency, setMaxLatency] = useState("");
  const [notes, setNotes] = useState("");
  const [state, setState] = useState<{ busy: boolean; error?: string; created?: string }>({ busy: false });

  const assertions: AssertionSpec[] = [];
  if (noError) assertions.push({ type: "no_error" });
  if (splitTerms(containsAny).length) assertions.push({ type: "contains_any", values: splitTerms(containsAny) });
  if (splitTerms(notContains).length) assertions.push({ type: "not_contains", values: splitTerms(notContains) });
  if (regex.trim()) assertions.push({ type: "regex", pattern: regex.trim() });
  if (Number(maxLatency) > 0) assertions.push({ type: "max_latency_ms", value: Number(maxLatency) });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setState({ busy: true });
    try {
      await api.createCase(dataset.trim(), {
        trace_id: trace.trace_id,
        name: name.trim(),
        replay_policy: policy,
        assertions,
        notes: notes.trim() || undefined,
      });
      setState({ busy: false, created: dataset.trim() });
    } catch (err) {
      setState({ busy: false, error: (err as Error).message });
    }
  };

  if (state.created) {
    return (
      <p className="form-success">
        ✓ Added to <Link to={`/evals/datasets/${encodeURIComponent(state.created)}`}>{state.created}</Link>. Run the
        dataset against a candidate to check it.
      </p>
    );
  }

  return (
    <form className="case-form" onSubmit={submit}>
      <div className="form-row">
        <label>
          Dataset
          <input list="dataset-names" value={dataset} onChange={(e) => setDataset(e.target.value)} required />
          <datalist id="dataset-names">
            {datasets.data?.map((d) => <option key={d.id} value={d.name} />)}
          </datalist>
        </label>
        <label>
          Case name
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={200} />
        </label>
      </div>
      <label>
        Replay as
        <select value={policy} onChange={(e) => setPolicy(e.target.value)}>
          <option value="recorded">recorded</option>
          <option value="llm-live">llm-live</option>
          <option value="live">live</option>
        </select>
        <span className="hint">{POLICY_HELP[policy]}</span>
      </label>
      <fieldset>
        <legend>Pass when</legend>
        <label className="check">
          <input type="checkbox" checked={noError} onChange={(e) => setNoError(e.target.checked)} />
          the run completes without raising
        </label>
        <label>
          output contains any of <span className="hint">comma-separated, case-insensitive</span>
          <input value={containsAny} onChange={(e) => setContainsAny(e.target.value)} placeholder="faithfulness, groundedness" />
        </label>
        <label>
          output contains none of
          <input value={notContains} onChange={(e) => setNotContains(e.target.value)} />
        </label>
        <label>
          output matches regex
          <input value={regex} onChange={(e) => setRegex(e.target.value)} placeholder="does not contain|insufficient" />
        </label>
        <label>
          finishes within (ms)
          <input type="number" min={1} value={maxLatency} onChange={(e) => setMaxLatency(e.target.value)} />
        </label>
      </fieldset>
      <label>
        Notes
        <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} placeholder="What went wrong, and what should happen instead" />
      </label>
      {state.error && <p className="text-error">{state.error}</p>}
      <button className="button primary" type="submit" disabled={state.busy || assertions.length === 0}>
        {state.busy ? "Creating…" : "Create regression case"}
      </button>
      {assertions.length === 0 && <span className="hint"> Add at least one condition.</span>}
    </form>
  );
}

export function ReplayPanel({ trace }: { trace: TraceSummary }) {
  const replays = useApi(() => api.replays(trace.trace_id), [trace.trace_id]);
  const [showForm, setShowForm] = useState(false);
  const canReplay = trace.replayability && trace.replayability !== "non_replayable";
  const sdkSnippet = `bundle = ObzerveClient(ENDPOINT, API_KEY).replay_bundle("${trace.trace_id}")
with observer.replay(bundle, RECORDED):   # or LLM_LIVE / LIVE
    agent.run(**bundle.root_input)`;

  return (
    <section className="panel replay-panel">
      <div className="replay-head">
        <h2>Replay &amp; regression testing</h2>
        {trace.replayability && <ReplayBadge value={trace.replayability} />}
        {trace.replayability && (
          <span className="muted">
            {trace.replay_points} recorded operation{trace.replay_points === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {trace.replay_reasons && trace.replay_reasons.length > 0 && (
        <ul className="reasons">
          {trace.replay_reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      {canReplay && (
        <div className="replay-grid">
          <div>
            <h4>Replay it</h4>
            {trace.name === "research_agent" && (
              <CopyBlock text={`uv run research-agent replay ${trace.trace_id} --mode recorded`} />
            )}
            <details>
              <summary>From your own code (Python SDK)</summary>
              <CopyBlock text={sdkSnippet} />
            </details>
          </div>
          <div>
            <h4>Replays of this trace</h4>
            {replays.data && replays.data.length === 0 && <p className="muted">None yet.</p>}
            <ul className="replay-list">
              {replays.data?.map((r) => (
                <li key={r.trace_id}>
                  <StatusBadge status={r.status} />
                  <span className="mode">{r.replay_mode}</span>
                  {r.replay_label && <span className="muted">{r.replay_label}</span>}
                  <span className="muted">
                    {formatDuration(r.duration_ms)} · {relativeTime(r.start_time)}
                  </span>
                  <Link to={`/traces/${trace.trace_id}/compare/${r.trace_id}`}>Compare</Link>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {canReplay &&
        (showForm ? (
          <CaseForm trace={trace} />
        ) : (
          <button className="button" onClick={() => setShowForm(true)}>
            Turn into a regression case…
          </button>
        ))}
    </section>
  );
}
