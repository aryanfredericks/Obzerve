"""Span attribute keys shared by the SDK and the Obzerve server.

AI-specific keys follow the OpenTelemetry GenAI semantic conventions where one
exists; Obzerve-specific keys live under the ``obzerve.`` namespace.
"""

# Obzerve
SPAN_KIND = "obzerve.span.kind"
RETRY_COUNT = "obzerve.retry.count"
RETRY_ATTEMPT = "obzerve.retry.attempt"
INPUT = "obzerve.input"
OUTPUT = "obzerve.output"
INPUT_TRUNCATED = "obzerve.input.truncated"
OUTPUT_TRUNCATED = "obzerve.output.truncated"
INPUT_REDACTED = "obzerve.input.redacted"
SDK_VERSION = "obzerve.sdk.version"

# Replay: recording (set on replayable operations)
REPLAY_KEY = "obzerve.replay.key"  # "<name>#<occurrence>" within the trace
REPLAY_INPUT_HASH = "obzerve.replay.input_hash"
REPLAY_COMPLETE = "obzerve.replay.complete"  # the recorded output round-trips exactly
REPLAY_INCOMPLETE_REASON = "obzerve.replay.incomplete_reason"
REPLAY_SIDE_EFFECT = "obzerve.replay.side_effect"
REPLAY_ERROR_CLASS = "obzerve.replay.error_class"  # "module:QualName" of a raised exception
# Replay: replaying
REPLAY_SOURCE = "obzerve.replay.source"  # "live" or "recorded"
REPLAY_INPUT_MATCHED = "obzerve.replay.input_matched"
REPLAY_OF = "obzerve.replay.of"  # on the root span: the trace being replayed
REPLAY_MODE = "obzerve.replay.mode"
REPLAY_LABEL = "obzerve.replay.label"

# Versioned configuration and evaluation (root span)
PROMPT_NAME = "obzerve.prompt.name"
PROMPT_VERSION = "obzerve.prompt.version"
EVAL_RUN_ID = "obzerve.eval.run_id"
EVAL_CASE_ID = "obzerve.eval.case_id"

# OpenTelemetry GenAI semantic conventions
GEN_AI_PROVIDER = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# Input tokens served from the provider's prompt cache (a subset of input tokens).
GEN_AI_CACHED_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"

# OpenTelemetry general conventions
ERROR_TYPE = "error.type"
SERVICE_NAME = "service.name"


class SpanKind:
    """Logical operation types understood by the dashboard."""

    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVAL = "retrieval"
    CHAIN = "chain"
    HANDOFF = "handoff"
    FUNCTION = "function"
