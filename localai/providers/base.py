"""Sağlayıcılar (Ollama, Claude) için ortak tipler.

Her sağlayıcı kendi konuşma geçmişini kendi API formatında tutar. Ajan döngüsü
sadece bu dosyadaki nötr tiplerle konuşur; böylece yeni bir sağlayıcı eklemek
için tek yapılması gereken ``Provider`` arayüzünü uygulamaktır.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class StepResult:
    """Modelin tek bir cevabı: metin, istenen araç çağrıları ve token kullanımı."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    # "end", "tool_use", "max_tokens", "refusal" veya "error"
    stop: str = "end"


class ProviderError(RuntimeError):
    """Sağlayıcıya ulaşılamadığında veya API hata döndürdüğünde."""


class Provider(Protocol):
    name: str  # "local" veya "cloud"
    model: str

    def add_user(self, text: str) -> None: ...

    def add_tool_results(self, results: list[ToolResult]) -> None: ...

    def step(self) -> StepResult: ...

    def reset(self) -> None: ...
