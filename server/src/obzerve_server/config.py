from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBZERVE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://obzerve:obzerve@localhost:5432/obzerve"
    # Disable connection pooling (useful in tests that create several event loops).
    database_null_pool: bool = False
    database_pool_size: int = 10
    # Bound how long a request waits for a connection or a statement before failing fast.
    database_pool_timeout_s: float = 5.0
    database_statement_timeout_ms: int = 15_000

    # Upper bound on a single OTLP request body, after decompression.
    max_ingest_bytes: int = 5 * 1024 * 1024
    cors_origins: list[str] = ["http://localhost:5173"]
    # How long a valid ingest API key is cached; also the delay before a revocation applies.
    api_key_cache_ttl_s: float = 60.0
    # Serve Prometheus metrics at /metrics (unauthenticated: expose only to your scraper).
    metrics_enabled: bool = True

    # "direct": the API writes spans to PostgreSQL before responding.
    # "queue": the API validates and enqueues to Redis Streams; `obzerve-server worker` persists.
    ingest_mode: Literal["direct", "queue"] = "direct"
    redis_url: str = "redis://localhost:6379/0"
    queue_stream: str = "obzerve:ingest"
    queue_dead_letter_stream: str = "obzerve:ingest:dead"
    queue_group: str = "obzerve-workers"
    # Backpressure: above this many unprocessed messages the API answers 503 + Retry-After.
    queue_max_length: int = 10_000
    retry_after_s: int = 2

    # Worker
    worker_batch_size: int = 50
    worker_block_ms: int = 1000
    # A message not acknowledged for this long (e.g. its worker crashed) is reclaimed.
    worker_claim_idle_ms: int = 30_000
    # After this many delivery attempts a message moves to the dead-letter stream.
    worker_max_deliveries: int = 5
    worker_metrics_port: int = 9101


@lru_cache
def get_settings() -> Settings:
    return Settings()
