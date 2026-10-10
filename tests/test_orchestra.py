"""Orkestra şefini gerçek git ve sahte claude/agy komutlarıyla uçtan uca test eder."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from localai.orchestra import store as S
from localai.orchestra.conductor import Conductor
from localai.orchestra.config import OrchestraConfig
from localai.orchestra.store import Store
from localai.orchestra.workers import (
    AntigravityWorker,
    ClaudeCodeWorker,
    extract_json,
    looks_limited,
    parse_reset,
)
from localai.orchestra.workspace import Workspace, git

FAKE = str(Path(__file__).parent / "fake_agent.py")

APPROVE = {"structured": {"approved": True, "summary": "İyi görünüyor", "issues": []}}


def plan(*subtasks):
    return {"structured": {"subtasks": [
        {"title": t, "prompt": f"{t} yap", "difficulty": "normal", "depends_on": deps} for t, deps in subtasks
    ]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAI_HOME", str(tmp_path / "home"))
    repo = tmp_path / "proje"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# Proje\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ilk")
    scenario = tmp_path / "senaryo.json"
    monkeypatch.setenv("FAKE_SCENARIO", str(scenario))

    def make(scenario_data, **overrides):
        scenario.write_text(json.dumps(scenario_data, ensure_ascii=False), encoding="utf-8")
        cfg = OrchestraConfig(
            claude_command=[sys.executable, FAKE, "claude"],
            antigravity_command=[sys.executable, FAKE, "agy"],
            local_enabled=False,
            **overrides,
        )
        ws = Workspace(repo, cfg.data_dir(repo), cfg.integration_branch)
        store = Store(cfg.data_dir(repo) / "orkestra.db")
        workers = {"claude": ClaudeCodeWorker(cfg), "antigravity": AntigravityWorker(cfg)}
        logs = []
        conductor = Conductor(cfg, store, ws, workers, say=logs.append, threaded=False)
        return conductor, store, ws, logs

    def calls():
        path = Path(str(scenario) + ".calls")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    return make, calls, repo


def run_until_idle(conductor, max_ticks=30):
    for _ in range(max_ticks):
        if conductor.tick() == 0:
            return


def files_on(repo: Path, branch: str) -> set[str]:
    return set(git(repo, "ls-tree", "-r", "--name-only", branch).splitlines())


def test_goal_is_planned_implemented_cross_reviewed_and_merged(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({
        "claude": {
            "plan": [plan(("API ekle", []), ("Testleri yaz", [1]))],
            "impl": [{"write": {"api.py": "def f(): pass\n"}, "text": "API eklendi"},
                     {"write": {"test_api.py": "def test(): pass\n"}, "text": "Test yazıldı"}],
        },
        "agy": {"review": [APPROVE]},
    })
    goal = store.add("Özellik", "Bir API ve testlerini ekle", kind="hedef")

    run_until_idle(conductor)

    tasks = store.all()
    assert store.get(goal).status == S.DONE, logs
    assert [t.status for t in tasks if t.parent_id == goal] == [S.DONE, S.DONE]
    assert {"api.py", "test_api.py"} <= files_on(repo, "orkestra")
    assert "api.py" not in files_on(repo, "main")  # ana dala dokunulmaz

    roles = [(c["flavor"], c["role"]) for c in calls()]
    assert roles == [("claude", "plan"), ("claude", "impl"), ("agy", "review"), ("claude", "impl"), ("agy", "review")]
    # İkinci görev, birinci birleştirildikten sonra başladı ve onun dosyasını gördü
    second_impl_cwd = Path(calls()[3]["cwd"])
    assert not second_impl_cwd.exists()  # iş bitince worktree silindi
    # Planlayıcı ve denetçi salt-okunur, uygulayıcı düzenleme izniyle çağrıldı
    assert "dontAsk" in calls()[0]["args"]
    assert "acceptEdits" in calls()[1]["args"]


def test_rejected_work_goes_back_with_feedback(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({
        "claude": {"impl": [{"write": {"a.py": "x = 1\n"}}, {"write": {"a.py": "x = 2\n"}}]},
        "agy": {"review": [
            {"structured": {"approved": False, "summary": "Yanlış değer", "issues": ["x 2 olmalı"]}},
            APPROVE,
        ]},
    })
    task = store.add("x'i ayarla", "a.py'de x = 2 olsun")

    run_until_idle(conductor)

    assert store.get(task).status == S.DONE
    impl_prompts = [c["prompt"] for c in calls() if c["role"] == "impl"]
    assert len(impl_prompts) == 2
    assert "x 2 olmalı" in impl_prompts[1]
    content = git(repo, "show", "orkestra:a.py")
    assert content.strip() == "x = 2"


def test_failing_check_skips_reviewer(env):
    make, calls, repo = env
    check = f'"{sys.executable}" -c "import pathlib,sys; sys.exit(0 if pathlib.Path(\'ok.txt\').exists() else 1)"'
    conductor, store, ws, logs = make({
        "claude": {"impl": [{"write": {"yanlis.txt": "x"}}, {"write": {"ok.txt": "x"}}]},
        "agy": {"review": [APPROVE]},
    }, check_command=check)
    task = store.add("ok.txt oluştur", "ok.txt dosyasını oluştur")

    run_until_idle(conductor)

    assert store.get(task).status == S.DONE
    roles = [(c["flavor"], c["role"]) for c in calls()]
    assert roles == [("claude", "impl"), ("claude", "impl"), ("agy", "review")]  # ilk denemede denetçi çağrılmadı
    assert "başarısız" in [c for c in calls() if c["role"] == "impl"][1]["prompt"]


def test_limit_puts_worker_to_rest_and_work_moves_on(env):
    make, calls, repo = env
    reset = int(time.time()) + 3 * 3600
    conductor, store, ws, logs = make({
        "claude": {"impl": [{"limit": f"Claude AI usage limit reached|{reset}"}], "review": [APPROVE]},
        "agy": {"impl": [{"write": {"b.py": "y = 1\n"}}]},
    }, antigravity_can_edit=True, claude_weight=5.0)
    task = store.add("b.py ekle", "b.py oluştur", difficulty="zor")  # zor iş önce Claude'a gider

    run_until_idle(conductor)

    until, note = store.cooling_until("claude")
    assert until == reset and "limit" in note
    assert store.get(task).status == S.REVIEW  # Antigravity yaptı; denetçi (Claude) dinleniyor
    assert store.get(task).worker == "antigravity"
    assert store.get(task).attempts == 0  # limit, deneme hakkından yemez

    store.set_cooling("claude", 0, "")  # zaman geçti, kota açıldı
    run_until_idle(conductor)
    assert store.get(task).status == S.DONE
    assert "b.py" in files_on(repo, "orkestra")


def test_reviewer_cannot_change_files(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({
        "claude": {"impl": [{"write": {"c.py": "z = 1\n"}}]},
        "agy": {"review": [{**APPROVE, "write": {"kotu.txt": "denetçi yazdı"}}]},
    })
    task = store.add("c.py ekle", "c.py oluştur")

    run_until_idle(conductor)

    assert store.get(task).status == S.DONE
    assert "c.py" in files_on(repo, "orkestra")
    assert "kotu.txt" not in files_on(repo, "orkestra")
    assert any("geri alındı" in line for line in logs)


def test_gives_up_after_max_attempts(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({"claude": {"impl": [{"error": "Bir şeyler ters gitti"}]}, "agy": {}},
                                      max_attempts=2)
    task = store.add("imkansız", "yapılamaz bir şey")
    run_until_idle(conductor)
    assert store.get(task).status == S.NEEDS_HUMAN
    assert store.get(task).attempts == 2


def test_missing_cli_marks_worker_unavailable(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({"claude": {}, "agy": {}})
    conductor.workers["claude"].command = ["olmayan-komut-xyz"]
    task = store.add("bir şey", "yap")
    run_until_idle(conductor)
    until, note = store.cooling_until("claude")
    assert until > time.time() and "bulunamadı" in note
    assert store.get(task).status == S.QUEUED  # Antigravity düzenleyemiyor; Claude dönünce devam eder


def test_restart_recovers_interrupted_work(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({"claude": {}, "agy": {}})
    task = store.add("yarım", "yarım kalan iş")
    store.update(task, status=S.IMPL_RUN)
    Conductor(conductor.cfg, store, ws, conductor.workers, say=logs.append, threaded=False)
    assert store.get(task).status == S.QUEUED


# -- yardımcı fonksiyonlar ------------------------------------------------------

def test_parse_reset_formats():
    now = 1_700_000_000.0
    assert parse_reset("Claude AI usage limit reached|1700003600", now) == 1700003600
    assert parse_reset("Quota exceeded. Resets in 2h 30m", now) == now + 2 * 3600 + 30 * 60
    assert parse_reset("try again in 15 minutes", now) == now + 15 * 60
    at = parse_reset("You've hit your limit · resets 3pm", now)
    assert at is not None and 0 < at - now <= 24 * 3600
    assert parse_reset("bilinmeyen hata", now) is None


def test_limit_detection_and_json_extraction():
    assert looks_limited("Individual quota reached")
    assert looks_limited("Error 429: Too Many Requests")
    assert not looks_limited("Dosya bulunamadı")
    text = 'Bitti.\n```json\n{"approved": true, "summary": "ok", "issues": []}\n```'
    assert extract_json(text)["approved"] is True
    assert extract_json('Sonuç: {"a": {"b": 1}} tamam')["a"] == {"b": 1}
    assert extract_json("JSON yok") is None


def test_not_a_git_repo(tmp_path):
    from localai.orchestra.workspace import GitError

    with pytest.raises(GitError, match="git deposu değil"):
        Workspace(tmp_path, tmp_path / "data", "orkestra")


def test_cli_add_and_status(env, capsys):
    from localai.orchestra.cli import main

    make, calls, repo = env
    make({"claude": {}, "agy": {}})
    assert main(["-w", str(repo), "ekle", "--direkt", "--zorluk", "basit", "README", "düzelt"]) == 0
    assert main(["-w", str(repo), "durum"]) == 0
    out = capsys.readouterr().out
    assert "README düzelt" in out and "basit" in out
    assert "claude" in out and "antigravity" in out


def test_subprocess_smoke_fake_cli_runs():
    # Sahte CLI'nin kendisi çalışıyor mu (Windows/Linux farkı olmadan)
    proc = subprocess.run([sys.executable, FAKE, "--help"], capture_output=True, text=True)
    assert proc.returncode != 0  # senaryo yok: hata vermeli ama çökmeden çalıştırılabilmeli


def test_two_workers_run_in_parallel(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({
        "claude": {"impl": [{"write": {"claude.txt": "c"}}], "review": [APPROVE]},
        "agy": {"impl": [{"write": {"agy.txt": "a"}}], "review": [APPROVE]},
    }, antigravity_can_edit=True)
    conductor.threaded = True
    first = store.add("bir", "claude.txt veya agy.txt yaz")
    second = store.add("iki", "claude.txt veya agy.txt yaz")

    assert conductor.tick() == 2  # iki işçi aynı anda iki göreve başladı
    for _ in range(10):
        conductor.wait_idle()
        if conductor.tick() == 0 and not conductor.threads:
            break

    assert store.get(first).status == S.DONE and store.get(second).status == S.DONE
    assert {store.get(first).worker, store.get(second).worker} == {"claude", "antigravity"}
    # Her iş diğer firmanın işçisince denetlendi
    assert store.get(first).reviewer != store.get(first).worker
    assert {"claude.txt", "agy.txt"} <= files_on(repo, "orkestra")


def test_crashing_job_does_not_leave_task_stuck(env, monkeypatch):
    make, calls, repo = env
    conductor, store, ws, logs = make({"claude": {}, "agy": {}})
    monkeypatch.setattr(conductor.ws, "create", lambda task_id: 1 / 0)
    task = store.add("çöken", "x")
    conductor.tick()
    assert store.get(task).status == S.QUEUED
    assert store.get(task).attempts == 1
    assert any("ZeroDivisionError" in line for line in logs)


def test_simple_task_falls_back_when_local_model_is_down(env):
    from localai.orchestra.policy import Policy
    from localai.orchestra.workers import Worker

    make, calls, repo = env
    conductor, store, ws, logs = make({"claude": {}, "agy": {}})
    workers = {**conductor.workers, "yerel": Worker()}
    workers["yerel"].vendor = "yerel"
    policy = Policy(conductor.cfg, store, workers)
    task = store.get(store.add("basit iş", "x", difficulty="basit"))

    assert policy.pick_implementer(task, set()) == "yerel"
    assert policy.pick_implementer(task, {"yerel"}) is None  # meşgul: bekle
    store.set_cooling("yerel", time.time() + 600, "Ollama kapalı")
    assert policy.pick_implementer(task, set()) == "claude"  # kullanılamıyor: aboneliğe düş


def test_old_cli_version_is_reported_clearly(env):
    make, calls, repo = env
    conductor, store, ws, logs = make({"claude": {}, "agy": {}})
    worker = conductor.workers["claude"]
    script = "import sys; sys.stderr.write(\"error: unknown option '--permission-prompts'\"); sys.exit(1)"
    worker.command = [sys.executable, "-c", script]
    res = worker.run("x", repo, edit=True)
    assert res.unavailable and "güncelle" in res.error
