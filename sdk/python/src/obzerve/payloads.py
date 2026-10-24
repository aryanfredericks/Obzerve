"""Serialization, redaction, and truncation of captured inputs/outputs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"

DEFAULT_REDACT_KEYS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "private_key",
    "cookie",
)

# Values that look like credentials even when they are not under a sensitive key.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


@dataclass(frozen=True)
class ExactPayload:
    """A payload serialized for replay: ``text`` is exact JSON unless ``complete`` is False."""

    text: str
    complete: bool
    redacted: bool
    reason: str | None = None


class PayloadSerializer:
    def __init__(self, redact_keys: Iterable[str] = DEFAULT_REDACT_KEYS, max_chars: int = 8192):
        self._redact_keys = {k.lower() for k in redact_keys}
        self._max_chars = max_chars

    def serialize(self, value: Any) -> tuple[str, bool]:
        """Return ``(text, truncated)`` for a captured payload (display fidelity)."""
        text, truncated, _ = self.serialize_with_flags(value)
        return text, truncated

    def serialize_with_flags(self, value: Any) -> tuple[str, bool, bool]:
        """Return ``(text, truncated, redacted)``."""
        redacted, changed = self.redact(value)
        if isinstance(redacted, str):
            text = redacted
        else:
            try:
                text = json.dumps(redacted, default=repr, ensure_ascii=False)
            except (TypeError, ValueError):
                text = repr(redacted)
        if len(text) > self._max_chars:
            return text[: self._max_chars], True, changed
        return text, False, changed

    def serialize_exact(self, value: Any, max_chars: int) -> ExactPayload:
        """Serialize for replay. The result is ``complete`` only if it round-trips
        exactly: JSON-native, not redacted, and within ``max_chars``."""
        redacted, changed = self.redact(value)
        try:
            text = json.dumps(redacted, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            text, _ = self.serialize(value)
            return ExactPayload(text, False, changed, "not JSON-serializable")
        if len(text) > max_chars:
            return ExactPayload(text[:max_chars], False, changed, f"larger than {max_chars} chars")
        if changed:
            return ExactPayload(text, False, True, "contains redacted values")
        return ExactPayload(text, True, False)

    def redact(self, value: Any) -> tuple[Any, bool]:
        """Return ``(value with secrets replaced, whether anything was replaced)``."""
        hits = [0]
        result = self._redact(value, 0, hits)
        return result, hits[0] > 0

    def _redact(self, value: Any, depth: int, hits: list[int]) -> Any:
        if depth > 20:
            return "[MAX_DEPTH]"
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if str(k).lower() in self._redact_keys:
                    hits[0] += 1
                    out[k] = REDACTED
                else:
                    out[k] = self._redact(v, depth + 1, hits)
            return out
        if isinstance(value, (list, tuple, set)):
            return [self._redact(v, depth + 1, hits) for v in value]
        if isinstance(value, str):
            for pattern in _SECRET_VALUE_PATTERNS:
                value, n = pattern.subn(REDACTED, value)
                hits[0] += n
            return value
        return value
