import asyncio
import json

import pytest
from opentelemetry.trace import StatusCode

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from obzerve.replay import RECORDED, bundle_from_spans  # noqa: E402

MODEL = "claude-opus-5-5"
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": MODEL,
    "content": [{"type": "text", "text": "Groundedness checks claims against sources."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 12,
        "output_tokens": 9,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 20,
    },
}


def events() -> bytes:
    flow = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    **MESSAGE,
                    "content": [],
                    "stop_reason": None,
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 100,
                        "cache_creation_input_tokens": 20,
                    },
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Groundedness "},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "checks claims."},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 9},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in flow)


class Server:
    def __init__(self, status=200):
        self.requests: list[dict] = []
        self.status = status

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx2.Response(
                self.status,
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            )
        if body.get("stream"):
            return httpx2.Response(
                200, content=events(), headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(200, json=MESSAGE)

    def client(self):
        return anthropic.Anthropic(
            api_key="test",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(self.handler)),
        )

    def async_client(self):
        return anthropic.AsyncAnthropic(
            api_key="test",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self.handler)
            ),
        )


ARGS = {
    "model": MODEL,
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "What is groundedness?"}],
}


def assert_usage(attributes):
    # input includes cache reads and writes; cache reads are also reported as cached
    assert attributes["gen_ai.usage.input_tokens"] == 12 + 100 + 20
    assert attributes["gen_ai.usage.cache_read.input_tokens"] == 100
    assert attributes["gen_ai.usage.cache_creation.input_tokens"] == 20
    assert attributes["gen_ai.usage.output_tokens"] == 9


def test_messages_create(instrumented):
    rec = instrumented
    with rec.observer.trace("agent"):
        message = Server().client().messages.create(**ARGS)
    assert message.content[0].text.startswith("Groundedness")
    span = rec.one("anthropic.messages")
    a = span.attributes
    assert span.parent.span_id == rec.one("agent").context.span_id
    assert a["gen_ai.provider.name"] == "anthropic"
    assert a["gen_ai.request.model"] == a["gen_ai.response.model"] == MODEL
    assert a["gen_ai.request.max_tokens"] == 1024
    assert a["gen_ai.response.finish_reasons"] == ("end_turn",)
    assert a["server.address"] == "api.anthropic.com"
    assert_usage(a)
    assert a["obzerve.replay.complete"] is True


def test_create_with_stream_true(instrumented):
    stream = Server().client().messages.create(**ARGS, stream=True)
    text = "".join(e.delta.text for e in stream if e.type == "content_block_delta")
    assert text == "Groundedness checks claims."
    a = instrumented.one("anthropic.messages").attributes
    assert a["obzerve.output"] == text
    assert a["gen_ai.response.finish_reasons"] == ("end_turn",)
    assert_usage(a)


def test_messages_stream_helper(instrumented):
    rec = instrumented
    with Server().client().messages.stream(**ARGS) as stream:
        text = "".join(stream.text_stream)
        final = stream.get_final_message()
    assert text == "Groundedness checks claims."
    assert final.usage.output_tokens == 9
    a = rec.one("anthropic.messages").attributes
    assert a["obzerve.output"] == text
    assert a["gen_ai.request.stream"] is True
    assert_usage(a)


def test_async_create_and_stream_helper(instrumented):
    server = Server()

    async def run():
        client = server.async_client()
        message = await client.messages.create(**ARGS)
        async with client.messages.stream(**ARGS) as stream:
            text = "".join([t async for t in stream.text_stream])
        return message, text

    message, text = asyncio.run(run())
    assert text == "Groundedness checks claims."
    spans = instrumented.spans("anthropic.messages")
    assert len(spans) == 2
    for span in spans:
        assert_usage(span.attributes)


def test_errors_are_recorded_and_raised(instrumented):
    with pytest.raises(anthropic.APIStatusError) as raised:
        Server(status=529).client().messages.create(**ARGS)
    span = instrumented.one("anthropic.messages")
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == type(raised.value).__name__  # e.g. OverloadedError


def test_recorded_replay_rebuilds_the_message(instrumented):
    rec = instrumented

    def agent(client):
        with rec.observer.trace("agent", input={"q": "groundedness"}):
            return client.messages.create(**ARGS)

    agent(Server().client())
    rec.observer.flush()
    bundle = bundle_from_spans(rec.exporter.get_finished_spans(), rec.observer.last_trace_id)
    offline = Server()
    with rec.observer.replay(bundle, RECORDED):
        message = agent(offline.client())
    assert offline.requests == []
    assert isinstance(message, anthropic.types.Message)
    assert message.usage.cache_read_input_tokens == 100
    assert message.content[0].text == MESSAGE["content"][0]["text"]
