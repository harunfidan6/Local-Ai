"""Testler için sahte `claude` / `agy` komutu.

Kullanım: python fake_agent.py <claude|agy> [gerçek CLI argümanları...]
FAKE_SCENARIO ortam değişkeni bir JSON dosyasını gösterir:
    {"claude": {"plan": [cevap, ...], "impl": [...], "review": [...]}, "agy": {...}}
Her rol için cevaplar sırayla tüketilir. Bir cevap:
    {"text": "...", "write": {"dosya": "içerik"}, "structured": {...}, "limit": "mesaj", "error": "mesaj"}
Her çağrı FAKE_SCENARIO + ".calls" dosyasına bir JSON satırı olarak yazılır.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    flavor, args = sys.argv[1], sys.argv[2:]
    scenario_path = Path(os.environ["FAKE_SCENARIO"])
    if flavor == "claude":
        prompt = sys.stdin.read()
    else:
        prompt = args[args.index("-p") + 1]

    if "planlayıcısın" in prompt:
        role = "plan"
    elif "denetçisin" in prompt:
        role = "review"
    else:
        role = "impl"

    counter_path = Path(str(scenario_path) + ".state")
    state = json.loads(counter_path.read_text()) if counter_path.exists() else {}
    key = f"{flavor}:{role}"
    index = state.get(key, 0)
    state[key] = index + 1
    counter_path.write_text(json.dumps(state))

    with open(str(scenario_path) + ".calls", "a", encoding="utf-8") as f:
        f.write(json.dumps({"flavor": flavor, "role": role, "args": args, "prompt": prompt, "cwd": os.getcwd()},
                           ensure_ascii=False) + "\n")

    responses = json.loads(scenario_path.read_text(encoding="utf-8"))[flavor][role]
    response = responses[min(index, len(responses) - 1)]

    for name, content in (response.get("write") or {}).items():
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        Path(name).write_text(content, encoding="utf-8")

    failure = response.get("limit") or response.get("error")
    text = failure or response.get("text", "Tamam.")
    structured = response.get("structured")
    if flavor == "claude":
        out = {"type": "result", "subtype": "success", "is_error": bool(failure), "result": text,
               "total_cost_usd": 0.05, "session_id": "s1"}
        if structured is not None:
            out["structured_output"] = structured
    else:
        if structured is not None:
            text += "\n```json\n" + json.dumps(structured, ensure_ascii=False) + "\n```"
        out = {"conversation_id": "c1", "status": "ERROR" if failure else "SUCCESS", "response": text,
               "num_turns": 1, "usage": {}}
    print(json.dumps(out, ensure_ascii=False))
    return 1 if failure else 0


if __name__ == "__main__":
    sys.exit(main())
