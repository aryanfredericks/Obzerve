"""Queue worker: persists OTLP payloads from the Redis ingest stream.

Failure handling:

* **Database unavailable** (transient): the message stays unacknowledged, the
  worker pauses until PostgreSQL answers again, then re-processes its own
  pending messages. Outages do not count toward a message's delivery limit.
* **Invalid message** (permanent: undecodable payload, unknown project,
  out-of-range data): moved to the dead-letter stream immediately.
* **Unexpected error**: the message stays pending; it is reclaimed after
  ``worker_claim_idle_ms`` and dead-lettered once it has been delivered
  ``worker_max_deliveries`` times.
* **Crashed worker**: its unacknowledged messages are reclaimed by any other
  worker (or by itself after a restart) once they have been idle long enough.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid

from redis.asyncio import Redis
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from obzerve_server import metrics
from obzerve_server.config import Settings
from obzerve_server.errors import is_redis_unavailable, is_transient_db_error
from obzerve_server.ingest import persist_spans
from obzerve_server.otlp import CONTENT_TYPE_PROTOBUF, PayloadError, decode_spans, parse_request
from obzerve_server.queue import FIELD_ENQUEUED_AT, FIELD_PAYLOAD, FIELD_PROJECT, IngestQueue

logger = logging.getLogger("obzerve.worker")

_MAX_BACKOFF_S = 30.0


class _DatabaseUnavailable(Exception):
    pass


class _PermanentFailure(Exception):
    pass


class IngestWorker:
    def __init__(
        self,
        redis: Redis,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        consumer: str | None = None,
    ):
        self.queue = IngestQueue(redis, settings)
        self.redis = redis
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self._stop = asyncio.Event()
        # Re-read this consumer's own unacknowledged messages on the next iteration
        # (at start-up and after recovering from a database outage).
        self._recheck_own_pending = True

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Process messages until ``stop()`` is called; the message in flight is finished."""
        await self.queue.ensure_group()
        logger.info("worker %s consuming %s", self.consumer, self.queue.stream)
        backoff = 0.5
        while not self._stop.is_set():
            try:
                await self.run_once()
                backoff = 0.5
            except _DatabaseUnavailable:
                logger.warning("database unavailable; pausing until it recovers")
                await self._wait_for_database()
            except Exception as exc:
                # Never exit on an error: a worker that dies silently stops ingestion.
                if is_redis_unavailable(exc):
                    logger.warning("redis unavailable (%s); retrying in %.1fs", exc, backoff)
                else:
                    logger.exception("worker iteration failed; retrying in %.1fs", backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
        logger.info("worker %s stopped", self.consumer)

    async def run_once(self, block: bool = True) -> int:
        """One polling round. Returns the number of messages handled."""
        handled = 0
        batch = self.settings.worker_batch_size
        stream = self.queue.stream

        if self._recheck_own_pending:
            own = await self.redis.xreadgroup(
                self.queue.group, self.consumer, {stream: "0"}, count=batch
            )
            entries = own[0][1] if own else []
            self._recheck_own_pending = len(entries) == batch
            handled += await self._handle(entries)

        _, claimed, _ = await self.redis.xautoclaim(
            stream,
            self.queue.group,
            self.consumer,
            min_idle_time=self.settings.worker_claim_idle_ms,
            start_id="0-0",
            count=batch,
        )
        if claimed:
            handled += await self._handle(claimed, deliveries=await self._delivery_counts(claimed))

        fresh = await self.redis.xreadgroup(
            self.queue.group,
            self.consumer,
            {stream: ">"},
            count=batch,
            block=self.settings.worker_block_ms if block and not handled else None,
        )
        if fresh:
            handled += await self._handle(fresh[0][1])
        return handled

    async def _delivery_counts(self, entries) -> dict[bytes, int]:
        ids = [message_id for message_id, _ in entries]
        info = await self.redis.xpending_range(
            self.queue.stream,
            self.queue.group,
            min=min(ids),
            max=max(ids),
            count=len(ids) * 2,
            consumername=self.consumer,
        )
        return {item["message_id"]: item["times_delivered"] for item in info}

    async def _handle(self, entries, deliveries: dict[bytes, int] | None = None) -> int:
        handled = 0
        for message_id, fields in entries:
            if self._stop.is_set():
                break
            if fields is None:  # deleted while pending
                continue
            attempts = (deliveries or {}).get(message_id, 1)
            if attempts > self.settings.worker_max_deliveries:
                await self._dead_letter(message_id, fields, "exceeded max deliveries", attempts)
                handled += 1
                continue
            try:
                await self._process(fields)
            except _DatabaseUnavailable:
                metrics.WORKER_MESSAGES.labels("retry_later").inc()
                # Retry from this consumer's own pending list (which does not count as a
                # new delivery) rather than via XAUTOCLAIM (which would), so an outage
                # cannot push healthy messages into the dead-letter stream.
                self._recheck_own_pending = True
                raise
            except _PermanentFailure as exc:
                await self._dead_letter(message_id, fields, str(exc), attempts)
            except Exception:
                logger.exception("unexpected error processing %s; will retry", message_id)
                metrics.WORKER_MESSAGES.labels("retry_later").inc()
            else:
                await self._ack(message_id)
                metrics.WORKER_MESSAGES.labels("processed").inc()
            handled += 1
        return handled

    async def _process(self, fields: dict[bytes, bytes]) -> None:
        try:
            project_id = uuid.UUID(fields[FIELD_PROJECT].decode())
            message = parse_request(fields[FIELD_PAYLOAD], CONTENT_TYPE_PROTOBUF)
        except (KeyError, ValueError, PayloadError) as exc:
            raise _PermanentFailure(f"invalid message: {exc}") from exc

        decoded = decode_spans(message)
        try:
            async with self.sessionmaker() as session:
                inserted = await persist_spans(session, project_id, decoded.spans)
        except (sa_exc.IntegrityError, sa_exc.DataError) as exc:
            # e.g. the project was deleted after the message was enqueued
            raise _PermanentFailure(f"rejected by database: {exc.orig}") from exc
        except Exception as exc:
            if is_transient_db_error(exc):
                metrics.DB_ERRORS.labels("worker").inc()
                raise _DatabaseUnavailable() from exc
            raise

        metrics.SPANS_PERSISTED.labels("inserted").inc(inserted)
        metrics.SPANS_PERSISTED.labels("duplicate").inc(len(decoded.spans) - inserted)
        enqueued_ms = fields.get(FIELD_ENQUEUED_AT)
        if enqueued_ms:
            metrics.WORKER_LAG_SECONDS.observe(max(time.time() - int(enqueued_ms) / 1000, 0))

    async def _ack(self, message_id: bytes) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(self.queue.stream, self.queue.group, message_id)
            pipe.xdel(self.queue.stream, message_id)
            await pipe.execute()

    async def _dead_letter(
        self, message_id: bytes, fields: dict[bytes, bytes], reason: str, attempts: int
    ) -> None:
        logger.error("dead-lettering %s after %d attempt(s): %s", message_id, attempts, reason)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(
                self.queue.dead_letter_stream,
                {
                    **fields,
                    b"original_id": message_id,
                    b"error": reason[:2000].encode(),
                    b"attempts": str(attempts).encode(),
                    b"failed_at_ms": str(int(time.time() * 1000)).encode(),
                },
            )
            pipe.xack(self.queue.stream, self.queue.group, message_id)
            pipe.xdel(self.queue.stream, message_id)
            await pipe.execute()
        metrics.WORKER_MESSAGES.labels("dead_lettered").inc()

    async def _wait_for_database(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                async with self.sessionmaker() as session:
                    await session.execute(text("SELECT 1"))
                logger.info("database reachable again; resuming")
                return
            except Exception as exc:
                if not is_transient_db_error(exc):
                    logger.exception("unexpected error while waiting for the database")
            await self._sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass
