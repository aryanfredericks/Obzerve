# Obzerve architecture

## Data flow

1. **The SDK** (`sdk/python`) wraps the OpenTelemetry SDK in a private `TracerProvider`, so it never replaces the application's own OTel setup. `Observer.trace()` and `Observer.span()` create spans. Context propagation (contextvars) builds the parent/child links. A `BatchSpanProcessor` exports spans from a background thread through a bounded queue. The OTLP/HTTP exporter sends them to `POST /v1/traces` with `Authorization: Bearer <key>`.
2. **Ingestion** (`server/.../routes/ingest.py`) authenticates the key (`ingest` scope; valid keys are cached for `api_key_cache_ttl_s`, 60s by default, which is also the delay before a revoked key stops working) and caps the body size before and after decompression, which also stops zip bombs. It accepts OTLP protobuf or JSON, optionally gzip. Each span is decoded and validated on its own (`otlp.py`). An invalid span is rejected through OTLP `partial_success`; the rest of the batch is still stored.
3. **Ingest mode.** In `direct` mode (the default) the API persists the spans before responding. In `queue` mode it appends the validated payload to a Redis stream and responds; `obzerve-server worker` processes persist it (see *Queue and workers* below).
4. **Persistence** (`ingest.py`) runs in one transaction:
   - LLM spans are priced from `model_prices` (`pricing.py`).
   - Spans are inserted with `ON CONFLICT (project_id, trace_id, span_id) DO NOTHING RETURNING ...`. The statements are prepared once and run as executemany, so SQLAlchemy does not recompile them per request.
   - Trace aggregates (time bounds, counts, tokens, root-span name and status) are upserted from the rows that were *actually inserted*. Re-delivering a batch after an exporter retry therefore changes nothing (idempotent ingestion).
   - Spans can arrive in any order. A trace's status stays `unset` (shown as "pending") until its root span arrives.
5. **The dashboard API** (`routes/traces.py`, `routes/metrics_api.py`) checks for the `read` scope. Every query filters on the caller's `project_id`, so a trace ID alone never grants access. Lists use keyset pagination on `(start_time, id)`.
6. **The dashboard** (`web/`) shows a trace list with filters, a trace waterfall with a span detail panel, a metrics page, and an errors and retries view.

## Queue and workers

Redis Streams with one consumer group (`queue.py`, `worker.py`). Delivery is at-least-once: a message is acknowledged and deleted only after its transaction commits. That is safe because persistence is idempotent. Because acknowledged messages are deleted, the stream length is the backlog.

| Failure | Behaviour |
| --- | --- |
| Backlog ≥ `queue_max_length` | The API returns `503` + `Retry-After`. OTLP exporters retry, so ingestion slows down rather than losing data |
| Redis unreachable | The API returns `503` + `Retry-After`. Workers back off and retry |
| PostgreSQL down (queue mode) | Ingestion continues for cached keys. Workers pause, poll until the database answers, then re-process their own pending messages. **An outage does not count toward a message's delivery limit** |
| PostgreSQL down (direct mode) | API and dashboard return `503` + `Retry-After` |
| Invalid message (undecodable payload, unknown project, out-of-range data) | Moved to the dead-letter stream immediately |
| Unexpected processing error | Left pending, reclaimed after `worker_claim_idle_ms`, and dead-lettered after `worker_max_deliveries` |
| Worker crash | Its unacknowledged messages are reclaimed by another worker (or by itself after a restart) |
| Shutdown (SIGTERM/SIGINT) | The worker finishes the message in flight, then exits |

Transient database errors are recognised by exception type and by SQLSTATE: connection failures (08xxx), insufficient resources (53xxx), shutdown/restart (57P01–57P03), statement timeout, and serialization failure or deadlock. Workers never exit on an error; they log it and back off. `obzerve-server queue requeue-dead` sends dead-lettered messages back to the stream after a fix.

## Metrics

`/api/v1/metrics/summary`, `/timeseries` and `/breakdown` (by workflow, service, operation, kind or model) compute exact percentiles (`percentile_cont`) over a window of up to 90 days. Requests are traces. "Time per request" sums span durations by kind, so concurrent operations can add up to more than the request duration (the UI says so). Costs are estimates; `unpriced_llm_calls > 0` means the total is a lower bound.

Exact percentiles mean a sort per query. On one PostgreSQL instance with 2.7M spans in the window, the summary takes about 0.25 s and span breakdowns 0.5–2 s. The next scaling step is per-minute rollup tables (or TimescaleDB continuous aggregates) with t-digest sketches for percentiles.

## Benchmarks

Measured on one machine (Ryzen 7 7800X3D), with one API process and PostgreSQL 17 and Redis 7 in Podman. Each request carried 5 traces × 10 spans, from 16 concurrent senders (`tools/loadtest.py`):

| Scenario | Accepted | Request latency | Lost |
| --- | --- | --- | --- |
| Direct mode | 382 req/s · 19k spans/s | p50 39 ms · p95 64 ms | 0 |
| Queue mode, 2 workers | 686 req/s · 34k spans/s (workers persist ≈18k spans/s) | p50 12 ms · p95 76 ms | 0 |
| Queue mode, PostgreSQL stopped for ~11 s mid-run (8 senders) | 639k spans; 48 backpressure 503s retried | p50 7 ms · p95 14 ms | **0** |

Before the executemany change, direct mode handled 65 req/s (p50 245 ms): about half of each request was spent recompiling multi-row INSERT statements.

## Replay

Replay runs in the SDK, because only the agent's own process can re-execute its code. The server stores recordings and serves them back as *replay bundles*.

- **Recording.** `Observer.replayable` gives each call a key, `"<name>#<n>"` (the n-th call of that operation in the trace). It stores the result as exact JSON in `obzerve.output`, after an optional `encode`. If the call raised, it stores the exception class as `module:QualName`. A recording is marked incomplete when its output is not JSON-native, was redacted, or is over 256 KiB. The root span's input is recorded the same way, because a replay re-runs the agent with it.
- **Replayability** is computed at ingest from per-trace aggregates: `replay_points`, `replay_points_incomplete`, `replay_side_effects`, `root_input_complete`. The result is *replayable*, *partial* (some operations would have to run live) or *non-replayable*, each with reasons.
- **Replaying.** `Observer.replay(bundle, policy)` intercepts replayable calls. Each call either returns its decoded recording, re-raises the recorded exception, or runs live, depending on the policy. Exception classes are looked up only among modules that are already imported; data from the server never triggers an import. A class that can't be found is raised as `ReplayedError`.
- **Divergence.** Each call's arguments are hashed, leaving out test-harness knobs such as injected faults. A replayed call whose arguments differ from the recording is flagged on its span and in the session. A call that isn't in the recording raises `ReplayMissError`, unless the policy allows running it live.
- **Side effects.** Operations marked `side_effect=True` are served from their recording. Under any policy they never run live unless `allow_side_effects=True`.
- **Linking.** A replay's root span carries `obzerve.replay.of`, `.mode` and `.label`, plus the changed configuration such as the prompt version. The original trace lists its replays, and `/api/v1/trace-comparison` aligns two traces step by step by replay key.
- **Access.** Bundles and cases contain captured payloads. They are scoped to the project, and every read and every case creation is written to `audit_events`.

## Evaluations

| Table | Purpose |
| --- | --- |
| `prompt_versions` | Immutable, numbered configurations (prompts, sampling, model), deduplicated by content hash |
| `eval_datasets` | Named case sets; `version` increments on every case added or archived |
| `eval_cases` | A frozen replay bundle from a source trace, assertions, and a per-case replay policy (`recorded` / `llm-live` / `live`) |
| `eval_runs`, `eval_results` | One run per dataset version: evaluator version, prompt version, replay policy, config, per-case verdicts and trace links |

`obzerve.evals.run_eval` runs in the agent's process. It replays each case under that case's policy, applies its deterministic assertions (evaluator `obzerve-assertions/1`), and submits the results. The server rejects a run unless it covers exactly the cases active at the dataset version it claims. Run summaries join each result's trace for token and cost figures, and latency is measured by the runner. Two runs count as comparable only if they share the dataset version, evaluator version and replay policy. The gate (`/api/v1/eval-runs/gate/{baseline}/{candidate}`, or `obzerve eval gate`) checks:
- the number of regressions,
- the minimum pass rate,
- the maximum pass-rate drop,
- the cost increase,
- the p95 latency increase.

It always fails when the runs aren't comparable.

Not included yet: LLM-as-judge evaluators. Deterministic checks are preferred; a judge can be added as a new assertion type with its own evaluator version.

## Span model

| Column | Source attribute(s) |
| --- | --- |
| `kind` | `obzerve.span.kind` (agent, llm, tool, retrieval, chain, handoff, function). If missing, `llm` when `gen_ai.*` attributes are present, otherwise `function` |
| `model`, `provider` | `gen_ai.request.model` / `gen_ai.response.model`, `gen_ai.provider.name` / `gen_ai.system` |
| `input_tokens`, `output_tokens` | `gen_ai.usage.input_tokens` / `output_tokens` (legacy `prompt_tokens` / `completion_tokens` are also accepted) |
| `retry_count` | `obzerve.retry.count` |
| `error_type` | `error.type`, else the `exception.type` of an `exception` event |
| `input`, `output` | `obzerve.input` / `obzerve.output` (also OpenInference `input.value` / `output.value`) |

All other attributes are kept in `attributes` (JSONB). Events and resource attributes are stored as JSONB too. NUL characters are stripped because PostgreSQL cannot store them.

## Tables

`projects`, `project_api_keys` (SHA-256 hash and scopes; raw keys are never stored), `traces` (PK `project_id, trace_id`), `spans` (PK `project_id, trace_id, span_id`; carries token counts, cached tokens and the estimated cost with the `price_id` used), `model_prices` (per-model USD/1M tokens with `effective_from`; project and provider are optional overrides). Indexes follow the query patterns: project + start time, project + status, project + error type (partial index on errors only), and project + kind + start time (covering duration, status, error type and retries, for index-only per-kind metrics). Migrations live in `server/src/obzerve_server/migrations` (Alembic) and are applied with `obzerve-server migrate`.

## Reliability decisions

- **Telemetry is best-effort.** If the backend is down, the SDK drops spans when its queue fills. It never blocks the agent or raises into it. This is covered by a test.
- **Ingestion is idempotent**, so exporter retries are safe.
- **Delivery is accounted** in the SDK: `Observer.stats()` gives exact exported, failed, dropped and pending counts. The SDK enforces its buffer bound itself, so drops are counted rather than silent.
- **Batches are sorted and deduplicated before insert**, which avoids lock-order deadlocks between concurrent batches for the same traces.
- **Health checks:** `/healthz` reports liveness and `/readyz` checks the database (and Redis in queue mode). The connection, pool and statement timeouts are all bounded.
- **Self-monitoring:** Prometheus metrics at `/metrics` (API) and `:9101/metrics` (workers).

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | SDK, OTLP ingestion, PostgreSQL, trace list/detail UI, error and retry search, tenant scoping | **Done** |
| 2 | Queue (Redis Streams) and workers, SDK delivery accounting, latency/error/token metrics, estimated cost with dated prices, Prometheus self-metrics, failure and load tests | **Done** |
| 3 | Replay (recorded / LLM-live / live) with divergence detection and side-effect guards, trace-to-regression-case conversion, prompt versions, eval runs, comparison and release gate | **Done** |
| 4 (next) | User auth, CI/CD, containerized deployment, docs, polished demo; alerting (p95 / error rate / budget rules with windows and cooldowns) | |
