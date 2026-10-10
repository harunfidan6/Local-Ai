"""ClaudeProvider'ı gerçek anthropic SDK'sı ile, sahte bir yerel HTTP sunucusuna karşı test eder."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

anthropic = pytest.importorskip("anthropic")

from localai.providers.base import ToolResult  # noqa: E402
from localai.providers.claude import ClaudeProvider, _echoable_content  # noqa: E402
from localai.tools import TOOL_SPECS  # noqa: E402


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def tool_use_stream():
    return [
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 1,
                      "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Dosyayı okuyorum."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"a.py\"}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ]


@pytest.fixture
def server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "headers": dict(self.headers), "body": body})
            payload = sse(tool_use_stream())
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", received
    httpd.shutdown()


def test_request_shape_and_tool_call_parsing(server):
    url, received = server
    client = anthropic.Anthropic(api_key="test", base_url=url, max_retries=0)
    provider = ClaudeProvider("claude-opus-5-5", "sistem", TOOL_SPECS, client=client)
    provider.add_user("a.py'yi oku")

    pieces = []
    step = provider.step(on_delta=pieces.append)

    assert pieces == ["Dosyayı okuyorum."] and step.streamed
    assert step.stop == "tool_use"
    assert step.text == "Dosyayı okuyorum."
    assert step.tool_calls[0].name == "read_file"
    assert step.tool_calls[0].arguments == {"path": "a.py"}
    assert step.usage.input_tokens == 100
    assert step.usage.output_tokens == 20
    assert step.usage.cache_read_tokens == 50

    req = received[0]
    body = req["body"]
    assert "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")
    assert body["fallbacks"] == "default"
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert body["stream"] is True
    assert all(t["eager_input_streaming"] for t in body["tools"])

    # Asistan cevabı geçmişe eklendi; araç sonucu tek user mesajında gider
    provider.add_tool_results([ToolResult("toolu_1", "read_file", "içerik")])
    assert provider.messages[1]["role"] == "assistant"
    assert provider.messages[2]["content"][0]["tool_use_id"] == "toolu_1"


def test_haiku_gets_no_fallback(server):
    url, received = server
    client = anthropic.Anthropic(api_key="test", base_url=url, max_retries=0)
    provider = ClaudeProvider("claude-haiku-5-5", "sistem", TOOL_SPECS, client=client)
    provider.add_user("selam")
    provider.step()
    assert "fallbacks" not in received[0]["body"]


class Block:
    def __init__(self, type):
        self.type = type


def test_blocks_before_fallback_boundary_are_dropped():
    types = ["thinking", "text", "tool_use", "fallback", "thinking", "tool_use"]
    blocks = [Block(t) for t in types]
    kept = [b.type for b in _echoable_content(blocks)]
    assert kept == ["text", "fallback", "thinking", "tool_use"]
