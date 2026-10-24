"""Queue-mode ingestion and worker failure handling (needs Redis)."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from obzerve_server.auth import Principal, hash_api_key
from obzerve_server.config import Settings
from obzerve_server.db import create_engine, create_sessionmaker
from obzerve_server.queue import FIELD_ENQUEUED_AT, FIELD_PAYLOAD, FIELD_PROJECT, IngestQueue
from obzerve_server.worker import IngestWorker, _DatabaseUnavailable

PROTOBUF = {"Content-Type": "application/x-protobuf"}
T0 = datetime(2026, 4, 1, tzinfo=UTC)
DEAD_DB = "postgresql+asyncpg://obzerve:obzerve@127.0.0.1:1/obzerve"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def queue_settings(redis_url, database_url):
    """Unique stream names per test so tests never see each other's messages."""
    prefix = f"test:{uuid.uuid4().hex[:8]}"

    def factory(**overrides) -> dict:
        return {
            "ingest_mode": "queue",
            "redis_url": redis_url,
            "queue_stream": f"{prefix}:ingest",
            "queue_dead_letter_stream": f"{prefix}:dead",
            "worker_block_ms": 10,
            "worker_claim_idle_ms": 0,
            "database_url": database_url,
            "database_null_pool": True,
            **overrides,
        }

    return factory


@pytest.fixture
async def redis(redis_url):
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


def make_worker(settings_dict: dict, redis: Redis, *, database_url=None, consumer="w1"):
    settings = Settings(
        **(settings_dict | ({"database_url": database_url} if database_url else {}))
    )
    engine = create_engine(settings)
    return IngestWorker(redis, create_sessionmaker(engine), settings, consumer=consumer), engine


def one_trace(otlp, trace_id="a1"):
    return (
        otlp()
        .span(trace_id, "1", "agent", start=T0, duration_ms=100, kind="agent")
        .span(trace_id, "2", "tool", start=T0, duration_ms=10, parent="1", kind="tool")
    )


async def span_count(client, key, trace_id="a1") -> int | None:
    resp = await client.get("/api/v1/traces/" + trace_id.rjust(32, "0"), headers=auth(key))
    return resp.json()["trace"]["span_count"] if resp.status_code == 200 else None


async def test_enqueue_then_worker_persists(
    make_app, client_for, make_project, otlp, queue_settings, redis
):
    _, key = await make_project()
    settings = queue_settings()
    app = make_app(**settings)
    worker, engine = make_worker(settings, redis)
    await worker.queue.ensure_group()

    async with client_for(app) as client:
        resp = await client.post(
            "/v1/traces", content=one_trace(otlp).body(), headers=auth(key) | PROTOBUF
        )
        assert resp.status_code == 200
        assert await span_count(client, key) is None  # accepted, not yet persisted
        assert (await app.state.queue.depth()).length == 1

        assert await worker.run_once(block=False) == 1
        assert await span_count(client, key) == 2
        depth = await app.state.queue.depth()
        assert (depth.length, depth.pending, depth.dead_letters) == (0, 0, 0)

        metrics = (await client.get("/metrics")).text
        assert 'obzerve_ingest_requests_total{mode="queue",outcome="accepted"}' in metrics
        assert "obzerve_queue_depth 0.0" in metrics
    await engine.dispose()


async def test_redelivered_messages_are_idempotent(
    make_app, client_for, make_project, otlp, queue_settings, redis
):
    project_id, key = await make_project()
    settings = queue_settings()
    worker, engine = make_worker(settings, redis)
    await worker.queue.ensure_group()
    for _ in range(3):  # e.g. an exporter retry after a lost response
        await worker.queue.enqueue(project_id, one_trace(otlp).body())
    assert await worker.run_once(block=False) == 3

    async with client_for(make_app()) as client:
        assert await span_count(client, key) == 2
    await engine.dispose()


async def test_backpressure_returns_503_with_retry_after(
    make_app, client_for, make_project, otlp, queue_settings
):
    _, key = await make_project()
    app = make_app(**queue_settings(queue_max_length=2, retry_after_s=7))
    async with client_for(app) as client:
        codes = []
        for i in range(3):
            resp = await client.post(
                "/v1/traces", content=one_trace(otlp, f"b{i}").body(), headers=auth(key) | PROTOBUF
            )
            codes.append(resp.status_code)
        assert codes == [200, 200, 503]
        assert resp.headers["retry-after"] == "7"


async def test_redis_down_returns_503(make_app, client_for, make_project, otlp, queue_settings):
    _, key = await make_project()
    app = make_app(**queue_settings(redis_url="redis://127.0.0.1:1/0"))
    async with client_for(app) as client:
        resp = await client.post(
            "/v1/traces", content=one_trace(otlp).body(), headers=auth(key) | PROTOBUF
        )
        assert resp.status_code == 503
        assert "retry-after" in resp.headers
        ready = await client.get("/readyz")
        assert ready.status_code == 503 and ready.json()["queue"] == "unreachable"


async def test_queue_mode_accepts_traces_while_database_is_down(
    make_app, client_for, otlp, queue_settings, redis
):
    # The key was validated (and cached) before the outage.
    app = make_app(**queue_settings(database_url=DEAD_DB))
    project_id = uuid.uuid4()
    app.state.key_cache.put(
        hash_api_key("cached-key"),
        Principal(project_id=project_id, key_id=uuid.uuid4(), scopes=frozenset({"ingest"})),
    )
    async with client_for(app) as client:
        resp = await client.post(
            "/v1/traces", content=one_trace(otlp).body(), headers=auth("cached-key") | PROTOBUF
        )
        assert resp.status_code == 200
        # An uncached key needs the database: temporary failure, not 401 or 500.
        resp = await client.post(
            "/v1/traces", content=one_trace(otlp).body(), headers=auth("other-key") | PROTOBUF
        )
        assert resp.status_code == 503
    assert (await app.state.queue.depth()).length == 1


async def test_worker_survives_database_outage_without_losing_messages(
    make_app, client_for, make_project, otlp, queue_settings, redis
):
    project_id, key = await make_project()
    settings = queue_settings(worker_max_deliveries=1)
    outage_worker, dead_engine = make_worker(settings, redis, database_url=DEAD_DB)
    await outage_worker.queue.ensure_group()
    await outage_worker.queue.enqueue(project_id, one_trace(otlp).body())

    for _ in range(3):  # repeated attempts while the database is down
        with pytest.raises(_DatabaseUnavailable):
            await outage_worker.run_once(block=False)
    depth = await outage_worker.queue.depth()
    assert (depth.length, depth.pending, depth.dead_letters) == (1, 1, 0)

    # Database is back: the same consumer re-processes its pending message, and the
    # outage did not count toward the delivery limit (max 1) — it is not dead-lettered.
    recovered, engine = make_worker(settings, redis)
    assert await recovered.run_once(block=False) == 1
    async with client_for(make_app()) as client:
        assert await span_count(client, key) == 2
    depth = await recovered.queue.depth()
    assert (depth.length, depth.pending, depth.dead_letters) == (0, 0, 0)
    await dead_engine.dispose()
    await engine.dispose()


async def test_poison_and_orphaned_messages_are_dead_lettered(
    make_project, otlp, queue_settings, redis
):
    settings = queue_settings()
    worker, engine = make_worker(settings, redis)
    await worker.queue.ensure_group()
    stream = settings["queue_stream"]
    await redis.xadd(stream, {FIELD_PROJECT: b"not-a-uuid", FIELD_PAYLOAD: b"x"})
    await redis.xadd(
        stream, {FIELD_PROJECT: str(uuid.uuid4()).encode(), FIELD_PAYLOAD: b"\xff\xff"}
    )
    # A well-formed payload for a project that no longer exists.
    await worker.queue.enqueue(uuid.uuid4(), one_trace(otlp).body())

    assert await worker.run_once(block=False) == 3
    depth = await worker.queue.depth()
    assert (depth.length, depth.pending, depth.dead_letters) == (0, 0, 3)
    dead = await redis.xrange(settings["queue_dead_letter_stream"])
    errors = [fields[b"error"].decode() for _, fields in dead]
    assert "invalid message" in errors[0]
    assert "invalid message" in errors[1]
    assert "rejected by database" in errors[2]
    await engine.dispose()


async def test_messages_of_a_crashed_worker_are_reclaimed(
    make_app, client_for, make_project, otlp, queue_settings, redis
):
    project_id, key = await make_project()
    settings = queue_settings()
    queue = IngestQueue(redis, Settings(**settings))
    await queue.ensure_group()
    await queue.enqueue(project_id, one_trace(otlp).body())
    # "crashed" consumer takes the message and never acknowledges it
    taken = await redis.xreadgroup(
        settings.get("queue_group", "obzerve-workers"),
        "crashed",
        {settings["queue_stream"]: ">"},
        count=10,
    )
    assert len(taken[0][1]) == 1

    survivor, engine = make_worker(settings, redis, consumer="survivor")
    assert await survivor.run_once(block=False) == 1
    async with client_for(make_app()) as client:
        assert await span_count(client, key) == 2
    assert (await queue.depth()).pending == 0
    await engine.dispose()


async def test_repeatedly_failing_message_is_dead_lettered_after_max_deliveries(
    make_project, otlp, queue_settings, redis, monkeypatch
):
    project_id, _ = await make_project()
    settings = queue_settings(worker_max_deliveries=3)
    worker, engine = make_worker(settings, redis)
    await worker.queue.ensure_group()
    await worker.queue.enqueue(project_id, one_trace(otlp).body())

    async def boom(fields):
        raise RuntimeError("bug in processing")

    monkeypatch.setattr(worker, "_process", boom)
    for _ in range(6):
        await worker.run_once(block=False)
    depth = await worker.queue.depth()
    assert (depth.length, depth.pending, depth.dead_letters) == (0, 0, 1)
    _, fields = (await redis.xrange(settings["queue_dead_letter_stream"]))[0]
    assert fields[b"error"] == b"exceeded max deliveries"
    assert int(fields[b"attempts"]) == 4
    assert fields[FIELD_ENQUEUED_AT]
    await engine.dispose()


async def test_worker_stops_gracefully(queue_settings, redis):
    worker, engine = make_worker(queue_settings(), redis)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.1)
    worker.stop()
    await asyncio.wait_for(task, timeout=2)
    await engine.dispose()
