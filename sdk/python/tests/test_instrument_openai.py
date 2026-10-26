import asyncio
import json

import pytest
from opentelemetry.trace import StatusCode

openai = pytest.importorskip("openai")
httpx2 = pytest.importorskip("httpx2")

from obzerve.replay import LLM_LIVE, RECORDED, ReplayMissError, bundle_from_spans  # noqa: E402

CHAT = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "gpt-x-2026",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Faithfulness."},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 40,
        "completion_tokens": 3,
        "total_tokens": 43,
        "prompt_tokens_details": {"cached_tokens": 32},
    },
}


def sse(events: list) -> bytes:
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events) + b"data: [DONE]\n\n"


def chunk(content=None, finish=None, usage=None, choices=True):
    body = {
        "id": "chatcmpl-2",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-x-2026",
        "choices": [],
    }
    if choices:
        body["choices"] = [
            {"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}
        ]
    if usage:
        body["usage"] = usage
    return body


STREAM = [
    chunk("Faith"),
    chunk("fulness."),
    chunk(finish="stop"),
    chunk(choices=False, usage={"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43}),
]


class Server:
    """A fake OpenAI endpoint behind an httpx2 transport."""

    def __init__(self, status=200):
        self.requests: list[dict] = []
        self.status = status

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "boom"}})
        if request.url.path.endswith("/embeddings"):
            return httpx2.Response(
                200,
                json={
                    "object": "list",
                    "model": "emb-1",
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                    "usage": {"prompt_tokens": 4, "total_tokens": 4},
                },
            )
        if body.get("stream"):
            return httpx2.Response(
                200, content=sse(STREAM), headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(200, json=CHAT)

    def client(self, base_url="https://api.openai.com/v1"):
        transport = httpx2.MockTransport(self.handler)
        return openai.OpenAI(
            api_key="test",
            base_url=base_url,
            max_retries=0,
            http_client=openai.DefaultHttpxClient(transport=transport),
        )

    def async_client(self):
        transport = httpx2.MockTransport(self.handler)
        return openai.AsyncOpenAI(
            api_key="test",
            max_retries=0,
            http_client=openai.DefaultAsyncHttpxClient(transport=transport),
        )


MESSAGES = [{"role": "user", "content": "What is faithfulness?"}]


def test_chat_completion_becomes_an_llm_span(instrumented):
    rec, server = instrumented, Server()
    with rec.observer.trace("agent") as trace:
        reply = server.client().chat.completions.create(
            model="gpt-x", messages=MESSAGES, temperature=0.2, max_completion_tokens=50
        )
    assert reply.choices[0].message.content == "Faithfulness."

    span = rec.one("openai.chat.completions")
    a = span.attributes
    assert span.parent.span_id == rec.one("agent").context.span_id
    assert format(span.context.trace_id, "032x") == trace.trace_id
    assert a["obzerve.span.kind"] == "llm"
    assert a["gen_ai.provider.name"] == "openai"
    assert a["gen_ai.operation.name"] == "chat"
    assert a["server.address"] == "api.openai.com"
    assert a["gen_ai.request.model"] == "gpt-x"
    assert a["gen_ai.response.model"] == "gpt-x-2026"
    assert a["gen_ai.request.temperature"] == 0.2
    assert a["gen_ai.request.max_tokens"] == 50
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"]) == (40, 3)
    assert a["gen_ai.usage.cache_read.input_tokens"] == 32
    assert a["gen_ai.response.finish_reasons"] == ("stop",)
    assert "What is faithfulness?" in a["obzerve.input"]
    assert json.loads(a["obzerve.output"])["choices"][0]["message"]["content"] == "Faithfulness."
    assert a["obzerve.replay.key"] == "openai.chat.completions#1"
    assert a["obzerve.replay.complete"] is True
    assert span.status.status_code is StatusCode.OK


def test_compatible_servers_are_not_reported_as_openai(instrumented):
    Server().client("http://localhost:11434/v1").chat.completions.create(
        model="qwen3", messages=MESSAGES
    )
    a = instrumented.one("openai.chat.completions").attributes
    assert a["gen_ai.provider.name"] == "openai_compatible"
    assert a["server.address"] == "localhost"


def test_streamed_chat_is_traced_until_the_stream_ends(instrumented):
    rec, server = instrumented, Server()
    stream = server.client().chat.completions.create(
        model="gpt-x", messages=MESSAGES, stream=True, stream_options={"include_usage": True}
    )
    assert rec.spans("openai.chat.completions") == []  # still open while unread
    text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
    assert text == "Faithfulness."

    a = rec.one("openai.chat.completions").attributes
    assert a["obzerve.output"] == "Faithfulness."
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"]) == (40, 3)
    assert a["gen_ai.response.finish_reasons"] == ("stop",)
    assert a["gen_ai.request.stream"] is True
    assert a["obzerve.replay.complete"] is False
    assert "not recorded for replay" in a["obzerve.replay.incomplete_reason"]


def test_stream_closed_early_still_ends_its_span(instrumented):
    stream = (
        Server().client().chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True)
    )
    with stream:
        next(iter(stream))
    span = instrumented.one("openai.chat.completions")
    assert span.attributes["obzerve.output"] == "Faith"


def test_async_chat_and_stream(instrumented):
    rec, server = instrumented, Server()

    async def run():
        client = server.async_client()
        reply = await client.chat.completions.create(model="gpt-x", messages=MESSAGES)
        stream = await client.chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True)
        parts = [c.choices[0].delta.content async for c in stream if c.choices]
        return reply, "".join(p for p in parts if p)

    reply, text = asyncio.run(run())
    assert reply.choices[0].message.content == text == "Faithfulness."
    spans = rec.spans("openai.chat.completions")
    assert len(spans) == 2
    assert {s.attributes.get("obzerve.output") for s in spans} >= {"Faithfulness."}


def test_api_errors_are_recorded_and_raised(instrumented):
    with pytest.raises(openai.InternalServerError):
        Server(status=500).client().chat.completions.create(model="gpt-x", messages=MESSAGES)
    span = instrumented.one("openai.chat.completions")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "InternalServerError"
    assert span.attributes["obzerve.replay.error_class"] == "openai:InternalServerError"


def test_embeddings_record_input_tokens(instrumented):
    response = Server().client().embeddings.create(model="emb-1", input="hello world")
    assert response.data[0].embedding == [0.1, 0.2]
    a = instrumented.one("openai.embeddings").attributes
    assert a["gen_ai.operation.name"] == "embeddings"
    assert a["gen_ai.usage.input_tokens"] == 4


def test_recorded_replay_returns_the_sdk_object_without_calling_the_api(instrumented):
    rec, server = instrumented, Server()

    def agent(client):
        with rec.observer.trace("agent", input={"q": "faithfulness"}) as trace:
            reply = client.chat.completions.create(model="gpt-x", messages=MESSAGES)
            trace.set_output(reply.choices[0].message.content)
            return reply

    original = agent(server.client())
    rec.observer.flush()
    bundle = bundle_from_spans(rec.exporter.get_finished_spans(), rec.observer.last_trace_id)

    offline = Server()
    with rec.observer.replay(bundle, RECORDED) as session:
        replayed = agent(offline.client())
    assert offline.requests == []  # nothing was sent
    assert isinstance(replayed, openai.types.chat.ChatCompletion)
    assert replayed.choices[0].message.content == original.choices[0].message.content
    assert replayed.usage.prompt_tokens == 40
    assert session.recorded_calls == 1

    with rec.observer.replay(bundle, LLM_LIVE):
        agent(offline.client())
    assert len(offline.requests) == 1  # the model ran live


def test_streams_cannot_be_replayed_from_recordings_yet(instrumented):
    rec, server = instrumented, Server()
    with rec.observer.trace("agent", input={"q": 1}):
        list(server.client().chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True))
    rec.observer.flush()
    bundle = bundle_from_spans(rec.exporter.get_finished_spans(), rec.observer.last_trace_id)
    with pytest.raises(ReplayMissError, match="streamed responses"):
        with rec.observer.replay(bundle, RECORDED), rec.observer.trace("agent", input={"q": 1}):
            server.client().chat.completions.create(model="gpt-x", messages=MESSAGES, stream=True)


def test_telemetry_failures_never_break_the_call(instrumented, monkeypatch):
    from obzerve.instrumentation import openai as integration

    def broken(*args, **kwargs):
        raise RuntimeError("SDK changed shape")

    monkeypatch.setattr(integration, "_after_chat", broken)
    handle = instrumented.observer.instrument()  # re-patch, picking up the broken hook
    reply = Server().client().chat.completions.create(model="gpt-x", messages=MESSAGES)
    assert reply.choices[0].message.content == "Faithfulness."
    span = instrumented.one("openai.chat.completions")  # still traced, minus usage
    assert span.status.status_code is StatusCode.OK
    assert "gen_ai.usage.input_tokens" not in span.attributes
    handle.uninstrument()


def test_uninstrument_restores_the_sdk(rec):
    handle = rec.observer.instrument(
        openai=True, anthropic=False, huggingface_hub=False, transformers=False
    )
    assert handle.libraries == {"openai": 6}
    handle.uninstrument()
    Server().client().chat.completions.create(model="gpt-x", messages=MESSAGES)
    assert rec.spans("openai.chat.completions") == []
