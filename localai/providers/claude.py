"""Bulut sağlayıcısı: Anthropic Claude API (resmi `anthropic` Python SDK'sı).

- Prompt caching açık: her adımda tekrar gönderilen proje bağlamı ~%90 ucuza gelir.
- Sunucu tarafı fallback açık: güvenlik sınıflandırıcısı bir isteği reddederse
  API isteği aynı çağrı içinde Anthropic'in önerdiği başka bir modelde tekrar
  çalıştırır.
- Geçmiş sadece sona ekleme ile büyür; modelin döndürdüğü içerik (thinking
  blokları dahil) değiştirilmeden geri gönderilir.
"""

from __future__ import annotations

from typing import Any

from ..tools import ToolSpec
from .base import DeltaFn, ProviderError, StepResult, ToolCall, ToolResult, Usage

# Sunucu tarafı fallback'i destekleyen modeller. Haiku 5.5'te fallback yok;
# ona bu parametre gönderilmez.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Akış (stream) sırasında JSON'u hiç ayrıştırılamayan araç girdisi gelirse
# isteği en fazla bu kadar kez yeniden göndeririz.
MAX_JSON_RETRIES = 2


class ClaudeProvider:
    name = "cloud"

    def __init__(
        self,
        model: str,
        system_prompt: str,
        tools: list[ToolSpec],
        effort: str = "high",
        max_tokens: int = 64000,
        client: Any = None,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client
        self.reset()

    @property
    def client(self):
        # SDK'yı sadece bulut gerçekten kullanılınca yükle; yalnızca yerel model
        # kullanan biri için API anahtarı gerekmez.
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise ProviderError("Bulut modu için 'anthropic' paketi gerekli: pip install anthropic") from e
            self._client = anthropic.Anthropic()
        return self._client

    def reset(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # Paralel araç çağrılarının tüm sonuçları tek bir user mesajında gider
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        )

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system_prompt,
            "messages": self.messages,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                    "eager_input_streaming": True,
                }
                for t in self.tools
            ],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            # Otomatik önbellek: son önbelleklenebilir bloğa kadar olan önek
            "cache_control": {"type": "ephemeral"},
        }
        if self.model in FALLBACK_MODELS:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _call(self, on_delta: DeltaFn | None):
        import anthropic

        for attempt in range(MAX_JSON_RETRIES + 1):
            try:
                with self.client.beta.messages.stream(**self._request_kwargs()) as stream:
                    for event in stream:
                        if on_delta and event.type == "text":
                            on_delta(event.text)
                    return stream.get_final_message()
            except anthropic.AuthenticationError as e:
                raise ProviderError(
                    "Claude API anahtarı geçersiz veya eksik. ANTHROPIC_API_KEY ortam değişkenini ayarla."
                ) from e
            except anthropic.RateLimitError as e:
                raise ProviderError("Claude API hız sınırına takıldı; biraz bekleyip tekrar dene.") from e
            except anthropic.APIStatusError as e:
                raise ProviderError(f"Claude API hatası ({e.status_code}): {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise ProviderError("Claude API'ye bağlanılamadı; internet bağlantını kontrol et.") from e
            except ValueError as e:
                # Akış sırasında ayrıştırılamayan araç girdisi: isteği yeniden gönder
                if attempt == MAX_JSON_RETRIES:
                    raise ProviderError(f"Claude geçersiz araç girdisi üretti: {e}") from e
        raise AssertionError("unreachable")

    def step(self, on_delta: DeltaFn | None = None) -> StepResult:
        msg = self._call(on_delta)
        content = _echoable_content(list(msg.content))

        if msg.stop_reason == "refusal":
            # Reddedilen turdaki araç çağrıları yarım kalmış olabilir; hiçbirini
            # çalıştırma ve geçmişe ekleme. Sıra bozulmasın diye kısa bir not ekle.
            self.messages.append({"role": "assistant", "content": [{"type": "text", "text": "[İstek reddedildi.]"}]})
            category = getattr(getattr(msg, "stop_details", None), "category", None)
            return StepResult(
                text=f"Claude bu isteği reddetti (kategori: {category or 'belirtilmedi'}).",
                usage=_usage(msg),
                model=msg.model,
                stop="refusal",
            )

        self.messages.append({"role": "assistant", "content": content})
        text = "".join(b.text for b in content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, arguments=b.input) for b in content if b.type == "tool_use"]
        if msg.stop_reason == "max_tokens":
            stop = "max_tokens"
        elif calls:
            stop = "tool_use"
        else:
            stop = "end"
        return StepResult(text, calls, _usage(msg), msg.model, stop, streamed=on_delta is not None and bool(text))


def _usage(msg) -> Usage:
    u = msg.usage
    return Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )


def _echoable_content(blocks: list) -> list:
    """Yanıt bir fallback sınırı içeriyorsa geri gönderilemeyecek blokları ayıklar.

    Kural: son `fallback` bloğundan önce gelen thinking, redacted_thinking ve
    tool_use blokları geri gönderilmez; metin blokları ve sınırdan sonrası kalır.
    """
    last = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    if last < 0:
        return blocks
    drop = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(blocks) if i >= last or b.type not in drop]
