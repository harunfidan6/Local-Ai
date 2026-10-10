"""Ollama sağlayıcısı ve CLI'yi sahte bir Ollama sunucusuna karşı uçtan uca test eder."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from localai.cli import main
from localai.providers.base import ProviderError, ToolResult
from localai.providers.ollama import OllamaProvider
from localai.tools import TOOL_SPECS


@pytest.fixture
def ollama():
    """İlk çağrıda read_file ister, ikincide cevap verir."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._json({"models": [{"name": "qwen2.5-coder:7b"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            if body["model"] == "olmayan-model":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error": "model not found"}')
                return
            if len(received) % 2 == 1:
                message = {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "read_file", "arguments": {"path": "not.txt"}}}]}
            else:
                message = {"role": "assistant", "content": "Dosyada 'gizli kelime' yazıyor."}
            self._json({"message": message, "prompt_eval_count": 42, "eval_count": 7, "done_reason": "stop"})

        def _json(self, obj):
            data = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", received
    httpd.shutdown()


def test_ollama_provider_round_trip(ollama):
    host, received = ollama
    provider = OllamaProvider("qwen2.5-coder:7b", "sistem", TOOL_SPECS, host=host, num_ctx=8192)
    provider.add_user("not.txt'yi oku")

    step = provider.step()
    assert step.stop == "tool_use"
    assert step.tool_calls[0].arguments == {"path": "not.txt"}
    assert step.usage.input_tokens == 42

    body = received[0]
    assert body["options"]["num_ctx"] == 8192
    assert body["messages"][0] == {"role": "system", "content": "sistem"}
    assert body["tools"][0]["type"] == "function"

    provider.add_tool_results([ToolResult(step.tool_calls[0].id, "read_file", "gizli kelime")])
    assert provider.step().text.startswith("Dosyada")
    assert received[1]["messages"][-1] == {"role": "tool", "tool_name": "read_file", "content": "gizli kelime"}


def test_missing_model_gives_pull_hint(ollama):
    host, _ = ollama
    provider = OllamaProvider("olmayan-model", "sistem", TOOL_SPECS, host=host)
    provider.add_user("selam")
    with pytest.raises(ProviderError, match="ollama pull olmayan-model"):
        provider.step()


def test_unreachable_ollama():
    provider = OllamaProvider("x", "sistem", TOOL_SPECS, host="http://127.0.0.1:9", timeout=2)
    provider.add_user("selam")
    with pytest.raises(ProviderError, match="bağlanılamadı"):
        provider.step()


def test_cli_one_shot(ollama, tmp_path, monkeypatch, capsys):
    host, _ = ollama
    (tmp_path / "not.txt").write_text("gizli kelime", encoding="utf-8")
    monkeypatch.setenv("LOCALAI_OLLAMA_HOST", host)
    monkeypatch.setenv("LOCALAI_HOME", str(tmp_path / ".home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    code = main(["-w", str(tmp_path), "not.txt'de ne yazıyor?"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Bulut: kapalı" in out
    assert "read_file" in out
    assert "gizli kelime" in out
    assert "ücretsiz" in out
