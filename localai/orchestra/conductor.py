"""Orkestra şefi: görevleri parçalar, işçilere dağıtır, sonuçları denetletir, birleştirir.

Akış:
    hedef ──(planlayıcı)──► alt görevler ──(uygulayıcı)──► test ──► (denetçi) ──► entegrasyon dalı
                                     ▲                         │ başarısız       │ reddedildi
                                     └─────── geri bildirim ◄──┴─────────────────┘

Her işçi aynı anda tek iş yapar; farklı işçiler paralel çalışır.
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import store as S
from .config import OrchestraConfig
from .policy import Policy
from .store import Store, Task
from .workers import Worker, WorkResult
from .workspace import GitError, Workspace

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "prompt": {"type": "string"},
                    "difficulty": {"type": "string", "enum": ["basit", "normal", "zor"]},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["title", "prompt", "difficulty", "depends_on"],
            },
        }
    },
    "required": ["subtasks"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "summary", "issues"],
}

PLAN_PROMPT = """\
Sen bir yazılım ekibinde planlayıcısın. Şu anki klasör projenin güncel hali; dosyaları okuyabilirsin ama DEĞİŞTİRME.

Hedef #{id}: {title}
{prompt}

Bu hedefi, başka bir geliştiricinin tek başına yapabileceği 1-8 alt göreve böl:
- Her alt görevin "prompt" alanı kendi başına anlaşılır olsun: hangi dosyalar, ne yapılacak, nasıl doğrulanacak.
- "difficulty": "basit" (tek dosya, mekanik değişiklik, küçük düzeltme), "normal" veya "zor" (çok dosyalı, tasarım
  kararı, zor hata ayıklama). Basit işler ücretsiz yerel modele gider; gerçekten basit olanları basit işaretle.
- "depends_on": bu alt görevden önce bitmesi gereken alt görevlerin listedeki sıra numaraları (1'den başlar).
- Hedef zaten küçükse tek alt görev yeterli.
"""

IMPL_PROMPT = """\
Sen bir yazılım ekibinde uygulayıcısın. Şu anki klasör bu görev için ayrılmış bir git worktree'si.

Görev #{id}: {title}
{prompt}
{context}
Kurallar:
- Sadece bu görevin kapsamındaki değişiklikleri yap.
- Önceki denemelerden kalan değişiklikler olabilir; önce `git status` ve `git diff` ile bak.
{check_rule}- Commit atma; bunu orkestra yapacak.
- Bitince ne yaptığını 3-5 cümleyle özetle.
"""

REVIEW_PROMPT = """\
Sen bir yazılım ekibinde denetçisin. Şu anki klasör, başka bir yapay zekânın bu görev için yaptığı değişiklikleri
içeren git worktree'si. Dosyaları okuyabilirsin ama DEĞİŞTİRME.

Görev #{id}: {title}
{prompt}

Uygulayıcının ({worker}) özeti:
{result}

Değişiklikler (`git diff {base}...HEAD`):
{diff}
{check}
Şunları kontrol et: görev gerçekten tamamlanmış mı, hata/eksik var mı, kapsam dışı değişiklik var mı, güvenlik sorunu
var mı. Küçük üslup tercihleri yüzünden reddetme. Reddedersen "issues" listesine uygulayıcının düzeltmesi gereken
somut maddeleri yaz.
"""


class Conductor:
    def __init__(
        self,
        cfg: OrchestraConfig,
        store: Store,
        workspace: Workspace,
        workers: dict[str, Worker],
        say: Callable[[str], None] = print,
        threaded: bool = True,
    ):
        self.cfg = cfg
        self.store = store
        self.ws = workspace
        self.workers = workers
        self.policy = Policy(cfg, store, workers)
        self.say = say
        self.threaded = threaded
        self.busy: set[str] = set()
        self.threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._recover()

    # -- yardımcılar ------------------------------------------------------------

    def _log(self, task_id: int | None, message: str) -> None:
        self.store.log(task_id, message)
        prefix = f"#{task_id} " if task_id else ""
        self.say(f"[{datetime.now():%H:%M:%S}] {prefix}{message}")

    def _recover(self) -> None:
        for task in self.store.all(include_finished=False):
            if task.status in S.RECOVER:
                self.store.update(task.id, status=S.RECOVER[task.status])
                self.store.log(task.id, "Şef yeniden başladı; yarım kalan iş tekrar sıraya alındı")

    def _start(self, worker: str, job: Callable[[], None], task: Task, recover_to: str) -> None:
        with self._lock:
            self.busy.add(worker)

        def wrapper():
            try:
                job()
            except Exception as e:  # bir işin çökmesi şefi durdurmamalı
                self._log(task.id, f"Beklenmeyen hata ({worker}): {e!r}")
                self._fail(self.store.get(task.id) or task, recover_to, f"Beklenmeyen hata: {e!r}")
            finally:
                with self._lock:
                    self.busy.discard(worker)

        if self.threaded:
            thread = threading.Thread(target=wrapper, name=f"is-{worker}", daemon=True)
            self.threads.append(thread)
            thread.start()
        else:
            wrapper()

    def _busy(self) -> set[str]:
        with self._lock:
            return set(self.busy)

    def _account(self, worker: str, res: WorkResult) -> None:
        self.store.record_usage(worker, res.minutes, res.cost_usd)
        if res.limited:
            until = res.reset_at or time.time() + self.cfg.default_cooldown_min * 60
            self.store.set_cooling(worker, until, f"limit: {res.error[:200]}")
            self._log(None, f"{worker} limiti doldu, {datetime.fromtimestamp(until):%d.%m %H:%M}'e kadar dinleniyor")
        elif res.unavailable:
            self.store.set_cooling(worker, time.time() + 15 * 60, res.error[:200])
            self._log(None, f"{worker} kullanılamıyor (15 dk sonra tekrar denenecek): {res.error}")

    def _fail(self, task: Task, back_to: str, feedback: str) -> None:
        attempts = task.attempts + 1
        if attempts >= self.cfg.max_attempts:
            self.store.update(task.id, status=S.NEEDS_HUMAN, attempts=attempts, feedback=feedback)
            self._log(task.id, f"{attempts} denemede olmadı, insan gerekli. Son sorun: {feedback[:300]}")
        else:
            self.store.update(task.id, status=back_to, attempts=attempts, feedback=feedback)
            self._log(task.id, f"Tekrar sıraya alındı (deneme {attempts}/{self.cfg.max_attempts})")

    def _run_check(self, path: Path) -> tuple[bool, str]:
        if not self.cfg.check_command:
            return True, ""
        try:
            proc = subprocess.run(
                self.cfg.check_command, shell=True, cwd=path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.cfg.task_timeout_min * 60,
            )
        except subprocess.TimeoutExpired:
            return False, "Test komutu zaman aşımına uğradı."
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode == 0, output[-4000:]

    # -- zamanlama --------------------------------------------------------------

    def tick(self) -> int:
        """Bir zamanlama turu: boştaki işçilere iş verir. Başlatılan iş sayısını döner."""
        started = 0
        tasks = self.store.all(include_finished=False)
        by_id = {t.id: t for t in self.store.all()}

        for goal in (t for t in tasks if t.status == S.WAITING):
            self._update_goal(goal)

        # Önce denetimler (biten işi kapatmak kuyruğu açar), sonra uygulama, en son planlama
        for task in (t for t in tasks if t.status == S.REVIEW):
            reviewer = self.policy.pick_reviewer(task, self._busy())
            if reviewer and self.store.claim(task.id, S.REVIEW, S.REVIEW_RUN):
                self._start(reviewer, lambda t=task, w=reviewer: self._review(t, w), task, S.REVIEW)
                started += 1

        for task in (t for t in tasks if t.status == S.QUEUED):
            if any(by_id.get(d) is None or by_id[d].status != S.DONE for d in task.depends_on):
                continue
            worker = self.policy.pick_implementer(task, self._busy())
            if worker and self.store.claim(task.id, S.QUEUED, S.IMPL_RUN):
                self._start(worker, lambda t=task, w=worker: self._implement(t, w), task, S.QUEUED)
                started += 1

        for goal in (t for t in tasks if t.status == S.PLANNING):
            planner = self.policy.pick_planner(self._busy())
            if planner and self.store.claim(goal.id, S.PLANNING, S.PLANNING_RUN):
                self._start(planner, lambda g=goal, w=planner: self._plan(g, w), goal, S.PLANNING)
                started += 1
        return started

    def wait_idle(self) -> None:
        for thread in list(self.threads):
            thread.join()
        self.threads = [t for t in self.threads if t.is_alive()]

    def run_forever(self, stop: threading.Event) -> None:
        self._log(None, f"Şef çalışıyor. İşçiler: {', '.join(self.workers)}. Durdurmak için Ctrl+C.")
        while not stop.is_set():
            self.tick()
            stop.wait(self.cfg.poll_seconds)

    def _update_goal(self, goal: Task) -> None:
        children = self.store.children(goal.id)
        if children and all(c.status == S.DONE for c in children):
            summary = "\n".join(f"- #{c.id} {c.title}" for c in children)
            self.store.update(goal.id, status=S.DONE, result=summary)
            self._log(goal.id, f"Hedef tamamlandı ({len(children)} alt görev). "
                               f"Sonuç '{self.ws.branch}' dalında; incele ve ana dalına birleştir.")
        elif any(c.status in (S.NEEDS_HUMAN, S.CANCELED) for c in children):
            self.store.update(goal.id, status=S.NEEDS_HUMAN)
            self._log(goal.id, "Bir alt görev takıldı; hedef insan bekliyor")

    # -- işler ------------------------------------------------------------------

    def _plan(self, goal: Task, worker: str) -> None:
        self._log(goal.id, f"{worker} planlıyor")
        # Geçici kopyada çalışır; planlayıcı bir şey değiştirse bile kopya silinir
        with self.ws.readonly_snapshot(f"plan-{goal.id}") as path:
            res = self.workers[worker].run(
                PLAN_PROMPT.format(id=goal.id, title=goal.title, prompt=goal.prompt),
                path, edit=False, schema=PLAN_SCHEMA, timeout_s=self.cfg.task_timeout_min * 60,
            )
        self._account(worker, res)
        if res.limited or res.unavailable:
            self.store.update(goal.id, status=S.PLANNING)
            return
        subtasks = (res.structured or {}).get("subtasks") if res.ok else None
        if not isinstance(subtasks, list) or not subtasks:
            self._fail(goal, S.PLANNING, f"Planlayıcı geçerli bir plan vermedi: {res.error or res.text[:300]}")
            return

        ids: list[int] = []
        for item in subtasks:
            difficulty = item.get("difficulty") if item.get("difficulty") in ("basit", "normal", "zor") else "normal"
            deps = [ids[i - 1] for i in item.get("depends_on") or [] if isinstance(i, int) and 1 <= i <= len(ids)]
            ids.append(self.store.add(
                str(item.get("title") or "Alt görev")[:200], str(item.get("prompt") or ""),
                difficulty=difficulty, parent_id=goal.id, depends_on=deps,
            ))
        self.store.update(goal.id, status=S.WAITING, worker=worker)
        self._log(goal.id, f"{worker} hedefi {len(ids)} alt göreve böldü: " + ", ".join(f"#{i}" for i in ids))

    def _implement(self, task: Task, worker: str) -> None:
        try:
            branch, path = self.ws.create(task.id)
        except GitError as e:
            self.store.update(task.id, status=S.NEEDS_HUMAN, feedback=str(e))
            self._log(task.id, f"Worktree oluşturulamadı: {e}")
            return
        self.store.update(task.id, branch=branch, worktree=str(path), worker=worker, reviewer=None)
        self._log(task.id, f"{worker} uyguluyor ({task.difficulty})")

        context = ""
        if task.parent_id and (goal := self.store.get(task.parent_id)):
            context += f"\nBu görev şu büyük hedefin parçası — #{goal.id} {goal.title}:\n{goal.prompt}\n"
        if task.feedback:
            context += f"\nÖNCEKİ DENEME REDDEDİLDİ. Düzeltilmesi gerekenler:\n{task.feedback}\n"
        check_rule = (f"- Bitirmeden önce `{self.cfg.check_command}` komutunu çalıştır ve geçtiğinden emin ol.\n"
                      if self.cfg.check_command else "")
        res = self.workers[worker].run(
            IMPL_PROMPT.format(id=task.id, title=task.title, prompt=task.prompt, context=context,
                               check_rule=check_rule),
            path, edit=True, timeout_s=self.cfg.task_timeout_min * 60,
        )
        self._account(worker, res)
        if res.limited or res.unavailable:
            # Yarım kalan değişiklikler worktree'de kalır; sıradaki işçi devam eder
            self.store.update(task.id, status=S.QUEUED)
            return
        if not res.ok:
            self._fail(task, S.QUEUED, f"{worker} hata verdi: {res.error}")
            return
        self.ws.commit_all(path, f"Görev #{task.id}: {task.title} ({worker})")
        self.store.update(task.id, status=S.REVIEW, result=res.text[:8000])
        self._log(task.id, f"{worker} bitirdi ({res.minutes:.1f} dk), denetime gidiyor")

    def _review(self, task: Task, reviewer: str) -> None:
        task = self.store.get(task.id) or task
        if not task.worktree or not Path(task.worktree).is_dir():
            self.store.update(task.id, status=S.QUEUED)
            self._log(task.id, "Görevin çalışma klasörü bulunamadı; uygulama baştan yapılacak")
            return
        path = Path(task.worktree)
        passed, output = self._run_check(path)
        if not passed:
            # Testler geçmiyorsa denetçi kredisi harcamaya gerek yok
            self._log(task.id, f"Testler başarısız ('{self.cfg.check_command}'), uygulayıcıya geri dönüyor")
            self._fail(task, S.QUEUED, f"`{self.cfg.check_command}` başarısız:\n{output}")
            return
        if not self.ws.has_changes_vs_base(path) and not task.result.strip():
            self._fail(task, S.QUEUED, "Hiçbir değişiklik yapılmamış ve açıklama da yok.")
            return

        self._log(task.id, f"{reviewer} denetliyor")
        check = f"\nTest komutu `{self.cfg.check_command}` geçti.\n" if self.cfg.check_command else ""
        res = self.workers[reviewer].run(
            REVIEW_PROMPT.format(id=task.id, title=task.title, prompt=task.prompt, worker=task.worker,
                                 result=task.result or "(özet yok)", base=self.ws.branch,
                                 diff=self.ws.diff(path) or "(kod değişikliği yok)", check=check),
            path, edit=False, schema=REVIEW_SCHEMA, timeout_s=self.cfg.task_timeout_min * 60,
        )
        if self.ws.discard_uncommitted(path):
            self._log(task.id, f"{reviewer} denetlerken dosya değiştirdi; değişiklikler geri alındı")
        self._account(reviewer, res)
        if res.limited or res.unavailable:
            self.store.update(task.id, status=S.REVIEW)
            return
        verdict = res.structured if res.ok else None
        if not isinstance(verdict, dict) or not isinstance(verdict.get("approved"), bool):
            self.store.update(task.id, status=S.REVIEW, reviewer=reviewer)  # başka denetçi denensin
            self._log(task.id, f"{reviewer} geçerli bir karar vermedi; başka denetçi denenecek")
            return

        if not verdict["approved"]:
            issues = "\n".join(f"- {i}" for i in verdict.get("issues") or [])
            self._log(task.id, f"{reviewer} reddetti: {verdict.get('summary', '')[:200]}")
            self._fail(task, S.QUEUED, f"Denetçi ({reviewer}): {verdict.get('summary', '')}\n{issues}")
            return

        self._log(task.id, f"{reviewer} onayladı: {verdict.get('summary', '')[:200]}")
        if self.ws.has_changes_vs_base(path):
            if not self.ws.merge(task.branch, f"Görev #{task.id}: {task.title} (uygulayan {task.worker}, "
                                              f"denetleyen {reviewer})"):
                self.store.update(task.id, status=S.NEEDS_HUMAN,
                                  feedback=f"'{self.ws.branch}' dalına birleştirirken çakışma çıktı.")
                self._log(task.id, f"Birleştirme çakışması; '{task.branch}' dalını elle birleştir")
                return
        self.store.update(task.id, status=S.DONE, reviewer=reviewer)
        self.ws.remove(task.branch, task.worktree)
        self._log(task.id, f"Tamamlandı ve '{self.ws.branch}' dalına birleştirildi")
