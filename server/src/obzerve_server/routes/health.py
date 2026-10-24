from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Readiness: the dependencies this instance needs are reachable."""
    checks: dict[str, str] = {}
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unreachable"

    queue = request.app.state.queue
    if queue is not None:
        try:
            await queue.redis.ping()
            checks["queue"] = "ok"
        except Exception:
            checks["queue"] = "unreachable"

    # In queue mode ingestion only needs Redis; the dashboard still needs the database.
    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ready else "unavailable", **checks}
