"""Classification of failures into transient (retry later) and permanent."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

from redis import exceptions as redis_exc
from sqlalchemy import exc as sa_exc

# SQLSTATEs meaning "the server cannot serve you right now", not "your data is bad":
# connection exceptions (08), insufficient resources (53), operator intervention such
# as shutdown or "cannot connect now" (57P01-57P03), statement timeout (57014),
# serialization failure / deadlock (40001, 40P01).
_TRANSIENT_SQLSTATE_CLASSES = ("08", "53")
_TRANSIENT_SQLSTATES = {"57P01", "57P02", "57P03", "57014", "40001", "40P01"}


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception, the driver exception SQLAlchemy wrapped, and their causes."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack.extend([getattr(current, "orig", None), current.__cause__, current.__context__])


def is_transient_db_error(exc: BaseException) -> bool:
    """True when PostgreSQL is unreachable, restarting, overloaded, or the connection
    broke: retrying the same operation later can succeed."""
    for e in _chain(exc):
        if isinstance(e, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)):
            return True
        if isinstance(e, sa_exc.DBAPIError) and e.connection_invalidated:
            return True
        # Connection refused/reset can surface unwrapped from the async driver.
        if isinstance(e, (OSError, asyncio.TimeoutError)):
            return True
        sqlstate = getattr(e, "sqlstate", None)
        if isinstance(sqlstate, str) and (
            sqlstate in _TRANSIENT_SQLSTATES or sqlstate[:2] in _TRANSIENT_SQLSTATE_CLASSES
        ):
            return True
    return False


def is_redis_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, (redis_exc.ConnectionError, redis_exc.TimeoutError, OSError))
