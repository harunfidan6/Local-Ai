"""Oturum: yönlendirme, iki sağlayıcı arasında bağlam paylaşımı ve maliyet kaydı."""

from __future__ import annotations

from dataclasses import dataclass

from .agent import Events, TaskOutcome, run_task
from .config import Config, home_dir
from .costs import Ledger, estimate_cost
from .providers.base import Provider, StepResult, ToolCall, ToolResult
from .router import Decision, decide
from .tools import ToolBox

MAX_CARRY_CHARS = 1500  # diğer modele taşınan her cevabın en fazla uzunluğu


@dataclass
class Exchange:
    provider: str
    request: str
    answer: str


class Session:
    def __init__(
        self,
        cfg: Config,
        providers: dict[str, Provider],
        toolboxes: dict[str, ToolBox],
        ledger: Ledger,
        cloud_available: bool,
        events: Events | None = None,
    ):
        self.cfg = cfg
        self.providers = providers
        self.toolboxes = toolboxes
        self.ledger = ledger
        self.cloud_available = cloud_available
        self.events = events
        self.mode = cfg.mode
        self.reset()

    def reset(self) -> None:
        self.transcript: list[Exchange] = []
        self.last_toolbox: ToolBox | None = None
        # Her sağlayıcının transkriptin kaçıncı kaydına kadar haberdar olduğu
        self.synced = {name: 0 for name in self.providers}
        for provider in self.providers.values():
            provider.reset()

    def budget_left(self) -> float | None:
        limit = self.cfg.budget_limit
        return None if limit is None else limit - self.ledger.month_usd()

    def route(self, text: str) -> Decision:
        return decide(text, self.mode, self.cloud_available, self.budget_left())

    def ask(self, text: str) -> tuple[Decision, TaskOutcome]:
        decision = self.route(text)
        return decision, self.run_on(decision.provider, decision.text)

    def escalate(self) -> TaskOutcome | None:
        """Son isteği bulut modelde tekrar çalıştırır."""
        if not self.transcript or not self.cloud_available:
            return None
        return self.run_on("cloud", self.transcript[-1].request)

    def undo(self) -> list[str]:
        """Son görevin dosya değişikliklerini geri alır; geri alınan dosyaları döner."""
        return self.last_toolbox.undo() if self.last_toolbox else []

    def run_on(self, name: str, text: str) -> TaskOutcome:
        provider = self.providers[name]
        prompt = self._with_missed_context(name, text)
        self.last_toolbox = self.toolboxes[name]
        self.last_toolbox.begin_task()
        limit = self.cfg.budget_limit if name == "cloud" else None
        recorder = _CostRecorder(name, provider.model, self.ledger, self.events, limit)
        try:
            outcome = run_task(
                provider, self.toolboxes[name], prompt, max_steps=self.cfg.max_steps, events=recorder
            )
        finally:
            self.synced[name] = len(self.transcript)
        outcome.cost = recorder.cost
        answer = outcome.text if outcome.ok else f"[başarısız: {outcome.problem}] {outcome.text}"
        self.transcript.append(Exchange(name, text, answer))
        self.synced[name] = len(self.transcript)
        return outcome

    def _with_missed_context(self, name: str, text: str) -> str:
        """Bu sağlayıcının görmediği, diğer modelle yapılmış konuşmayı isteğin önüne ekler."""
        missed = [e for e in self.transcript[self.synced[name]:] if e.provider != name]
        if not missed:
            return text
        lines = ["[Bu konuşmanın bir kısmı başka bir modelle yapıldı. Özet:]"]
        for e in missed:
            answer = e.answer if len(e.answer) <= MAX_CARRY_CHARS else e.answer[:MAX_CARRY_CHARS] + " [...]"
            lines.append(f"Kullanıcı: {e.request}\nAsistan: {answer}")
        lines.append("[Yeni istek:]")
        lines.append(text)
        return "\n\n".join(lines)


class _CostRecorder(Events):
    """Her model adımının maliyetini anında kaydeder (görev yarıda kesilse bile)
    ve olayları arayüze iletir."""

    def __init__(self, provider: str, model: str, ledger: Ledger, inner: Events | None, budget: float | None):
        self.provider = provider
        self.model = model
        self.ledger = ledger
        self.inner = inner or Events()
        self.budget = budget
        self.cost: float | None = 0.0

    def on_step(self, step: StepResult) -> None:
        # Fallback olduysa cevabı başka model üretmiş olabilir; onun fiyatı geçerli
        cost = estimate_cost(step.model or self.model, step.usage, self.provider)
        self.ledger.record(self.provider, step.usage, cost)
        self.cost = None if cost is None or self.cost is None else self.cost + cost
        self.inner.on_step(step)

    def on_delta(self, text: str) -> None:
        self.inner.on_delta(text)

    def on_text(self, text: str) -> None:
        self.inner.on_text(text)

    def on_tool_call(self, call: ToolCall) -> None:
        self.inner.on_tool_call(call)

    def on_tool_result(self, result: ToolResult) -> None:
        self.inner.on_tool_result(result)

    def should_stop(self) -> str | None:
        if self.budget is not None and self.ledger.month_usd() >= self.budget:
            return f"aylık bulut bütçesi (${self.budget:.2f}) doldu"
        return self.inner.should_stop()


def default_ledger() -> Ledger:
    return Ledger(home_dir() / "usage.json")
