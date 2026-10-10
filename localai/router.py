"""Bir isteğin yerel modele mi buluta mı gideceğine karar verir.

Kurallar bilerek basit ve açıklanabilir tutuldu; her karar bir gerekçeyle döner
ki kullanıcı neden o tarafın seçildiğini görebilsin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Bu ifadeler genelde çok adımlı veya derin akıl yürütme gerektiren işlere işaret eder
HARD_HINTS = [
    "refactor", "yeniden yapılandır", "mimari", "architecture", "tüm proje", "bütün proje",
    "tüm dosya", "bütün dosya", "projenin tamamı", "hata ayıkla", "debug", "neden çalışmıyor",
    "çalışmıyor", "performans", "optimize", "güvenlik", "security", "migrate",
    "tasarla", "design", "karmaşık", "complex", "baştan yaz", "rewrite",
]

FORCE_CLOUD = ("@bulut", "@cloud")
FORCE_LOCAL = ("@yerel", "@local")

_PATH_RE = re.compile(r"[\w./\\-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|c|cpp|h|cs|php|rb|html|css|json|toml|yaml|yml|md)\b")


@dataclass
class Decision:
    provider: str  # "local" veya "cloud"
    reason: str
    text: str  # @bulut/@yerel etiketi temizlenmiş istek


def strip_tag(text: str, tags: tuple[str, ...]) -> tuple[str, bool]:
    lowered = text.lower()
    for tag in tags:
        idx = lowered.find(tag)
        if idx != -1:
            return (text[:idx] + text[idx + len(tag):]).strip(), True
    return text, False


def decide(
    text: str,
    mode: str,
    cloud_available: bool,
    budget_left: float | None,
) -> Decision:
    """mode: "local", "cloud" veya "auto". budget_left: kalan aylık bütçe (None = sınırsız)."""
    text, force_cloud = strip_tag(text, FORCE_CLOUD)
    text, force_local = strip_tag(text, FORCE_LOCAL)

    if force_local:
        return Decision("local", "@yerel etiketi", text)

    wants_cloud = force_cloud or mode == "cloud"
    if mode == "auto" and not force_cloud:
        score, why = _score(text)
        if score < 2:
            return Decision("local", f"basit görünüyor ({why or 'kısa istek'})", text)
        wants_cloud = True
        reason = f"zor görünüyor ({why})"
    else:
        reason = "@bulut etiketi" if force_cloud else "bulut modu"

    if wants_cloud:
        if not cloud_available:
            return Decision("local", "bulut kapalı (API anahtarı yok), yerel modele düşüldü", text)
        if budget_left is not None and budget_left <= 0:
            return Decision("local", "aylık bulut bütçesi doldu, yerel modele düşüldü", text)
        return Decision("cloud", reason, text)
    return Decision("local", "yerel mod", text)


def _score(text: str) -> tuple[int, str]:
    lowered = text.lower()
    score = 0
    reasons: list[str] = []
    hits = [h for h in HARD_HINTS if h in lowered]
    if hits:
        score += 2
        reasons.append(f"'{hits[0]}'")
    if len(text) > 600:
        score += 1
        reasons.append("uzun istek")
    if len(set(_PATH_RE.findall(text))) >= 3:
        score += 1
        reasons.append("birden çok dosya")
    return score, ", ".join(reasons)
