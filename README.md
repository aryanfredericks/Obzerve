# Obzerve

[![CI](https://github.com/aryanfredericks/Obzerve/actions/workflows/ci.yml/badge.svg)](https://github.com/aryanfredericks/Obzerve/actions/workflows/ci.yml)

Obzerve is an open-source, self-hostable observability and reliability platform for AI agents. It records each agent run as a trace of LLM calls, retrieval, tool calls and agent handoffs, so you can work out *which* operation made the answer bad.

**Status:** Phases 1–3 are done. You can trace agents end to end and see latency, error, token and estimated-cost metrics. Ingestion can run through a queue that survives database outages without losing data. A failed trace can be replayed and turned into a regression case, and a release gate checks prompt or model changes against a baseline. See [docs/architecture.md](docs/architecture.md) for the design, failure handling, benchmarks and roadmap.

```
                                        direct mode
agent + obzerve SDK ──OTLP/HTTP──▶ FastAPI ingestion ─────────────────▶ PostgreSQL ◀── dashboard/metrics API ◀── React UI
   (OpenTelemetry)    /v1/traces    (auth, validate)  ╲   queue mode       ▲
                                                       ╲─▶ Redis Streams ──▶ workers
```

## Repository layout

| Path | What it is |
| --- | --- |
| `sdk/python/` | `obzerve` (published to PyPI): the Python SDK, built on OpenTelemetry, with automatic instrumentation for OpenAI, Anthropic and Hugging Face |
| `server/` | `obzerve-server`: OTLP ingestion, dashboard API, migrations, admin CLI |
| `web/` | React + TypeScript dashboard (Vite) |
| `examples/research_agent/` | Demo research agent: local mock LLM, GPU embeddings, injectable failures |
| `tools/loadtest.py` | Ingestion load test that checks no telemetry was lost |
| `compose.yaml` | PostgreSQL and Redis for development; the whole platform with `--profile stack` |
| `server/Dockerfile`, `web/Dockerfile`, `deploy/` | Container images and the dashboard's nginx config |

The Python packages form a single [uv](https://docs.astral.sh/uv/) workspace with one lockfile.

**New here? Follow [docs/getting-started.md](docs/getting-started.md).** It goes step by step from a fresh clone to replaying a failure and gating a fix, in about 15 minutes, offline.

## Run it in containers

Only Docker or Podman is needed:

```bash
make stack-demo   # dashboard http://localhost:8080, API http://localhost:8000, key dev_project_key
```

See [docs/deployment.md](docs/deployment.md) for configuration, scaling, upgrades and hardening.

## Quick start (development)

Prerequisites: `uv`, Node 20+, and Docker or Podman.

```bash
make setup       # uv sync + npm install
make db-up       # start PostgreSQL and Redis
make bootstrap   # migrations, the "demo" project (API key `dev_project_key`), and a demo model price
make server      # API on http://localhost:8000      (terminal 1)
make web         # dashboard on http://localhost:5173 (terminal 2)
make demo        # run the demo agent once per failure scenario
```

Open http://localhost:5173 and click a trace, or open **Metrics** for latency percentiles, error and retry rates, tokens and estimated cost.

### Queue mode

In direct mode the API writes to PostgreSQL before it responds. In queue mode the API only validates each request and appends it to a Redis stream, and workers write it to the database. Ingestion then keeps working through database slowdowns and outages.

```bash
make server-queue                      # instead of `make server`
make worker                            # one or more workers
WORKER_METRICS_PORT=9102 make worker   # a second worker
make queue-status                      # backlog / in progress / dead letters
```

## Demo agent and failure scenarios

```bash
uv run research-agent "Compare recent approaches to RAG evaluation" --fault llm-timeout
```

| `--fault` | What happens | What the trace shows |
| --- | --- | --- |
| `none` | Normal run | search → retrieve → synthesize → hand off to report writer |
| `llm-timeout` | First two LLM attempts time out | Two failed `llm.generate` spans, then a successful retry; the trace is still OK |
| `llm-outage` | Every LLM attempt times out | The run fails; the root-cause callout points at the LLM call |
| `bad-retrieval` | Retriever returns irrelevant documents | Every span is "OK" but the answer is wrong; there is a `weak_evidence` event and low retrieval scores |
| `search-error` | The search tool is down | The tool span errors; the agent recovers without search results |

By default the "LLM" is a deterministic local mock with simulated latency and estimated token counts, so tests are fast and repeatable. Add `--llm ollama` to use a real model served by [Ollama](https://ollama.com) (default `qwen3:latest`; change it with `--ollama-model` and `--ollama-url`). Spans then carry the token counts and timings Ollama measured. With a real model, the same question can produce a different answer on each run. Injected timeouts (`--fault llm-timeout` / `llm-outage`) never call the model, and their error message says they were simulated.

```bash
ollama serve &                                  # if it is not already running
uv run obzerve-server pricing set qwen3:latest --provider ollama --input 0 --output 0   # local: $0
uv run research-agent --llm ollama --fault bad-retrieval
```

Retrieval uses `sentence-transformers/all-MiniLM-L6-v2` and runs on the GPU when PyTorch can see one.

### AMD GPU (ROCm)

The workspace installs PyTorch from the ROCm 7.1 wheel index (`[[tool.uv.index]]` in `pyproject.toml`), so torch-based packages such as sentence-transformers run on AMD GPUs. To check:

```bash
make gpu-check
# PyTorch: 2.13.0+rocm7.1 / HIP: 7.1.52802 / GPU available: True / GPU: AMD Radeon RX 9070 XT
```

## Instrumenting your own agent

Install the SDK in the agent's project:

```bash
pip install obzerve              # with extras for your clients, e.g. "obzerve[openai,anthropic]"
```

`obzerve` is published to [PyPI](https://pypi.org/project/obzerve/) from this repository (see [docs/publishing.md](docs/publishing.md)).

Then wrap each request in a trace and turn on automatic instrumentation. Every call made through `openai`, `anthropic`, `huggingface_hub` or `transformers` becomes an LLM span, with model, provider and token usage, and is recorded for replay:

```python
from obzerve import Observer, SpanKind

observer = Observer(
    endpoint="http://localhost:8000", api_key="dev_project_key", service_name="my-agent"
)
observer.instrument()  # OpenAI, Anthropic, Hugging Face (InferenceClient + transformers)

with observer.trace("research_agent", input=question) as trace:
    with observer.span("search_web", kind=SpanKind.TOOL, input={"query": question}) as span:
        results = search(question)
        span.set_output(results)
    reply = client.chat.completions.create(model="gpt-x", messages=[...])  # traced automatically
    trace.set_output(reply.choices[0].message.content)


@observer.observe(kind=SpanKind.TOOL)  # decorator form, sync or async
def fetch_page(url: str) -> str: ...
```

The [SDK README](sdk/python/README.md) has the details: what each integration records, streaming, replay, and current limits. For a model or client the instrumentation doesn't cover, record usage on any span with `span.set_llm(model=..., provider=..., input_tokens=..., output_tokens=...)`.

- **Non-blocking:** spans are exported in batches from a background thread through a bounded queue with short timeouts. When the server answers 503 (backpressure or an outage), the exporter retries and honours `Retry-After`. If the buffer fills up anyway, spans are dropped. The agent is never blocked and never sees an error from telemetry.
- **Delivery stats:** `observer.stats()` returns exact counts of exported, failed, dropped and pending spans, so lost telemetry is visible instead of silent.
- **Privacy:** inputs and outputs are captured with secret redaction (keys like `api_key` or `password`, and bearer and `sk-…` tokens) and are truncated. Pass `capture_payloads=False` to turn capture off.
- **Errors and retries:** an exception raised inside a span marks the span as failed and records the error type and message. `span.record_retry(attempt, exc)` records retries.
- **Across services:** `observer.inject_headers()` on the caller and `observer.trace(..., parent_headers=headers)` on the callee join both sides into one trace (W3C trace context).
- **Standard protocol:** the server accepts standard OTLP/HTTP (protobuf or JSON, optionally gzip), so any OpenTelemetry-instrumented app can send traces to it. Attributes follow the OpenTelemetry GenAI semantic conventions (`gen_ai.*`).

## Replay and regression testing

Wrap the operations your agent depends on with `observer.replayable`. Each call then records its exact result, or the exception it raised, so the trace can be replayed later:

```python
@observer.replayable(kind=SpanKind.TOOL, encode=docs_to_json, decode=docs_from_json)
def search(query: str) -> list[Doc]: ...


@observer.replayable(kind=SpanKind.TOOL, side_effect=True)  # never re-run by a replay
def send_email(to: str, body: str) -> None: ...
```

Every trace is labelled **replayable**, **partially replayable** or **not replayable**, with the reasons. Replay a trace with a policy:

| Policy | What runs live | Use it to |
| --- | --- | --- |
| `recorded` | nothing | reproduce the run exactly, including recorded failures, without calling any model or tool |
| `llm-live` | model calls | test a prompt or model change against the recorded tool and retrieval results |
| `live` | everything except side effects | check the whole workflow under current conditions |

```python
bundle = ObzerveClient(endpoint, key).replay_bundle(trace_id)
with observer.replay(bundle, RECORDED):
    agent.run(**bundle.root_input)  # linked to the original trace; divergences are flagged
```

The demo agent wraps this as `uv run research-agent replay <trace_id> --mode recorded`. The dashboard compares the original and the replay step by step.

**From a failed trace to a release gate:**

```bash
# 1. Turn traces into regression cases (or use "Turn into a regression case" on the trace page)
obzerve case add <failed_trace> --dataset regressions --name "outage degrades gracefully" \
    --mode recorded --no-error --contains-any unavailable
obzerve case add <bad_answer_trace> --dataset regressions --name "admits missing context" \
    --regex "does not contain|insufficient"

# 2. Run the dataset against a baseline and a candidate (prompt version, model, code change)
uv run research-agent eval regressions --label baseline --llm ollama
uv run research-agent eval regressions --label candidate --llm ollama --prompt concise --fallback

# 3. Gate the release (exits 1 on failure, so CI blocks it)
obzerve eval gate <baseline_run> <candidate_run> --max-regressions 0
```

A case stores a frozen copy of its source trace's recordings, so it keeps working after the trace is gone. Each case also has its own replay policy: `recorded` for a failure that must reproduce exactly, `llm-live` for checks on answer quality. Assertions are deterministic: `no_error`, `contains_all`/`contains_any`/`not_contains`, `regex`, `max_latency_ms`, `max_output_chars`. Each run records the dataset version, evaluator version, prompt version, replay policy and configuration. Comparisons say so when two runs aren't comparable.

## Estimated cost

LLM spans are priced when they are ingested, from a per-model price table (USD per million tokens). Each price has an effective date and can be overridden per project or per provider. Cache-read tokens can have their own price. All costs are labelled as estimates. If a span has token usage but no matching price, it is counted as *unpriced* and the dashboard marks the total as partial.

```bash
uv run obzerve-server pricing set gpt-x --provider acme --input 2.50 --output 10 --cached-input 1.25
uv run obzerve-server pricing set gpt-x --provider acme --input 2.00 --output 8 --effective-from 2026-11-01
uv run obzerve-server pricing list
uv run obzerve-server pricing recompute        # re-price spans already stored
```

## Monitoring Obzerve itself

`GET /metrics` on the API, and on port 9101 for each worker, serves Prometheus metrics: ingest outcomes, spans persisted vs duplicates, HTTP latency per route, database errors, queue depth, pending and dead-letter counts, and worker lag. `/healthz` reports liveness and `/readyz` checks the database (and Redis in queue mode). `/metrics` is unauthenticated, so expose it only to your scraper.

## API keys

Each project has hashed API keys with scopes: `ingest` to send traces, `read` to use the dashboard API, and `write` to register prompt versions, create eval cases and submit eval runs. Every query is scoped to the key's project.

```bash
uv run obzerve-server create-project my-app                   # prints a new key once
uv run obzerve-server create-key my-app --scopes ingest       # e.g. an ingest-only key for production agents
```

The dashboard sends the key from its **API key** control, which falls back to `VITE_OBZERVE_API_KEY` in `web/.env.development`. User accounts and login are planned for Phase 4.

## Development

```bash
make test        # pytest: SDK, server, queue/worker failure tests (need `make db-up`), demo agent
make lint        # ruff + TypeScript typecheck
make loadtest    # 20s of concurrent ingestion, then verifies every trace was stored
make ci          # the same checks GitHub CI runs
```

Server tests run against a throwaway `obzerve_test` database and Redis db 15. Set `OBZERVE_TEST_DATABASE_URL` or `OBZERVE_TEST_REDIS_URL` to use other ones. Server settings come from `OBZERVE_*` environment variables (see `server/src/obzerve_server/config.py`).
