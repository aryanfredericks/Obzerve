"""Redis Streams ingest queue.

The API appends one message per accepted OTLP request; workers in a consumer
group persist them. Delivery is at-least-once (a message is acknowledged only
after its transaction commits), which is safe because persistence is
idempotent. Acknowledged messages are deleted, so the stream length is the
backlog that drives backpressure.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from obzerve_server.config import Settings

FIELD_PROJECT = b"project_id"
FIELD_PAYLOAD = b"payload"
FIELD_ENQUEUED_AT = b"enqueued_at_ms"


class QueueFull(Exception):
    pass


@dataclass
class QueueDepth:
    length: int
    pending: int
    dead_letters: int


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.redis_url,
        socket_timeout=5,
        socket_connect_timeout=2,
        health_check_interval=30,
    )


class IngestQueue:
    def __init__(self, redis: Redis, settings: Settings):
        self.redis = redis
        self.stream = settings.queue_stream
        self.dead_letter_stream = settings.queue_dead_letter_stream
        self.group = settings.queue_group
        self.max_length = settings.queue_max_length

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP: the group already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, project_id: uuid.UUID, payload: bytes) -> str:
        """Append a validated OTLP payload. Raises ``QueueFull`` when the backlog is too long."""
        if await self.redis.xlen(self.stream) >= self.max_length:
            raise QueueFull(f"ingest backlog exceeds {self.max_length} messages")
        message_id = await self.redis.xadd(
            self.stream,
            {
                FIELD_PROJECT: str(project_id).encode(),
                FIELD_PAYLOAD: payload,
                FIELD_ENQUEUED_AT: str(int(time.time() * 1000)).encode(),
            },
        )
        return message_id.decode()

    async def depth(self) -> QueueDepth:
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.xlen(self.stream)
            pipe.xlen(self.dead_letter_stream)
            length, dead = await pipe.execute()
        try:
            pending = (await self.redis.xpending(self.stream, self.group))["pending"]
        except Exception:  # the group does not exist until a worker or enqueue creates it
            pending = 0
        return QueueDepth(length=length, pending=pending, dead_letters=dead)
