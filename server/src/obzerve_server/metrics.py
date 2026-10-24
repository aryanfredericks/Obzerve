"""Prometheus metrics for monitoring Obzerve itself."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUEST_SECONDS = Histogram(
    "obzerve_http_request_seconds",
    "HTTP request latency by route",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

INGEST_REQUESTS = Counter(
    "obzerve_ingest_requests_total",
    "OTLP ingest requests by outcome",
    ["mode", "outcome"],  # outcome: accepted, invalid, too_large, backpressure, unavailable
)
INGEST_SPANS = Counter(
    "obzerve_ingest_spans_total",
    "Spans received by the ingest API",
    ["outcome"],  # accepted, rejected
)
SPANS_PERSISTED = Counter(
    "obzerve_spans_persisted_total",
    "Spans written to PostgreSQL",
    ["outcome"],  # inserted, duplicate
)
DB_ERRORS = Counter(
    "obzerve_db_errors_total",
    "Transient database errors (database unreachable or connection lost)",
    ["component"],
)

QUEUE_DEPTH = Gauge("obzerve_queue_depth", "Messages waiting in the ingest stream")
QUEUE_PENDING = Gauge(
    "obzerve_queue_pending", "Messages delivered to a worker but not yet acknowledged"
)
QUEUE_DEAD_LETTERS = Gauge("obzerve_queue_dead_letters", "Messages in the dead-letter stream")

WORKER_MESSAGES = Counter(
    "obzerve_worker_messages_total",
    "Queue messages handled by workers",
    ["outcome"],  # processed, dead_lettered, retry_later
)
WORKER_LAG_SECONDS = Histogram(
    "obzerve_worker_lag_seconds",
    "Time from enqueue to persistence",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)
