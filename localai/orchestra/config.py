"""Orkestra ayarları: localai.toml / ~/.localai/config.toml içindeki [orkestra] bölümü."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from ..config import home_dir


@dataclass
class OrchestraConfig:
    # --- Claude Code (aboneliğinle giriş yapılmış, değiştirilmemiş `claude` komutu) ---
    claude_enabled: bool = True
    claude_command: list[str] = field(default_factory=lambda: ["claude"])
    claude_model: str = ""  # boş = hesabının varsayılanı
    claude_extra_args: list[str] = field(default_factory=list)

    # --- Google Antigravity CLI (`agy`) ---
    antigravity_enabled: bool = True
    antigravity_command: list[str] = field(default_factory=lambda: ["agy"])
    antigravity_model: str = ""
    # agy'nin başsız modda dosya düzenleyebilmesi için genelde
    # ["--dangerously-skip-permissions"] gerekir. Açarsan antigravity_can_edit = true yap.
    antigravity_extra_args: list[str] = field(default_factory=list)
    antigravity_can_edit: bool = False  # false: sadece planlama ve denetim yapar

    # --- Yerel model (Ollama, ana [localai] ayarlarındaki local_model) ---
    local_enabled: bool = True

    # --- İş akışı ---
    check_command: str = ""  # her görevden sonra çalışacak test, ör. "python -m pytest -q"
    max_attempts: int = 3  # bir görev en fazla kaç kez denensin (farklı işçilerle)
    integration_branch: str = "orkestra"  # onaylanan işlerin birleştirildiği dal
    task_timeout_min: int = 30
    default_cooldown_min: int = 30  # limit doldu ama sıfırlanma zamanı bilinmiyorsa bekleme
    poll_seconds: int = 10

    # --- Kredi dağıtımı: yüksek ağırlık = o işçiye daha çok iş ---
    claude_weight: float = 1.0
    antigravity_weight: float = 1.0
    balance_window_hours: float = 5.0  # son kaç saatlik kullanım dengelensin

    def data_dir(self, repo: Path) -> Path:
        import hashlib

        digest = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:10]
        return home_dir() / "orkestra" / f"{repo.name}-{digest}"


def load_orchestra_config(workspace: Path) -> OrchestraConfig:
    cfg = OrchestraConfig()
    known = {f.name for f in fields(cfg)}
    for path in (home_dir() / "config.toml", workspace / "localai.toml"):
        if not path.is_file():
            continue
        with path.open("rb") as f:
            section = tomllib.load(f).get("orkestra", {})
        for key, value in section.items():
            if key not in known:
                raise ValueError(f"{path}: [orkestra] bölümünde bilinmeyen ayar '{key}'")
            default = getattr(cfg, key)
            if isinstance(default, list) and isinstance(value, str):
                value = [value]
            elif isinstance(default, float) and isinstance(value, int):
                value = float(value)
            if type(value) is not type(default):
                raise ValueError(f"{path}: '{key}' için {type(default).__name__} bekleniyordu")
            setattr(cfg, key, value)
    return cfg
