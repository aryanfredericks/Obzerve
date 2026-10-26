"""Automatic instrumentation of LLM client libraries.

    from obzerve import Observer
    observer = Observer(endpoint=..., api_key=...)
    observer.instrument()          # or: obzerve.instrumentation.instrument(observer)

Every model call made through a supported library then becomes an LLM span
(model, provider, sampling settings, token usage, finish reason, inputs and
outputs subject to redaction and ``capture_payloads``) inside whatever trace or
span is active, and non-streaming calls are recorded for replay.

Supported libraries:

=================  ===============================================================
``openai``         Chat Completions, Responses, Embeddings; sync, async, streams.
                   OpenAI-compatible servers (vLLM, Ollama, TGI, ...) included.
``anthropic``      ``messages.create`` (incl. ``stream=True``) and ``messages.stream()``
``huggingface_hub`` ``InferenceClient`` ``chat_completion`` / ``text_generation``
``transformers``   any local model's ``generate()``, text-generation pipelines
=================  ===============================================================

Each library argument is ``True`` (instrument; fail if it is not installed),
``False`` (leave alone), or ``None`` (the default: instrument it if it is
installed). ``transformers`` is the exception to the default: importing it loads
PyTorch, so ``None`` instruments it only if your code has already imported it.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field

from obzerve.instrumentation._common import Patches
from obzerve.observer import Observer

logger = logging.getLogger("obzerve.instrumentation")

_LIBRARIES = {
    "openai": "obzerve.instrumentation.openai",
    "anthropic": "obzerve.instrumentation.anthropic",
    "huggingface_hub": "obzerve.instrumentation.huggingface_hub",
    "transformers": "obzerve.instrumentation.transformers",
}
# Only instrumented by default when the application has already imported them.
_HEAVY = {"transformers"}

_active: Instrumentation | None = None


@dataclass
class Instrumentation:
    """What ``instrument()`` patched; ``uninstrument()`` restores the originals."""

    libraries: dict[str, int] = field(default_factory=dict)
    _patches: Patches = field(default_factory=Patches, repr=False)

    def uninstrument(self) -> None:
        global _active
        self._patches.restore()
        self.libraries.clear()
        if _active is self:
            _active = None


def instrument(
    observer: Observer,
    *,
    openai: bool | None = None,
    anthropic: bool | None = None,
    huggingface_hub: bool | None = None,
    transformers: bool | None = None,
) -> Instrumentation:
    """Patch the selected libraries to report to ``observer``.

    Calling it again replaces the previous instrumentation (one observer at a time).
    """
    global _active
    if _active is not None:
        _active.uninstrument()
    choices = {
        "openai": openai,
        "anthropic": anthropic,
        "huggingface_hub": huggingface_hub,
        "transformers": transformers,
    }
    result = Instrumentation()
    for library, choice in choices.items():
        if choice is False:
            continue
        installed = importlib.util.find_spec(library) is not None
        if choice is None and (not installed or (library in _HEAVY and library not in sys.modules)):
            continue
        if not installed:
            raise ImportError(f"cannot instrument {library!r}: it is not installed")
        module = importlib.import_module(_LIBRARIES[library])
        try:
            result.libraries[library] = module.instrument(observer, result._patches)
        except Exception as exc:
            if choice is True:
                result.uninstrument()
                raise
            # An unsupported version must not stop the application.
            logger.warning("obzerve: could not instrument %s: %s", library, exc)
    _active = result
    return result


def uninstrument() -> None:
    """Undo the active instrumentation, if any."""
    if _active is not None:
        _active.uninstrument()


__all__ = ["Instrumentation", "instrument", "uninstrument"]
