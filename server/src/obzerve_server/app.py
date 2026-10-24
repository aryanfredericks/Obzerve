import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from obzerve_server import __version__, metrics
from obzerve_server.auth import KeyCache
from obzerve_server.config import Settings, get_settings
from obzerve_server.db import create_engine, create_sessionmaker
from obzerve_server.errors import is_transient_db_error
from obzerve_server.queue import IngestQueue, create_redis
from obzerve_server.routes import evals, health, ingest, metrics_api, replay, traces

logger = logging.getLogger("obzerve.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings)
    redis = create_redis(settings) if settings.ingest_mode == "queue" else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await engine.dispose()
        if redis is not None:
            await redis.aclose()

    app = FastAPI(title="Obzerve", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.state.key_cache = KeyCache(settings.api_key_cache_ttl_s)
    app.state.queue = IngestQueue(redis, settings) if redis is not None else None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    @app.middleware("http")
    async def record_latency(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        metrics.HTTP_REQUEST_SECONDS.labels(
            request.method, getattr(route, "path", "unmatched"), str(response.status_code)
        ).observe(time.perf_counter() - started)
        return response

    async def unavailable_dependency(request: Request, exc: Exception):
        # The database being down is a temporary condition clients should retry,
        # not an internal error.
        if not is_transient_db_error(exc):
            logger.error("database error", exc_info=exc)
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        metrics.DB_ERRORS.labels("api").inc()
        logger.warning("database unavailable: %s", exc)
        return JSONResponse(
            {"detail": "Database unavailable"},
            status_code=503,
            headers={"Retry-After": str(settings.retry_after_s)},
        )

    for exc_type in (DBAPIError, SQLAlchemyTimeoutError, OSError, TimeoutError):
        app.add_exception_handler(exc_type, unavailable_dependency)

    if settings.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        async def prometheus_metrics() -> Response:
            if app.state.queue is not None:
                try:
                    depth = await app.state.queue.depth()
                    metrics.QUEUE_DEPTH.set(depth.length)
                    metrics.QUEUE_PENDING.set(depth.pending)
                    metrics.QUEUE_DEAD_LETTERS.set(depth.dead_letters)
                except Exception as exc:  # keep serving the other metrics
                    logger.warning("could not read queue depth: %s", exc)
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(traces.router)
    app.include_router(metrics_api.router)
    app.include_router(replay.router)
    app.include_router(evals.router)
    return app
