import asyncio
import os
import uuid
from datetime import datetime

import asyncpg
import httpx
import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Status
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from obzerve import Observer
from obzerve_server.app import create_app
from obzerve_server.auth import ALL_SCOPES, generate_api_key, hash_api_key
from obzerve_server.cli import migrate
from obzerve_server.config import Settings
from obzerve_server.models import Project, ProjectApiKey

# In CI, a missing service must fail the run instead of silently skipping the server tests.
REQUIRE_SERVICES = os.environ.get("OBZERVE_REQUIRE_SERVICES") == "1"


def _unavailable(message: str) -> None:
    if REQUIRE_SERVICES:
        pytest.fail(f"{message} (OBZERVE_REQUIRE_SERVICES=1)", pytrace=False)
    pytest.skip(message)


TEST_DATABASE_URL = os.environ.get(
    "OBZERVE_TEST_DATABASE_URL",
    "postgresql+asyncpg://obzerve:obzerve@localhost:5432/obzerve_test",
)


async def _recreate_database(url: str) -> None:
    parsed = make_url(url)
    admin = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        database="postgres",
    )
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await admin.close()


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        asyncio.run(_recreate_database(TEST_DATABASE_URL))
    except (OSError, asyncpg.PostgresError) as exc:
        _unavailable(f"PostgreSQL unavailable for server tests ({exc}); run `podman compose up -d`")
    migrate(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def app(database_url):
    return create_app(
        Settings(database_url=database_url, database_null_pool=True, max_ingest_bytes=256 * 1024)
    )


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
async def _clean_tables(request):
    yield
    if "database_url" in request.fixturenames:
        engine = create_async_engine(request.getfixturevalue("database_url"), poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE projects, model_prices CASCADE"))
        await engine.dispose()


@pytest.fixture
def make_app(database_url):
    """Build an app with settings overrides (e.g. queue mode, a broken database)."""

    def factory(**overrides):
        settings = {
            "database_url": database_url,
            "database_null_pool": True,
            "max_ingest_bytes": 256 * 1024,
            **overrides,
        }
        return create_app(Settings(**settings))

    return factory


@pytest.fixture
def client_for():
    """``async with client_for(app) as client`` for apps built with ``make_app``."""

    def factory(app) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    return factory


@pytest.fixture
def make_project(app):
    async def factory(name: str | None = None, scopes=ALL_SCOPES, revoked: bool = False):
        key = generate_api_key()
        async with app.state.sessionmaker() as session:
            project = Project(name=name or f"p-{uuid.uuid4().hex[:8]}")
            session.add(project)
            await session.flush()
            api_key = ProjectApiKey(
                project_id=project.id,
                name="test",
                key_prefix=key[:12],
                key_hash=hash_api_key(key),
                scopes=list(scopes),
            )
            if revoked:
                from datetime import UTC, datetime

                api_key.revoked_at = datetime.now(UTC)
            session.add(api_key)
            await session.commit()
            return project.id, key

    return factory


class SpanRecorder:
    """Records spans with the real SDK and encodes them as an OTLP protobuf body."""

    def __init__(self):
        self.exporter = InMemorySpanExporter()
        self.observer = Observer(service_name="test-agent", exporter=self.exporter)

    def otlp_body(self) -> bytes:
        self.observer.flush()
        return encode_spans(self.exporter.get_finished_spans()).SerializeToString()


@pytest.fixture
def recorder():
    rec = SpanRecorder()
    yield rec
    rec.observer.shutdown()


class OtlpBuilder:
    """Builds OTLP requests with exact timestamps and attributes."""

    def __init__(self, service: str = "test-agent"):
        self.request = ExportTraceServiceRequest()
        resource_spans = self.request.resource_spans.add()
        attr = resource_spans.resource.attributes.add()
        attr.key, attr.value.string_value = "service.name", service
        self._spans = resource_spans.scope_spans.add().spans

    def span(
        self,
        trace_id: str,
        span_id: str,
        name: str,
        *,
        start: datetime,
        duration_ms: float,
        parent: str | None = None,
        kind: str = "function",
        error: str | None = None,
        **attributes,
    ) -> "OtlpBuilder":
        span = self._spans.add()
        span.trace_id = bytes.fromhex(trace_id.rjust(32, "0"))
        span.span_id = bytes.fromhex(span_id.rjust(16, "0"))
        if parent:
            span.parent_span_id = bytes.fromhex(parent.rjust(16, "0"))
        span.name = name
        start_ns = int(start.timestamp() * 1e9)
        span.start_time_unix_nano = start_ns
        span.end_time_unix_nano = start_ns + int(duration_ms * 1e6)
        attributes = {"obzerve.span.kind": kind, **attributes}
        if error:
            attributes["error.type"] = error
            span.status.code = Status.STATUS_CODE_ERROR
        else:
            span.status.code = Status.STATUS_CODE_OK
        for key, value in attributes.items():
            kv = span.attributes.add()
            kv.key = key.replace("__", ".")
            if isinstance(value, bool):
                kv.value.bool_value = value
            elif isinstance(value, int):
                kv.value.int_value = value
            elif isinstance(value, float):
                kv.value.double_value = value
            else:
                kv.value.string_value = str(value)
        return self

    def body(self) -> bytes:
        return self.request.SerializeToString()


@pytest.fixture
def otlp():
    return OtlpBuilder


async def _redis_reachable(url: str) -> bool:
    client = Redis.from_url(url, socket_connect_timeout=1)
    try:
        return await client.ping()
    except Exception:
        return False
    finally:
        await client.aclose()


@pytest.fixture
async def redis_url():
    url = os.environ.get("OBZERVE_TEST_REDIS_URL", "redis://localhost:6379/15")
    if not await _redis_reachable(url):
        _unavailable("Redis unavailable for queue tests; run `podman compose up -d`")
    yield url
    client = Redis.from_url(url)
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def live_server(database_url):
    """The API on a real port in a background thread, for SDK end-to-end tests."""
    import socket
    import threading
    import time

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Settings(database_url=database_url, database_null_pool=True))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
