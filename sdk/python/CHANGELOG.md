# Changelog

All notable changes to the `obzerve` Python SDK. Versions follow [Semantic Versioning](https://semver.org/); while the version is 0.x, minor releases may change the API.

## 0.1.0

First release.

- Tracing built on OpenTelemetry: `Observer.trace()`, `span()`, `@observe`, a bounded non-blocking export, and exact delivery counters (`Observer.stats()`).
- Automatic instrumentation (`Observer.instrument()`) for `openai` (Chat Completions, Responses, Embeddings), `anthropic` (Messages, including streaming), `huggingface_hub` (`InferenceClient`) and `transformers` (`generate()` and text-generation pipelines): sync, async, and streams.
- Replay: `@replayable` operations, automatic recordings of non-streaming instrumented calls, and `RECORDED` / `LLM_LIVE` / `LIVE` policies with divergence detection and side-effect guards.
- Evaluations: `obzerve.evals.run_eval`, deterministic assertions, and the `obzerve` CLI (`case add`, `eval compare`, `eval gate`).
- Payload redaction and truncation; W3C trace-context propagation across services.
