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
from .base import DeltaFn, ProviderError, StepResult, ToolCall, ToolResult, Usage

# Bağlam penceresinin bu oranı dolunca eski mesajlar budanır; kalan kısım cevaba ayrılır
CONTEXT_FILL = 0.75
# Son bu kadar mesaja asla dokunulmaz (modelin şu an üzerinde çalıştığı şeyler)
KEEP_RECENT = 6
PRUNED = "[Bu eski araç çıktısı bağlam penceresinde yer açmak için kaldırıldı. Gerekirse aracı tekrar çağır.]"


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
        # Karakter/token oranı tahmini; gerçek sayımlarla (sadece aşağı doğru) güncellenir
        self.chars_per_token = 3.5
        self._schema_chars = len(json.dumps(self._tool_schema(), ensure_ascii=False))
        self.reset()

    def reset(self) -> None:
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        self.pruned_count = 0

    # -- bağlam yönetimi -----------------------------------------------------

    def _chars(self) -> int:
        total = self._schema_chars
        for m in self.messages:
            total += len(m.get("content") or "")
            if "tool_calls" in m:
                total += len(json.dumps(m["tool_calls"], ensure_ascii=False))
        return total

    def _fits(self) -> bool:
        return self._chars() <= self.num_ctx * CONTEXT_FILL * self.chars_per_token

    def fit_context(self) -> None:
        """Konuşma bağlam penceresine sığmıyorsa önce eski araç çıktılarını, sonra en eski turları siler.

        Ollama taşan konuşmanın başını sessizce keser (sistem talimatı dahil);
        bunun yerine neyin atılacağını burada kontrollü seçiyoruz.
        """
        if self._fits():
            return
        protected_from = len(self.messages) - KEEP_RECENT
        for m in self.messages[1:protected_from]:
            if m["role"] == "tool" and m["content"] != PRUNED and len(m["content"]) > len(PRUNED):
                m["content"] = PRUNED
                self.pruned_count += 1
                if self._fits():
                    return
        # Hâlâ sığmıyor: sistem mesajından sonraki en eski turu (bir sonraki user mesajına kadar) at
        while not self._fits():
            users = [i for i, m in enumerate(self.messages) if m["role"] == "user"]
            if len(users) < 2 or users[1] >= len(self.messages) - KEEP_RECENT:
                return  # son tura dokunma
            del self.messages[1:users[1]]
            self.pruned_count += 1

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

    def _open(self, path: str, payload: dict[str, Any]):
        req = urllib.request.Request(
            self.host + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
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

    def _chat(self, on_delta: DeltaFn | None) -> tuple[dict[str, Any], str, list, bool]:
        """Akışlı /api/chat çağrısı. (son parça, tüm metin, araç çağrıları, metin gösterildi mi) döner."""
        payload = {
            "model": self.model,
            "messages": self.messages,
            "tools": self._tool_schema(),
            "stream": True,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
        }
        content, calls, final = "", [], {}
        gate = _StreamGate(on_delta)
        with self._open("/api/chat", payload) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise ProviderError(f"Ollama hatası: {chunk['error']}")
                message = chunk.get("message") or {}
                if piece := message.get("content"):
                    content += piece
                    gate.feed(piece)
                calls.extend(message.get("tool_calls") or [])
                final = chunk
                if chunk.get("done"):
                    break
        gate.finish()
        return final, content, calls, gate.shown

    def step(self, on_delta: DeltaFn | None = None) -> StepResult:
        self.fit_context()
        sent_chars = self._chars()
        data, content, raw_calls, streamed = self._chat(on_delta)

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

        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        if prompt_tokens > 200:
            # Önbellekten gelen tokenlar sayılmayabilir; bu yüzden oran sadece küçülebilir
            self.chars_per_token = max(2.0, min(self.chars_per_token, sent_chars / prompt_tokens))

        usage = Usage(
            input_tokens=prompt_tokens,
            output_tokens=int(data.get("eval_count") or 0),
        )
        stop = "tool_use" if calls else ("max_tokens" if data.get("done_reason") == "length" else "end")
        return StepResult(content, calls, usage, self.model, stop, streamed=streamed and bool(content))

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


class _StreamGate:
    """Metni ekrana akıtır; ama cevap bir araç çağrısı JSON'u gibi başlıyorsa göstermez.

    Küçük modeller araç çağrısını bazen düz metin olarak yazar; bunun kullanıcıya
    ham JSON olarak akmasını istemeyiz. İlk birkaç karakter gelene kadar bekler.
    """

    MARKERS = ("{", "[", "<tool_call>", "```json", "```tool")
    HOLD = 12

    def __init__(self, on_delta: DeltaFn | None):
        self.on_delta = on_delta
        self.buffer = ""
        self.decided = on_delta is None
        self.shown = False

    def feed(self, piece: str) -> None:
        if self.decided:
            if self.shown:
                self.on_delta(piece)
            return
        self.buffer += piece
        head = self.buffer.lstrip()
        if any(head.startswith(m) or m.startswith(head) for m in self.MARKERS) and len(head) < self.HOLD:
            return  # henüz karar verilemez, beklemeye devam
        self.decided = True
        if not any(head.startswith(m) for m in self.MARKERS):
            self.shown = True
            self.on_delta(self.buffer)

    def finish(self) -> None:
        if not self.decided and self.buffer.strip():
            head = self.buffer.lstrip()
            if not any(head.startswith(m) for m in self.MARKERS):
                self.shown = True
                self.on_delta(self.buffer)


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
