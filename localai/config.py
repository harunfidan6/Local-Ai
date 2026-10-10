"""Ayarlar: varsayılanlar < ~/.localai/config.toml < ./localai.toml < ortam değişkenleri."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


def home_dir() -> Path:
    return Path(os.environ.get("LOCALAI_HOME", Path.home() / ".localai"))


@dataclass
class Config:
    # Yerel model (Ollama)
    local_model: str = "qwen2.5-coder:7b"
    ollama_host: str = "http://127.0.0.1:11434"
    num_ctx: int = 16384  # 8 GB VRAM için makul; artırırsan VRAM dolar ve yavaşlar
    local_tool_output_chars: int = 6000  # küçük bağlam penceresini araç çıktılarıyla doldurma

    # Bulut model (Claude API)
    cloud_model: str = "claude-opus-5-5"
    effort: str = "high"  # low | medium | high | xhigh | max
    cloud_tool_output_chars: int = 30000
    monthly_budget_usd: float = 5.0  # 0 = bulutu tamamen kapat, -1 = sınırsız

    # Genel
    mode: str = "auto"  # auto | local | cloud
    max_steps: int = 25
    auto_approve: bool = False  # True: dosya yazma ve komutlar için onay sorma

    @property
    def budget_limit(self) -> float | None:
        return None if self.monthly_budget_usd < 0 else self.monthly_budget_usd


ENV_PREFIX = "LOCALAI_"


def _coerce(value: str, target_type: type):
    if target_type is bool:
        return value.strip().lower() in {"1", "true", "yes", "evet", "on"}
    return target_type(value)


def load_config(workspace: Path) -> Config:
    cfg = Config()
    types = {f.name: type(getattr(cfg, f.name)) for f in fields(cfg)}

    for path in (home_dir() / "config.toml", workspace / "localai.toml"):
        if path.is_file():
            with path.open("rb") as f:
                data = tomllib.load(f)
            for key, value in data.items():
                if key in types:
                    setattr(cfg, key, _coerce(str(value), types[key]))

    for key, target_type in types.items():
        env_value = os.environ.get(ENV_PREFIX + key.upper())
        if env_value is not None:
            setattr(cfg, key, _coerce(env_value, target_type))

    if cfg.mode not in {"auto", "local", "cloud"}:
        raise ValueError(f"Geçersiz mode: {cfg.mode} (auto, local veya cloud olmalı)")
    return cfg


def cloud_credentials_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
