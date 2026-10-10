"""Ajan döngüsü: model -> araç çağrısı -> sonuç -> model ... cevap bitene kadar."""

from __future__ import annotations

from dataclasses import dataclass, field

from .providers.base import Provider, StepResult, ToolCall, ToolResult, Usage
from .tools import ToolBox


class Events:
    """Arayüzün ajanı izlemesi için kancalar. Varsayılanlar hiçbir şey yapmaz."""

    def on_text(self, text: str) -> None: ...

    def on_tool_call(self, call: ToolCall) -> None: ...

    def on_tool_result(self, result: ToolResult) -> None: ...

    def on_step(self, step: StepResult) -> None: ...

    def should_stop(self) -> str | None:
        """Görevin durdurulması gerekiyorsa nedenini döner (ör. bütçe doldu)."""
        return None


@dataclass
class TaskOutcome:
    text: str
    ok: bool
    steps: int
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    # ok=False ise neden (ör. "adım sınırı", "art arda araç hatası")
    problem: str = ""
    cost: float | None = None  # dolar; yerel model için 0, fiyatı bilinmiyorsa None


def run_task(
    provider: Provider,
    toolbox: ToolBox,
    user_text: str,
    max_steps: int = 25,
    max_consecutive_errors: int = 3,
    events: Events | None = None,
) -> TaskOutcome:
    events = events or Events()
    provider.add_user(user_text)
    usage = Usage()
    consecutive_errors = 0
    last_text = ""
    model = provider.model

    for step_no in range(1, max_steps + 1):
        step = provider.step()
        usage.add(step.usage)
        model = step.model or model
        events.on_step(step)
        if step.text:
            last_text = step.text
            events.on_text(step.text)

        if step.stop == "refusal":
            return TaskOutcome(step.text, False, step_no, usage, model, "reddedildi")

        if not step.tool_calls:
            if step.stop == "max_tokens":
                return TaskOutcome(last_text, False, step_no, usage, model, "cevap uzunluk sınırında kesildi")
            return TaskOutcome(last_text, True, step_no, usage, model)

        if step.stop == "max_tokens":
            # Araç girdisi yarıda kesilmiş olabilir; hiçbirini çalıştırma
            results = [
                ToolResult(
                    c.id, c.name,
                    "Çıktın uzunluk sınırında kesildi, bu araç çağrısı çalıştırılmadı. "
                    "İçeriği daha küçük parçalara bölerek tekrar dene.",
                    is_error=True,
                )
                for c in step.tool_calls
            ]
        else:
            results = []
            try:
                for call in step.tool_calls:
                    events.on_tool_call(call)
                    result = toolbox.execute(call)
                    events.on_tool_result(result)
                    results.append(result)
            except KeyboardInterrupt:
                # Her araç çağrısının bir sonucu olmalı, yoksa sonraki istek geçersiz olur
                done = {r.call_id for r in results}
                results += [
                    ToolResult(c.id, c.name, "Kullanıcı işlemi iptal etti.", is_error=True)
                    for c in step.tool_calls
                    if c.id not in done
                ]
                provider.add_tool_results(results)
                raise

        if all(r.is_error for r in results):
            consecutive_errors += 1
        else:
            consecutive_errors = 0
        provider.add_tool_results(results)

        if consecutive_errors >= max_consecutive_errors:
            return TaskOutcome(last_text, False, step_no, usage, model, "art arda araç hataları")
        if reason := events.should_stop():
            return TaskOutcome(last_text, False, step_no, usage, model, reason)

    return TaskOutcome(last_text, False, max_steps, usage, model, f"{max_steps} adım sınırına ulaşıldı")
