"""Ingestion load test: concurrent OTLP senders, then a check that nothing was lost.

Each request carries several complete traces. Senders retry 429/503 responses
after ``Retry-After`` the way OTLP exporters do, so backpressure slows the test
down instead of losing data. At the end the script waits until every trace is
queryable (in queue mode, until workers have drained the backlog).

    uv run python tools/loadtest.py --duration 20 --concurrency 16
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import statistics
import time
from datetime import UTC, datetime, timedelta

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Status

KINDS = ["tool", "retrieval", "llm", "llm", "chain"]


def build_request(workflow: str, traces: int, spans_per_trace: int) -> bytes:
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    kv = resource.resource.attributes.add()
    kv.key, kv.value.string_value = "service.name", "loadtest"
    spans = resource.scope_spans.add().spans
    now = time.time_ns()
    for _ in range(traces):
        trace_id, root_id = secrets.token_bytes(16), secrets.token_bytes(8)
        root = spans.add()
        root.trace_id, root.span_id, root.name = trace_id, root_id, workflow
        root.start_time_unix_nano, root.end_time_unix_nano = now, now + 900_000_000
        root.status.code = Status.STATUS_CODE_OK
        _attr(root, "obzerve.span.kind", "agent")
        for i in range(spans_per_trace - 1):
            span = spans.add()
            span.trace_id, span.span_id, span.parent_span_id = (
                trace_id,
                secrets.token_bytes(8),
                root_id,
            )
            kind = KINDS[i % len(KINDS)]
            span.name = f"{kind}.op{i % 7}"
            span.start_time_unix_nano = now + i * 1_000_000
            span.end_time_unix_nano = span.start_time_unix_nano + (i % 20 + 1) * 5_000_000
            failed = secrets.randbelow(50) == 0
            span.status.code = Status.STATUS_CODE_ERROR if failed else Status.STATUS_CODE_OK
            _attr(span, "obzerve.span.kind", kind)
            if failed:
                _attr(span, "error.type", "SimulatedError")
            if kind == "llm":
                _attr(span, "gen_ai.request.model", "loadtest-model")
                _attr(span, "gen_ai.usage.input_tokens", 800 + i)
                _attr(span, "gen_ai.usage.output_tokens", 120)
            _attr(span, "obzerve.input", "x" * 200)
    return request.SerializeToString()


def _attr(span, key: str, value) -> None:
    kv = span.attributes.add()
    kv.key = key
    if isinstance(value, int):
        kv.value.int_value = value
    else:
        kv.value.string_value = value


async def sender(client, args, workflow, deadline, results) -> None:
    headers = {
        "Authorization": f"Bearer {args.api_key}",
        "Content-Type": "application/x-protobuf",
    }
    while time.monotonic() < deadline:
        body = build_request(workflow, args.traces_per_request, args.spans_per_trace)
        started = time.perf_counter()
        while True:
            try:
                resp = await client.post("/v1/traces", content=body, headers=headers)
            except httpx.HTTPError:
                results["errors"] += 1
                await asyncio.sleep(0.5)
                continue
            if resp.status_code in (429, 502, 503, 504):
                results["backpressure"] += 1
                await asyncio.sleep(float(resp.headers.get("retry-after", "1")))
                continue
            break
        results["latencies"].append(time.perf_counter() - started)
        if resp.status_code == 200:
            results["accepted_traces"] += args.traces_per_request
        else:
            results["errors"] += 1
            print(f"unexpected {resp.status_code}: {resp.text[:200]}")


async def count_traces(client, args, workflow, since: datetime) -> int:
    resp = await client.get(
        "/api/v1/metrics/summary",
        params={
            "workflow": workflow,
            "start": since.isoformat(),
            "end": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
        headers={"Authorization": f"Bearer {args.api_key}"},
    )
    if resp.status_code == 503:  # database still recovering: keep polling
        return -1
    resp.raise_for_status()
    return resp.json()["requests"]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--url", default=os.environ.get("OBZERVE_ENDPOINT", "http://localhost:8000")
    )
    parser.add_argument("--api-key", default=os.environ.get("OBZERVE_API_KEY", "dev_project_key"))
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--duration", type=float, default=20, help="seconds of sending")
    parser.add_argument("--traces-per-request", type=int, default=5)
    parser.add_argument("--spans-per-trace", type=int, default=10)
    parser.add_argument("--drain-timeout", type=float, default=120)
    args = parser.parse_args()

    workflow = f"loadtest-{secrets.token_hex(4)}"
    since = datetime.now(UTC) - timedelta(minutes=1)
    results = {"latencies": [], "accepted_traces": 0, "backpressure": 0, "errors": 0}
    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.url, timeout=30, limits=limits) as client:
        print(f"sending for {args.duration:.0f}s with {args.concurrency} senders ({workflow})")
        started = time.monotonic()
        deadline = started + args.duration
        await asyncio.gather(
            *(sender(client, args, workflow, deadline, results) for _ in range(args.concurrency))
        )
        elapsed = time.monotonic() - started

        expected = results["accepted_traces"]
        drain_started = time.monotonic()
        stored = 0
        while time.monotonic() - drain_started < args.drain_timeout:
            stored = await count_traces(client, args, workflow, since)
            if stored >= expected:
                break
            await asyncio.sleep(0.5)
        drain = time.monotonic() - drain_started

    lat = sorted(results["latencies"])
    spans = expected * args.spans_per_trace
    print(f"requests:            {len(lat)} ({len(lat) / elapsed:.0f}/s)")
    print(f"spans accepted:      {spans} ({spans / elapsed:.0f}/s)")
    if lat:
        p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) > 1 else lat[0]
        p50 = statistics.median(lat)
        print(f"request latency:     p50 {p50 * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms")
    print(f"backpressure (503):  {results['backpressure']}")
    print(f"errors:              {results['errors']}")
    print(f"traces stored:       {stored}/{expected} (drained {drain:.1f}s after sending stopped)")
    ok = stored == expected and results["errors"] == 0
    print("RESULT:", "OK - no telemetry lost" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
