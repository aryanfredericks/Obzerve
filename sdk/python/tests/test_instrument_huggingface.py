import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

huggingface_hub = pytest.importorskip("huggingface_hub")

from obzerve.replay import LLM_LIVE, RECORDED, bundle_from_spans  # noqa: E402

CHAT = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "tgi-llama",
    "system_fingerprint": "x",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Context recall."},
            "finish_reason": "stop",
            "logprobs": None,
        }
    ],
    "usage": {"prompt_tokens": 21, "completion_tokens": 4, "total_tokens": 25},
}


def chat_chunk(content=None, finish=None, usage=None):
    return {
        "id": "c2",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "tgi-llama",
        "system_fingerprint": "x",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content or ""},
                "finish_reason": finish,
                "logprobs": None,
            }
        ],
        "usage": usage,
    }


@pytest.fixture
def tgi():
    """A fake TGI server: OpenAI-style chat at /v1/chat/completions, generation at /."""
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if body.get("stream"):
                chunks = [
                    chat_chunk("Context "),
                    chat_chunk(
                        "recall.",
                        finish="stop",
                        usage={"prompt_tokens": 21, "completion_tokens": 4, "total_tokens": 25},
                    ),
                ]
                data = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
                data += b"data: [DONE]\n\n"
                ctype = "text/event-stream"
            else:
                data = json.dumps(
                    CHAT if "messages" in body else [{"generated_text": "Context recall."}]
                ).encode()
                ctype = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()


MESSAGES = [{"role": "user", "content": "What does context recall measure?"}]


def test_inference_client_chat_completion(instrumented, tgi):
    url, _ = tgi
    client = huggingface_hub.InferenceClient(base_url=url)
    with instrumented.observer.trace("agent"):
        out = client.chat_completion(messages=MESSAGES, max_tokens=64)
        alias = client.chat.completions.create(messages=MESSAGES)
    assert out.choices[0].message.content == alias.choices[0].message.content
    spans = instrumented.spans("huggingface.chat_completion")
    assert len(spans) == 2  # the OpenAI-style alias is traced too
    a = spans[0].attributes
    assert a["gen_ai.provider.name"] == "huggingface"
    assert a["server.address"] == "127.0.0.1"
    assert a["gen_ai.response.model"] == "tgi-llama"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"]) == (21, 4)
    assert a["gen_ai.request.max_tokens"] == 64
    assert a["obzerve.replay.complete"] is True

    # An endpoint client with a per-call model name: the name is the model, the URL the server.
    client.chat_completion(messages=MESSAGES, model="qwen3:latest")
    named = instrumented.spans("huggingface.chat_completion")[-1].attributes
    assert named["gen_ai.request.model"] == "qwen3:latest"
    assert named["server.address"] == "127.0.0.1"


def test_inference_client_stream_and_text_generation(instrumented, tgi):
    url, _ = tgi
    client = huggingface_hub.InferenceClient(base_url=url)
    parts = [
        c.choices[0].delta.content for c in client.chat_completion(messages=MESSAGES, stream=True)
    ]
    assert "".join(parts) == "Context recall."
    a = instrumented.one("huggingface.chat_completion").attributes
    assert a["obzerve.output"] == "Context recall."
    assert a["gen_ai.usage.output_tokens"] == 4

    assert client.text_generation("context recall is", max_new_tokens=8) == "Context recall."
    t = instrumented.one("huggingface.text_generation").attributes
    assert t["gen_ai.operation.name"] == "text_completion"
    assert t["gen_ai.request.max_tokens"] == 8


def test_inference_client_recorded_replay(instrumented, tgi):
    url, requests = tgi
    rec = instrumented

    def agent():
        client = huggingface_hub.InferenceClient(base_url=url)
        with rec.observer.trace("agent", input={"q": "recall"}):
            return client.chat_completion(messages=MESSAGES)

    agent()
    rec.observer.flush()
    bundle = bundle_from_spans(rec.exporter.get_finished_spans(), rec.observer.last_trace_id)
    sent = len(requests)
    with rec.observer.replay(bundle, RECORDED):
        out = agent()
    assert len(requests) == sent
    assert isinstance(out, huggingface_hub.ChatCompletionOutput)
    assert out.usage.prompt_tokens == 21


# ---- transformers (local models) ----

torch = pytest.importorskip("torch")
transformers = pytest.importorskip("transformers")


@pytest.fixture(scope="module")
def tiny():
    """A tiny randomly initialised GPT-2 and word-level tokenizer: no download."""
    from tokenizers import Tokenizer, models, pre_tokenizers

    words = ["[PAD]", "[UNK]", "[EOS]", "context", "recall", "faithfulness", "is", "measured"]
    vocab = {w: i for i, w in enumerate(words + [f"w{i}" for i in range(56)])}
    backend = Tokenizer(models.WordLevel(vocab=vocab, unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = transformers.PreTrainedTokenizerFast(
        tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]", eos_token="[EOS]"
    )
    torch.manual_seed(0)
    config = transformers.GPT2Config(
        vocab_size=len(vocab),
        n_positions=64,
        n_embd=16,
        n_layer=1,
        n_head=2,
        pad_token_id=0,
        eos_token_id=2,
        bos_token_id=2,
    )
    model = transformers.GPT2LMHeadModel(config).eval()
    model.config._name_or_path = "tiny-test-gpt2"
    model.name_or_path = "tiny-test-gpt2"
    return model, tokenizer


@pytest.fixture
def hf_instrumented(rec):
    handle = rec.observer.instrument(transformers=True)
    yield rec
    handle.uninstrument()


def test_generate_records_model_and_tokens(hf_instrumented, tiny):
    model, tokenizer = tiny
    rec = hf_instrumented
    inputs = tokenizer(["context recall is"], return_tensors="pt")
    with rec.observer.trace("agent"):
        out = model.generate(
            **inputs, max_new_tokens=5, do_sample=False, min_new_tokens=5, pad_token_id=0
        )
    new_tokens = out.shape[-1] - inputs["input_ids"].shape[-1]
    span = rec.one("transformers.generate")
    a = span.attributes
    assert a["gen_ai.request.model"] == "tiny-test-gpt2"
    assert a["gen_ai.provider.name"] == "huggingface"
    assert a["gen_ai.usage.input_tokens"] == 3
    assert a["gen_ai.usage.output_tokens"] == new_tokens == 5
    assert a["gen_ai.request.max_tokens"] == 5
    assert a["transformers.device"] == "cpu"
    assert a["obzerve.replay.complete"] is True


def test_pipeline_wraps_generate_with_readable_text(hf_instrumented, tiny):
    model, tokenizer = tiny
    generator = transformers.pipeline(
        "text-generation", model=model, tokenizer=tokenizer, device="cpu"
    )
    result = generator("context recall is", max_new_tokens=3, do_sample=False, pad_token_id=0)
    rec = hf_instrumented
    pipeline_span = rec.one("transformers.pipeline")
    generate_span = rec.one("transformers.generate")
    assert generate_span.parent.span_id == pipeline_span.context.span_id
    assert pipeline_span.attributes["obzerve.span.kind"] == "chain"  # tokens counted once
    assert "context recall is" in pipeline_span.attributes["obzerve.input"]
    assert result[0]["generated_text"].startswith("context recall is")


def test_generate_recorded_replay_skips_the_model(hf_instrumented, tiny, monkeypatch):
    model, tokenizer = tiny
    rec = hf_instrumented
    inputs = tokenizer(["faithfulness is measured"], return_tensors="pt")

    def agent():
        with rec.observer.trace("agent", input={"q": "faithfulness"}):
            return model.generate(**inputs, max_new_tokens=4, do_sample=False, pad_token_id=0)

    original = agent()
    rec.observer.flush()
    bundle = bundle_from_spans(rec.exporter.get_finished_spans(), rec.observer.last_trace_id)

    def explode(*args, **kwargs):
        raise AssertionError("the model must not run during a recorded replay")

    monkeypatch.setattr(model, "forward", explode)
    with rec.observer.replay(bundle, RECORDED) as session:
        replayed = agent()
    assert torch.equal(replayed, original.cpu())
    assert session.divergences == []

    with pytest.raises(AssertionError, match="must not run"):  # LLM_LIVE really runs it
        with rec.observer.replay(bundle, LLM_LIVE):
            agent()
