from localai.costs import PRICES, estimate_cost
from localai.providers.base import Usage
from localai.providers.ollama import parse_text_tool_calls
from localai.router import decide


def ids():
    counter = iter(range(100))
    return lambda: f"c{next(counter)}"


def test_simple_request_stays_local():
    d = decide("bu fonksiyon ne yapıyor?", "auto", cloud_available=True, budget_left=5)
    assert d.provider == "local"


def test_hard_request_goes_to_cloud():
    d = decide("tüm projeyi refactor et", "auto", cloud_available=True, budget_left=5)
    assert d.provider == "cloud"


def test_tags_override_and_are_stripped():
    d = decide("@bulut selam", "local", cloud_available=True, budget_left=5)
    assert d.provider == "cloud" and d.text == "selam"
    d = decide("tüm projeyi refactor et @yerel", "cloud", cloud_available=True, budget_left=5)
    assert d.provider == "local" and "@yerel" not in d.text


def test_cloud_unavailable_or_no_budget():
    assert decide("@bulut x", "auto", cloud_available=False, budget_left=5).provider == "local"
    assert decide("@bulut x", "auto", cloud_available=True, budget_left=0).provider == "local"
    assert decide("@bulut x", "auto", cloud_available=True, budget_left=None).provider == "cloud"


def test_costs():
    assert estimate_cost("qwen2.5-coder:7b", Usage(10**6, 10**6), "local") == 0.0
    cost = estimate_cost("claude-opus-5-5", Usage(10**6, 10**6, cache_read_tokens=10**6), "cloud")
    assert abs(cost - (4 + 20 + 0.20)) < 1e-9
    assert estimate_cost("bilinmeyen-model", Usage(1, 1), "cloud") is None
    assert "claude-haiku-5-5" in PRICES


def test_parses_qwen_style_tool_call():
    text = '<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call>'
    calls = parse_text_tool_calls(text, {"read_file"}, ids())
    assert len(calls) == 1 and calls[0].arguments == {"path": "a.py"}


def test_parses_fenced_json_and_string_arguments():
    text = 'Şunu çalıştırıyorum:\n```json\n{"name": "run_command", "arguments": "{\\"command\\": \\"ls\\"}"}\n```'
    calls = parse_text_tool_calls(text, {"run_command"}, ids())
    assert calls[0].arguments == {"command": "ls"}


def test_ignores_unknown_tools_and_plain_code():
    assert parse_text_tool_calls('{"name": "hack", "arguments": {}}', {"read_file"}, ids()) == []
    assert parse_text_tool_calls("```python\nprint(1)\n```", {"read_file"}, ids()) == []
