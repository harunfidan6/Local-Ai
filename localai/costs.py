"""Token kullanımından dolar maliyeti hesaplar ve aylık harcamayı diskte tutar."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .providers.base import Usage


@dataclass(frozen=True)
class Price:
    """1 milyon token başına dolar."""

    input: float
    output: float
    cache_read: float | None = None  # verilmezse girdi fiyatının 0.1 katı

    def cost(self, usage: Usage) -> float:
        cache_read = self.cache_read if self.cache_read is not None else self.input * 0.1
        cache_write = self.input * 1.25  # 5 dakikalık önbelleğe yazma
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * cache_read
            + usage.cache_write_tokens * cache_write
        ) / 1_000_000


# Anthropic API fiyatları (Ekim 2026). Güncel liste: https://claude.com/pricing
PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.00, 50.00, cache_read=0.25),
    "claude-opus-5-5": Price(4.00, 20.00, cache_read=0.20),
    "claude-opus-5": Price(5.00, 25.00),
    "claude-opus-4-8": Price(5.00, 25.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, cache_read=0.20),
    "claude-sonnet-5": Price(2.00, 10.00),
    "claude-haiku-5-5": Price(0.10, 0.50),
}


def estimate_cost(model: str, usage: Usage, provider: str) -> float | None:
    """Yerel model için 0 döner; fiyatı bilinmeyen bulut modeli için None."""
    if provider == "local":
        return 0.0
    price = PRICES.get(model)
    return price.cost(usage) if price else None


class Ledger:
    """Aylık bulut harcamasını ~/.localai/usage.json içinde tutar."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict[str, float]] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    @staticmethod
    def _month() -> str:
        return date.today().strftime("%Y-%m")

    def record(self, provider: str, usage: Usage, cost: float | None) -> None:
        month = self.data.setdefault(self._month(), {})
        prefix = provider + "_"
        for key, value in (
            ("input_tokens", usage.input_tokens),
            ("output_tokens", usage.output_tokens),
            ("cache_read_tokens", usage.cache_read_tokens),
            ("cache_write_tokens", usage.cache_write_tokens),
        ):
            month[prefix + key] = month.get(prefix + key, 0) + value
        if cost:
            month["usd"] = month.get("usd", 0.0) + cost
        self._save()

    def month_usd(self) -> float:
        return self.data.get(self._month(), {}).get("usd", 0.0)

    def month_summary(self) -> dict[str, float]:
        return dict(self.data.get(self._month(), {}))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass  # kayıt tutulamaması ajanı durdurmamalı
