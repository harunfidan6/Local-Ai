"""Yerel model sağlayıcısı: Ollama'nın /api/chat uç noktası.

Ek bağımlılık gerektirmemesi için sadece standart kütüphane (urllib) kullanır.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from ..tools import ToolSpec
from .base import ProviderError, StepResult, ToolCall, ToolResult, Usage


class OllamaProvider:
    name = "local"

    def __init__(
        self,
        model: str,
        system_prompt: str,
        tools: list[ToolSpec],
        host: str = "http://127.0.0.1:11434",
        num_ctx: int = 16384,
        temperature: float = 0.2,
        timeout: float = 600,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.tool_names = {t.name for t in tools}
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.timeout = timeout
        self._call_counter = 0
        self.reset()

    def reset(self) -> None:
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            content = f"HATA: {r.content}" if r.is_error else r.content
            self.messages.append({"role": "tool", "tool_name": r.name, "content": content})

    def _tool_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in self.tools
        ]

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 404:
                raise ProviderError(
                    f"Ollama '{self.model}' modelini bulamadı. Önce indir: ollama pull {self.model}"
                ) from e
            raise ProviderError(f"Ollama HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise ProviderError(
                f"Ollama'ya bağlanılamadı ({self.host}). Ollama açık mı? "
                "Windows'ta uygulamayı başlat, Linux/macOS'ta 'ollama serve' çalıştır."
            ) from e

    def step(self) -> StepResult:
        data = self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": self.messages,
                "tools": self._tool_schema(),
                "stream": False,
                "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
            },
        )
        message = data.get("message") or {}
        content = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []

        # Geçmişe, modelin gönderdiği haliyle ekle
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        if raw_calls:
            assistant["tool_calls"] = raw_calls
        self.messages.append(assistant)

        calls = [self._to_call(c.get("function", {})) for c in raw_calls]
        if not calls:
            # Küçük modeller araç çağrısını bazen düz metin JSON olarak yazar
            calls = parse_text_tool_calls(content, self.tool_names, self._next_id)
            if calls:
                content = ""

        usage = Usage(
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
        )
        stop = "tool_use" if calls else ("max_tokens" if data.get("done_reason") == "length" else "end")
        return StepResult(text=content, tool_calls=calls, usage=usage, model=self.model, stop=stop)

    def _next_id(self) -> str:
        self._call_counter += 1
        return f"call_{self._call_counter}"

    def _to_call(self, fn: dict[str, Any]) -> ToolCall:
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        return ToolCall(id=self._next_id(), name=fn.get("name", ""), arguments=args)


_FENCE_RE = re.compile(r"```(?:json|tool_call)?\s*(.*?)```", re.DOTALL)
_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def parse_text_tool_calls(content: str, tool_names: set[str], next_id) -> list[ToolCall]:
    """Metin içine gömülmüş araç çağrılarını yakalar.

    Desteklenen biçimler: <tool_call>{...}</tool_call>, ```json {...} ``` ve
    sadece JSON'dan oluşan cevap. JSON, {"name": ..., "arguments": {...}}
    biçiminde olmalı ve name bilinen bir araç olmalı.
    """
    candidates = _TAG_RE.findall(content) or _FENCE_RE.findall(content)
    if not candidates and content.strip().startswith("{"):
        candidates = [content.strip()]

    calls: list[ToolCall] = []
    for chunk in candidates:
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        for item in obj if isinstance(obj, list) else [obj]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            args = item.get("arguments", item.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    continue
            if name in tool_names and isinstance(args, dict):
                calls.append(ToolCall(id=next_id(), name=name, arguments=args))
    return calls
