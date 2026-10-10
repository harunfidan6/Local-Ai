from pathlib import Path

from localai.agent import run_task
from localai.config import Config
from localai.costs import Ledger
from localai.providers.base import StepResult, ToolCall, ToolResult, Usage
from localai.session import Session
from localai.tools import ToolBox


class FakeProvider:
    """Önceden yazılmış cevapları sırayla döndüren sahte model."""

    def __init__(self, name: str, steps: list[StepResult], model: str = "fake"):
        self.name = name
        self.model = model
        self.steps = list(steps)
        self.reset()

    def reset(self):
        self.users: list[str] = []
        self.tool_results: list[list[ToolResult]] = []

    def add_user(self, text):
        self.users.append(text)

    def add_tool_results(self, results):
        self.tool_results.append(results)

    def step(self, on_delta=None):
        return self.steps.pop(0)


def toolbox(tmp_path: Path) -> ToolBox:
    return ToolBox(tmp_path, confirm=lambda t, s: True)


def test_loop_runs_tools_until_answer(tmp_path):
    (tmp_path / "a.txt").write_text("içerik", encoding="utf-8")
    provider = FakeProvider("local", [
        StepResult("", [ToolCall("1", "read_file", {"path": "a.txt"})], Usage(10, 5), stop="tool_use"),
        StepResult("Dosyada 'içerik' yazıyor.", usage=Usage(20, 8)),
    ])
    outcome = run_task(provider, toolbox(tmp_path), "a.txt'de ne var?")
    assert outcome.ok
    assert outcome.steps == 2
    assert outcome.usage.input_tokens == 30
    assert "içerik" in provider.tool_results[0][0].content


def test_truncated_tool_calls_are_not_executed(tmp_path):
    provider = FakeProvider("cloud", [
        StepResult("", [ToolCall("1", "write_file", {"path": "x.txt", "content": "yarım"})], stop="max_tokens"),
        StepResult("Tamam, parçalara böleceğim."),
    ])
    outcome = run_task(provider, toolbox(tmp_path), "yaz")
    assert outcome.ok
    assert not (tmp_path / "x.txt").exists()
    assert provider.tool_results[0][0].is_error


def test_stops_after_repeated_tool_errors(tmp_path):
    bad = StepResult("", [ToolCall("1", "read_file", {"path": "yok.txt"})], stop="tool_use")
    provider = FakeProvider("local", [bad, bad, bad, bad])
    outcome = run_task(provider, toolbox(tmp_path), "oku", max_consecutive_errors=3)
    assert not outcome.ok
    assert "hata" in outcome.problem


def test_step_limit(tmp_path):
    step = StepResult("", [ToolCall("1", "list_files", {})], stop="tool_use")
    provider = FakeProvider("local", [step] * 5)
    outcome = run_task(provider, toolbox(tmp_path), "listele", max_steps=3)
    assert not outcome.ok
    assert outcome.steps == 3


def make_session(tmp_path, local_steps, cloud_steps, budget=5.0):
    cfg = Config(monthly_budget_usd=budget)
    providers = {
        "local": FakeProvider("local", local_steps, model="qwen"),
        "cloud": FakeProvider("cloud", cloud_steps, model="claude-sonnet-5-5"),
    }
    boxes = {"local": toolbox(tmp_path), "cloud": toolbox(tmp_path)}
    ledger = Ledger(tmp_path / "usage.json")
    return Session(cfg, providers, boxes, ledger, cloud_available=True), providers


def test_cloud_sees_what_happened_on_local(tmp_path):
    session, providers = make_session(
        tmp_path,
        local_steps=[StepResult("Merhaba!")],
        cloud_steps=[StepResult("Bulut cevabı", usage=Usage(1_000_000, 0))],
    )
    session.ask("selam")
    session.ask("@bulut projeyi incele")
    sent = providers["cloud"].users[0]
    assert "Kullanıcı: selam" in sent and "Asistan: Merhaba!" in sent
    assert sent.endswith("projeyi incele")
    # Sonnet 5.5: 1M girdi token = $2
    assert abs(session.ledger.month_usd() - 2.0) < 1e-9


def test_budget_exhausted_falls_back_to_local(tmp_path):
    session, providers = make_session(
        tmp_path, local_steps=[StepResult("yerel")], cloud_steps=[], budget=0.5
    )
    session.ledger.record("cloud", Usage(), 1.0)
    decision, outcome = session.ask("@bulut zor iş")
    assert decision.provider == "local"
    assert outcome.text == "yerel"


def test_budget_stops_cloud_task_midway(tmp_path):
    step = StepResult("", [ToolCall("1", "list_files", {})], Usage(1_000_000, 0), stop="tool_use")
    session, providers = make_session(tmp_path, local_steps=[], cloud_steps=[step] * 5, budget=3.0)
    _, outcome = session.ask("@bulut her şeyi listele")
    assert not outcome.ok
    assert "bütçe" in outcome.problem
    assert outcome.steps == 2  # 2 x $2 > $3
