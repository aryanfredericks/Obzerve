"""Decode OTLP/HTTP trace payloads into normalized span rows."""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as PbSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status as PbStatus

CONTENT_TYPE_PROTOBUF = "application/x-protobuf"
CONTENT_TYPE_JSON = "application/json"

# Attributes promoted to dedicated columns, in order of preference.
_KIND = "obzerve.span.kind"
_RETRY_COUNT = "obzerve.retry.count"
_INPUT_KEYS = ("obzerve.input", "input.value")
_OUTPUT_KEYS = ("obzerve.output", "output.value")
_MODEL_KEYS = ("gen_ai.request.model", "gen_ai.response.model")
_PROVIDER_KEYS = ("gen_ai.provider.name", "gen_ai.system")
_INPUT_TOKEN_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
_OUTPUT_TOKEN_KEYS = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
_CACHED_TOKEN_KEYS = (
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_read_input_tokens",
    "gen_ai.usage.cached_tokens",
)
_PROMOTED = {_KIND, _RETRY_COUNT, *_INPUT_KEYS, *_OUTPUT_KEYS}

_STATUS = {
    PbStatus.STATUS_CODE_UNSET: "unset",
    PbStatus.STATUS_CODE_OK: "ok",
    PbStatus.STATUS_CODE_ERROR: "error",
}


class PayloadError(ValueError):
    """The request body is not a valid OTLP trace payload."""


class PayloadTooLarge(ValueError):
    pass


@dataclass
class DecodeResult:
    spans: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


def decompress(body: bytes, encoding: str | None, limit: int) -> bytes:
    encoding = (encoding or "identity").lower()
    if encoding in ("identity", ""):
        return body
    if encoding not in ("gzip", "deflate"):
        raise PayloadError(f"Unsupported Content-Encoding: {encoding}")
    wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
    decompressor = zlib.decompressobj(wbits)
    try:
        out = decompressor.decompress(body, limit + 1)
    except zlib.error as exc:
        raise PayloadError(f"Invalid {encoding} body") from exc
    if len(out) > limit or decompressor.unconsumed_tail:
        raise PayloadTooLarge(f"Decompressed body exceeds {limit} bytes")
    return out


def parse_request(body: bytes, content_type: str) -> ExportTraceServiceRequest:
    message = ExportTraceServiceRequest()
    media_type = content_type.split(";")[0].strip().lower()
    try:
        if media_type == CONTENT_TYPE_PROTOBUF:
            message.ParseFromString(body)
        elif media_type == CONTENT_TYPE_JSON:
            json_format.Parse(body, message, ignore_unknown_fields=True)
        else:
            raise PayloadError(f"Unsupported Content-Type: {content_type or '(none)'}")
    except (DecodeError, json_format.ParseError, UnicodeDecodeError) as exc:
        raise PayloadError(f"Malformed OTLP payload: {exc}") from exc
    return message


def decode_spans(message: ExportTraceServiceRequest) -> DecodeResult:
    result = DecodeResult()
    for resource_spans in message.resource_spans:
        resource = _attributes(resource_spans.resource.attributes)
        service_name = resource.get("service.name")
        for scope_spans in resource_spans.scope_spans:
            for pb_span in scope_spans.spans:
                try:
                    result.spans.append(_span_row(pb_span, resource, service_name))
                except PayloadError as exc:
                    result.rejected += 1
                    if len(result.errors) < 5:
                        result.errors.append(str(exc))
    return result


def _span_row(span: PbSpan, resource: dict[str, Any], service_name: Any) -> dict[str, Any]:
    if len(span.trace_id) != 16 or not any(span.trace_id):
        raise PayloadError("span has an invalid trace_id")
    if len(span.span_id) != 8 or not any(span.span_id):
        raise PayloadError("span has an invalid span_id")
    if not span.start_time_unix_nano:
        raise PayloadError("span is missing start_time_unix_nano")
    start_ns = span.start_time_unix_nano
    end_ns = max(span.end_time_unix_nano, start_ns)

    attrs = _attributes(span.attributes)
    events = [
        {
            "name": _clean(event.name),
            "time": _datetime(event.time_unix_nano or start_ns).isoformat(),
            "attributes": _attributes(event.attributes),
        }
        for event in span.events
    ]
    status = _STATUS.get(span.status.code, "unset")
    error_type = attrs.get("error.type")
    if error_type is None and status == "error":
        error_type = next(
            (
                e["attributes"].get("exception.type")
                for e in events
                if e["name"] == "exception" and e["attributes"].get("exception.type")
            ),
            None,
        )

    kind = attrs.get(_KIND)
    if not isinstance(kind, str) or not kind:
        kind = "llm" if any(k.startswith("gen_ai.") for k in attrs) else "function"

    return {
        "trace_id": span.trace_id.hex(),
        "span_id": span.span_id.hex(),
        "parent_span_id": span.parent_span_id.hex() if any(span.parent_span_id) else None,
        "name": _clean(span.name) or "(unnamed)",
        "kind": kind[:32],
        "service_name": _str_or_none(service_name),
        "start_time": _datetime(start_ns),
        "end_time": _datetime(end_ns),
        "duration_ms": (end_ns - start_ns) / 1e6,
        "status": status,
        "status_message": _clean(span.status.message) or None,
        "error_type": _str_or_none(error_type),
        "model": _str_or_none(_first(attrs, _MODEL_KEYS)),
        "provider": _str_or_none(_first(attrs, _PROVIDER_KEYS)),
        "input_tokens": _int_or_none(_first(attrs, _INPUT_TOKEN_KEYS)),
        "output_tokens": _int_or_none(_first(attrs, _OUTPUT_TOKEN_KEYS)),
        "cached_input_tokens": _int_or_none(_first(attrs, _CACHED_TOKEN_KEYS)),
        "retry_count": max(_int_or_none(attrs.get(_RETRY_COUNT)) or 0, 0),
        "input": _payload(_first(attrs, _INPUT_KEYS)),
        "output": _payload(_first(attrs, _OUTPUT_KEYS)),
        "attributes": {k: v for k, v in attrs.items() if k not in _PROMOTED},
        "events": events,
        "resource": resource,
    }


def _attributes(kvs: list[KeyValue]) -> dict[str, Any]:
    return {_clean(kv.key): _any_value(kv.value) for kv in kvs}


def _any_value(value: AnyValue) -> Any:
    which = value.WhichOneof("value")
    if which == "string_value":
        return _clean(value.string_value)
    if which == "bool_value":
        return value.bool_value
    if which == "int_value":
        return value.int_value
    if which == "double_value":
        return value.double_value
    if which == "array_value":
        return [_any_value(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return _attributes(value.kvlist_value.values)
    if which == "bytes_value":
        return value.bytes_value.hex()
    return None


def _clean(text: str) -> str:
    # PostgreSQL text and JSONB cannot store NUL characters.
    return text.replace("\x00", "")


def _datetime(unix_nano: int) -> datetime:
    return datetime.fromtimestamp(unix_nano / 1e9, tz=UTC)


def _first(attrs: dict[str, Any], keys: tuple[str, ...]) -> Any:
    return next((attrs[k] for k in keys if k in attrs), None)


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)
