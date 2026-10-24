"""Project API keys: generation, hashing, and request authentication."""

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy import select

from obzerve_server.models import ProjectApiKey

SCOPE_INGEST = "ingest"
SCOPE_READ = "read"
# Register prompt versions, create eval cases, submit eval runs.
SCOPE_WRITE = "write"
ALL_SCOPES = (SCOPE_INGEST, SCOPE_READ, SCOPE_WRITE)

KEY_PREFIX = "obz_"


@dataclass(frozen=True)
class Principal:
    project_id: uuid.UUID
    key_id: uuid.UUID
    scopes: frozenset[str]


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return request.headers.get("x-api-key")


class KeyCache:
    """Short-lived cache of valid keys, so authenticating ingest traffic does not
    need a database round trip per request (and queue-mode ingestion keeps working
    while PostgreSQL is down). A revoked key stays usable for at most ``ttl_s``."""

    def __init__(self, ttl_s: float, max_entries: int = 10_000):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._entries: dict[str, tuple[Principal, float]] = {}

    def get(self, key_hash: str) -> Principal | None:
        entry = self._entries.get(key_hash)
        if entry is None:
            return None
        principal, expires = entry
        if time.monotonic() >= expires:
            self._entries.pop(key_hash, None)
            return None
        return principal

    def put(self, key_hash: str, principal: Principal) -> None:
        if len(self._entries) >= self.max_entries:
            self._entries.clear()
        self._entries[key_hash] = (principal, time.monotonic() + self.ttl_s)


async def _lookup(request: Request, key_hash: str) -> Principal | None:
    async with request.app.state.sessionmaker() as session:
        key = await session.scalar(
            select(ProjectApiKey).where(
                ProjectApiKey.key_hash == key_hash, ProjectApiKey.revoked_at.is_(None)
            )
        )
    if key is None:
        return None
    return Principal(project_id=key.project_id, key_id=key.id, scopes=frozenset(key.scopes))


def require_scope(scope: str, *, cache: bool = False):
    """Authenticate the request's API key and require ``scope``. With ``cache``,
    valid keys are remembered for ``api_key_cache_ttl_s`` (used for ingestion)."""

    async def dependency(request: Request) -> Principal:
        token = _bearer_token(request)
        if not token:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Missing API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        key_hash = hash_api_key(token)
        key_cache: KeyCache = request.app.state.key_cache
        principal = key_cache.get(key_hash) if cache else None
        if principal is None:
            principal = await _lookup(request, key_hash)
            if principal is not None and cache:
                key_cache.put(key_hash, principal)
        if principal is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if scope not in principal.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"API key lacks the '{scope}' scope")
        return principal

    return dependency
