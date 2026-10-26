# Obzerve Python SDK

Observability, replay, and regression testing for AI agents.

`obzerve` traces what your agent does: LLM calls, tool calls, retrieval, and handoffs. Traces go to a self-hosted [Obzerve](https://github.com/aryanfredericks/Obzerve) server, where you can see the timeline, latency, errors, token usage, and estimated cost of every request. When something goes wrong, the SDK can replay the exact execution from its recordings and turn it into a regression test that gates your next release.

The SDK is built on OpenTelemetry and sends standard OTLP.

```bash
pip install obzerve                  # or: uv add obzerve
pip install "obzerve[openai]"        # plus the client libraries you use: openai, anthropic,
                                     # huggingface, transformers, or all
```

You need a running Obzerve server and a project API key. See the [getting-started guide](https://github.com/aryanfredericks/Obzerve/blob/main/docs/getting-started.md); `make stack-demo` starts one locally in containers.

## Trace an agent in three lines

```python
from obzerve import Observer

observer = Observer(endpoint="http://localhost:8000", api_key="obz_...", service_name="support-bot")
observer.instrument()  # trace OpenAI, Anthropic and Hugging Face calls automatically


def handle(ticket):
    with observer.trace("support_bot", input={"ticket": ticket}) as trace:
        answer = ...  # your agent: every LLM call inside becomes a span of this trace
        trace.set_output(answer)
        return answer
```

## Automatic instrumentation

`observer.instrument()` patches the supported client libraries that are installed. Every model call becomes an LLM span inside the active trace, recording:
- model and provider
- sampling settings
- input, output, and cached token counts
- finish reason
- inputs and outputs, subject to redaction and `capture_payloads`

| Library | What is traced |
| --- | --- |
| `openai` | Chat Completions, Responses, and Embeddings: sync, async, and streams. OpenAI-compatible servers (vLLM, Ollama, TGI, LM Studio, ...) reached through the OpenAI client are recorded as `openai_compatible`, with their server address |
| `anthropic` | `messages.create` (including `stream=True`) and the `messages.stream()` helper, sync and async. Cache reads and writes are included in input tokens, and cache reads are priced as cached tokens |
| `huggingface_hub` | `InferenceClient.chat_completion` (also reached as `client.chat.completions.create`) and `text_generation`, sync, async, and streams. Covers the serverless Inference API, Inference Endpoints, and self-hosted TGI |
| `transformers` | Any local model's `generate()` (model, device, prompt and generated token counts) and text-generation pipelines |

```python
observer.instrument(openai=True, anthropic=False)  # True: require it; False: skip it;
# None (default): instrument it if installed
handle = observer.instrument()
handle.uninstrument()  # restore the original libraries
```

`transformers` loads PyTorch on import, so by default it is instrumented only if your code has already imported it. Pass `transformers=True` to instrument it regardless.

Instrumentation never breaks your call: if a library changes shape, you lose telemetry, not the request. A streamed response stays open as one span until you finish reading it, close it, or leave its `with` block. Close streams you stop reading early.

## Spans you define yourself

```python
from obzerve import SpanKind

with observer.span("search_orders", kind=SpanKind.TOOL, input={"customer": cid}) as span:
    orders = orders_api.search(cid)
    span.set_output(orders)


@observer.observe(kind=SpanKind.RETRIEVAL)  # decorator form, sync or async
def retrieve(query: str) -> list[str]: ...
```

Exceptions mark the span as failed, with the error type and message. `span.record_retry(attempt, exc)` records retries. `span.set_llm(model=..., input_tokens=..., output_tokens=...)` records model usage for calls the instrumentation doesn't cover.

## Replay and regression testing

Calls through the instrumented libraries are recorded exactly: the whole response, rebuilt as the library's own response type during a replay. For your own tools, use `@observer.replayable`:

```python
@observer.replayable(kind=SpanKind.TOOL)
def lookup_order(order_id: str) -> dict: ...


@observer.replayable(kind=SpanKind.TOOL, side_effect=True)  # never re-run by a replay
def refund(order_id: str) -> None: ...
```

Then reproduce a production request, without calling any model or tool:

```python
from obzerve.client import ObzerveClient
from obzerve.replay import RECORDED, LLM_LIVE

bundle = ObzerveClient("http://localhost:8000", api_key).replay_bundle(trace_id)
with observer.replay(bundle, RECORDED):  # or LLM_LIVE: tools recorded, model live
    handle(**bundle.root_input)
```

Turn the trace into a regression case, run your eval dataset against a change, and gate the release in CI:

```bash
obzerve case add <trace_id> --dataset regressions --name "refund flow" --mode recorded --no-error
obzerve eval gate <baseline_run> <candidate_run> --max-regressions 0     # exits 1 on failure
```

Run datasets with `obzerve.evals.run_eval`. The [main README](https://github.com/aryanfredericks/Obzerve#replay-and-regression-testing) covers the full workflow.

## Privacy and reliability defaults

- **Redaction:** captured inputs and outputs are redacted (keys such as `api_key`, `password`, `authorization`, plus bearer and `sk-…` tokens) and truncated. `Observer(capture_payloads=False)` turns capture off entirely.
- **Never in your way:** export runs in a background thread through a bounded buffer with short timeouts. A slow or unavailable server never blocks or fails your agent; telemetry is dropped instead. `observer.stats()` reports exactly how many spans were exported, failed, or dropped.
- **No global takeover:** the SDK uses its own OpenTelemetry tracer provider and never replaces yours.
- **Anthropic's own spans:** the Anthropic SDK can emit OpenTelemetry spans to your *global* provider. If you also send those to Obzerve, disable one of the two (`Anthropic(open_telemetry=False)`), or each call appears twice.

## Current limits

- Streamed responses are traced, but not yet recorded for replay. A replay that reaches one runs it live (`LLM_LIVE`) or fails with a clear error (`RECORDED`).
- Replay matches calls by operation name and order. Concurrent calls of the same operation can't yet be told apart reliably.
- Tested with Python 3.10–3.13, `openai` 3.x, `anthropic` 2.x, `huggingface_hub` 2.x, and `transformers` 5.x. Other versions are instrumented best-effort: an incompatible library logs a warning and is left alone.

Licensed under [Apache-2.0](https://github.com/aryanfredericks/Obzerve/blob/main/LICENSE).
