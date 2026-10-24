"""OTLP/HTTP trace ingestion: POST /v1/traces (protobuf or JSON).

In ``direct`` mode spans are persisted before responding. In ``queue`` mode the
validated payload is appended to Redis and a worker persists it, so ingestion
keeps working through database slowdowns and outages. Either way, a temporary
failure answers 503 with ``Retry-After``, which OTLP exporters retry.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from obzerve_server import metrics
from obzerve_server.auth import SCOPE_INGEST, Principal, require_scope
from obzerve_server.errors import is_redis_unavailable
from obzerve_server.ingest import persist_spans
from obzerve_server.otlp import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_PROTOBUF,
    PayloadError,
    PayloadTooLarge,
    decode_spans,
    decompress,
    parse_request,
)
from obzerve_server.queue import QueueFull

router = APIRouter(tags=["ingest"])


class ServiceUnavailable(HTTPException):
    def __init__(self, detail: str, retry_after_s: int):
        super().__init__(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail,
            headers={"Retry-After": str(retry_after_s)},
        )


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge(f"Body exceeds {limit} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge(f"Body exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/v1/traces")
async def ingest_traces(
    request: Request,
    principal: Principal = Depends(require_scope(SCOPE_INGEST, cache=True)),
) -> Response:
    settings = request.app.state.settings
    mode = settings.ingest_mode
    outcome = metrics.INGEST_REQUESTS.labels(mode, "accepted")
    content_type = request.headers.get("content-type", "")
    try:
        body = await _read_body(request, settings.max_ingest_bytes)
        body = decompress(body, request.headers.get("content-encoding"), settings.max_ingest_bytes)
        message = parse_request(body, content_type)
    except PayloadTooLarge as exc:
        metrics.INGEST_REQUESTS.labels(mode, "too_large").inc()
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    except PayloadError as exc:
        metrics.INGEST_REQUESTS.labels(mode, "invalid").inc()
        code = (
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            if "Content-Type" in str(exc)
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, str(exc)) from exc

    decoded = decode_spans(message)
    if decoded.spans:
        if mode == "queue":
            await _enqueue(request, principal, message.SerializeToString())
        else:
            async with request.app.state.sessionmaker() as session:
                inserted = await persist_spans(session, principal.project_id, decoded.spans)
            metrics.SPANS_PERSISTED.labels("inserted").inc(inserted)
            metrics.SPANS_PERSISTED.labels("duplicate").inc(len(decoded.spans) - inserted)

    outcome.inc()
    metrics.INGEST_SPANS.labels("accepted").inc(len(decoded.spans))
    metrics.INGEST_SPANS.labels("rejected").inc(decoded.rejected)

    reply = ExportTraceServiceResponse()
    if decoded.rejected:
        reply.partial_success.rejected_spans = decoded.rejected
        reply.partial_success.error_message = "; ".join(decoded.errors)
    if content_type.split(";")[0].strip().lower() == CONTENT_TYPE_JSON:
        return Response(json_format.MessageToJson(reply), media_type=CONTENT_TYPE_JSON)
    return Response(reply.SerializeToString(), media_type=CONTENT_TYPE_PROTOBUF)


async def _enqueue(request: Request, principal: Principal, payload: bytes) -> None:
    settings = request.app.state.settings
    try:
        await request.app.state.queue.enqueue(principal.project_id, payload)
    except QueueFull as exc:
        metrics.INGEST_REQUESTS.labels("queue", "backpressure").inc()
        raise ServiceUnavailable(str(exc), settings.retry_after_s) from exc
    except Exception as exc:
        if not is_redis_unavailable(exc):
            raise
        metrics.INGEST_REQUESTS.labels("queue", "unavailable").inc()
        raise ServiceUnavailable("Ingest queue unavailable", settings.retry_after_s) from exc
