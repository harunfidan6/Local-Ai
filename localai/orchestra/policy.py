"""Kredi dağıtımı: hangi işi hangi işçi yapsın?

İlkeler:
- Basit işler ücretsiz yerel modele gider; aboneliklerin kotası korunur.
- Zor işler önce Claude Code'a gider (en güçlü uygulayıcı).
- Normal işler, son birkaç saatte en az kullanılmış (ağırlığına göre) aboneliğe gider.
  Böylece iki aboneliğin kotası birlikte ve dengeli tükenir, biri boşta beklemez.
- Denetçi her zaman uygulayıcıdan FARKLI bir firmadan seçilir (çapraz denetim).
- Limiti dolan işçi sıfırlanma zamanına kadar kenara alınır; iş başkasına geçer
  ya da kota açılana kadar bekler. Limitleri aşmaya çalışılmaz.
"""

from __future__ import annotations

import time

from .config import OrchestraConfig
from .store import Store, Task
from .workers import Worker

LOCAL = "yerel"
PREMIUM_ORDER = ("claude", "antigravity")


class Policy:
    def __init__(self, cfg: OrchestraConfig, store: Store, workers: dict[str, Worker]):
        self.cfg = cfg
        self.store = store
        self.workers = workers

    def weight(self, name: str) -> float:
        return {"claude": self.cfg.claude_weight, "antigravity": self.cfg.antigravity_weight}.get(name, 1.0)

    def is_free(self, name: str, busy: set[str]) -> bool:
        if name not in self.workers or name in busy:
            return False
        until, _ = self.store.cooling_until(name)
        return until <= time.time()

    def load(self, name: str) -> float:
        """Son pencere içindeki çalışma dakikası / ağırlık. Düşük = daha çok kotası kalmış olabilir."""
        since = time.time() - self.cfg.balance_window_hours * 3600
        minutes, _, runs = self.store.usage_since(name, since)
        return (minutes + runs * 0.5) / max(self.weight(name), 0.01)

    def _premium(self) -> list[str]:
        return [n for n in PREMIUM_ORDER if n in self.workers]

    def pick_implementer(self, task: Task, busy: set[str]) -> str | None:
        editors = [n for n in self.workers if self.workers[n].can_edit]
        # Üçüncü denemeden itibaren son uygulayıcıyı değiştir: aynı hatayı tekrar etmesin
        excluded = {task.worker} if task.attempts >= 2 and task.worker else set()
        candidates = [n for n in editors if n not in excluded] or editors

        if task.difficulty == "basit" and LOCAL in candidates:
            if self.is_free(LOCAL, busy):
                return LOCAL
            if LOCAL in busy:
                return None  # yerel model meşgul: bekle, basit iş için abonelik harcama
            # Yerel model kullanılamıyor (Ollama kapalı vb.): aboneliğe düş

        premium = [n for n in self._premium() if n in candidates]
        if not premium:
            # Hiç abonelik işçisi yoksa (hepsi kapalı) yerel model her işi yapar
            return LOCAL if LOCAL in candidates and self.is_free(LOCAL, busy) else None

        free = [n for n in premium if self.is_free(n, busy)]
        if not free:
            return None  # kota açılana ya da işçi boşalana kadar bekle
        if task.difficulty == "zor" and "claude" in free:
            return "claude"
        return min(free, key=self.load)

    def pick_reviewer(self, task: Task, busy: set[str]) -> str | None:
        """Uygulayıcıdan farklı bir işçi seçer; mümkünse farklı firmadan.

        task.reviewer, son denetimi beceremeyen (geçerli karar vermeyen) işçiyi tutar;
        önce başka biri denenir.
        """
        impl = task.worker
        impl_vendor = self.workers[impl].vendor if impl in self.workers else None
        others = [n for n in self._premium() if n != impl]
        # Önce farklı firma, sonra en az yüklü
        order = sorted(others, key=lambda n: (self.workers[n].vendor == impl_vendor, self.load(n)))

        for name in order:
            if name != task.reviewer and self.is_free(name, busy):
                return name
        only_failed_left = all(n == task.reviewer for n in others)
        if (task.difficulty == "basit" or only_failed_left) and impl != LOCAL and self.is_free(LOCAL, busy):
            return LOCAL
        if not self._premium() and self.is_free(LOCAL, busy):
            return LOCAL  # hiç abonelik işçisi yok: testler + yerel model
        for name in order:  # son çare: daha önce beceremeyen denetçiyi tekrar dene
            if self.is_free(name, busy):
                return name
        return None

    def pick_planner(self, busy: set[str]) -> str | None:
        for name in self._premium():
            if self.is_free(name, busy):
                return name
        if not self._premium() and self.is_free(LOCAL, busy):
            return LOCAL
        return None
