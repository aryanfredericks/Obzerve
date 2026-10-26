"""Hugging Face ``transformers``: any local model's ``generate()``, plus text
generation pipelines.

``generate()`` becomes an LLM span with the model name, device, sampling
settings, and token counts (prompt tokens from the attention mask; output tokens
are the newly generated ones). Its output tensor is recorded, so a recorded
replay returns it without running the model. A text-generation pipeline call
becomes a parent span carrying the readable text in and out; its ``generate()``
span holds the token counts (so tokens are counted once).
"""

from __future__ import annotations

from typing import Any

from obzerve import attributes as attrs
from obzerve.attributes import SpanKind
from obzerve.instrumentation._common import Call, Patches, safely, set_present
from obzerve.observer import ObzerveSpan

GENERATE = "transformers.generate"
PIPELINE = "transformers.pipeline"


def _is_tensor(value: Any) -> bool:
    return type(value).__module__.startswith("torch") and hasattr(value, "tolist")


def _encode(output: Any) -> Any:
    if _is_tensor(output):
        return {"tensor": output.tolist(), "dtype": str(output.dtype).removeprefix("torch.")}
    raise TypeError(f"{type(output).__name__} output (return_dict_in_generate?) is not recorded")


def _decode(data: Any) -> Any:
    import torch

    return torch.tensor(data["tensor"], dtype=getattr(torch, data["dtype"]))


def _normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    """Tensors -> lists, so the input hash is independent of device and repr()."""

    def plain(value: Any) -> Any:
        if _is_tensor(value):
            return value.tolist()
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    return {k: plain(v) for k, v in arguments.items() if k != "generation_config"}


def _prompt(args: tuple, kwargs: dict) -> tuple[Any, Any]:
    inner = kwargs.get("kwargs", {}) if isinstance(kwargs.get("kwargs"), dict) else {}
    ids = kwargs.get("input_ids", kwargs.get("inputs", inner.get("input_ids")))
    if ids is None and len(args) > 1:
        ids = args[1]
    return ids, kwargs.get("attention_mask", inner.get("attention_mask"))


def _before(span: ObzerveSpan, args: tuple, kwargs: dict) -> None:
    model = args[0]
    config = getattr(model, "generation_config", None)
    ids, mask = _prompt(args, kwargs)
    prompt_tokens = None
    if mask is not None and _is_tensor(mask):
        prompt_tokens = int(mask.sum())
    elif ids is not None and _is_tensor(ids):
        prompt_tokens = int(ids.numel())
    span.set_llm(
        model=getattr(model, "name_or_path", None) or getattr(model.config, "_name_or_path", None),
        provider="huggingface",
        input_tokens=prompt_tokens,
    )
    set_present(
        span,
        {
            "gen_ai.operation.name": "text_completion",
            "gen_ai.request.max_tokens": kwargs.get(
                "max_new_tokens", getattr(config, "max_new_tokens", None)
            ),
            "gen_ai.request.temperature": kwargs.get(
                "temperature", getattr(config, "temperature", None)
            ),
            "transformers.do_sample": kwargs.get("do_sample", getattr(config, "do_sample", None)),
            "transformers.device": str(getattr(model, "device", "")),
        },
    )
    span._obzerve_prompt_shape = (  # used by _after to count new tokens
        tuple(ids.shape) if ids is not None and _is_tensor(ids) else None,
        bool(getattr(model.config, "is_encoder_decoder", False)),
    )


def _after(span: ObzerveSpan, output: Any) -> None:
    sequences = output if _is_tensor(output) else getattr(output, "sequences", None)
    if sequences is None or not _is_tensor(sequences):
        return
    shape, encoder_decoder = getattr(span, "_obzerve_prompt_shape", (None, False))
    if encoder_decoder or shape is None:
        generated = int(sequences.numel())
    else:
        generated = int(sequences.shape[0]) * max(int(sequences.shape[-1]) - shape[-1], 0)
    span.set_llm(output_tokens=generated)


def _wrap_generate(observer):
    def make(original):
        call = Call(
            observer,
            GENERATE,
            original,
            kind=SpanKind.LLM,
            encode=_encode,
            decode=_decode,
            before=_before,
            after=_after,
            normalize_input=_normalize,
        )

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return call.run(args, kwargs)

        return wrapper

    return make


def _wrap_pipeline(observer):
    def make(original):
        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            prompt = args[0] if args else kwargs.get("text_inputs")
            with observer.span(PIPELINE, kind=SpanKind.CHAIN, input=prompt) as span:
                safely(set_present)(
                    span,
                    {
                        "transformers.task": getattr(self, "task", None),
                        attrs.GEN_AI_REQUEST_MODEL: getattr(
                            getattr(self, "model", None), "name_or_path", None
                        ),
                    },
                )
                result = original(self, *args, **kwargs)
                safely(span.set_output)(result)
                return result

        return wrapper

    return make


def instrument(observer, patches: Patches) -> int:
    from transformers.generation.utils import GenerationMixin
    from transformers.pipelines.text_generation import TextGenerationPipeline

    count = patches.wrap(GenerationMixin, "generate", _wrap_generate(observer))
    count += patches.wrap(TextGenerationPipeline, "__call__", _wrap_pipeline(observer))
    return count
