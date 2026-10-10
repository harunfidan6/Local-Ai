"""Git ile yalıtım: her görev kendi worktree'sinde ve kendi dalında çalışır.

- İşçiler aynı anda farklı görevlerde çalışabilir, birbirinin dosyasını ezmez.
- Onaylanan iş `orkestra` entegrasyon dalına birleştirilir; senin ana dalına
  sen birleştirene kadar dokunulmaz.
- Salt-okunur işlerden (planlama, denetim) sonra worktree'de değişiklik kalırsa geri alınır.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


class Workspace:
    def __init__(self, repo: Path, data_dir: Path, integration_branch: str):
        try:
            top = git(repo, "rev-parse", "--show-toplevel")
        except (GitError, FileNotFoundError) as e:
            raise GitError(
                f"{repo} bir git deposu değil (veya git kurulu değil). "
                "Önce: git init && git add -A && git commit -m ilk"
            ) from e
        self.repo = Path(top)
        self.branch = integration_branch
        self.root = data_dir / "worktrees"
        self.root.mkdir(parents=True, exist_ok=True)
        self.integration = data_dir / "entegrasyon"
        # Paralel işçiler aynı depoda worktree ekleyip birleştirme yapar; git'in
        # kilit dosyaları çakışmasın diye depo yapısını değiştiren işlemler sıralanır.
        self.lock = threading.RLock()
        self._ensure_integration()

    def _ensure_integration(self) -> None:
        if not git(self.repo, "rev-parse", "--verify", "--quiet", "HEAD", check=False):
            raise GitError("Depoda hiç commit yok. Önce bir commit oluştur: git add -A && git commit -m ilk")
        if not git(self.repo, "branch", "--list", self.branch):
            git(self.repo, "branch", self.branch, "HEAD")
        if not (self.integration / ".git").exists():
            git(self.repo, "worktree", "prune")
            git(self.repo, "worktree", "add", str(self.integration), self.branch)

    @contextmanager
    def readonly_snapshot(self, name: str):
        """Entegrasyon dalının güncel halini ayrı, geçici bir worktree'de açar (planlama için)."""
        path = self.root / name
        with self.lock:
            if path.exists():
                self.remove(None, path)
            git(self.repo, "worktree", "add", "-q", "--detach", str(path), self.branch)
        try:
            yield path
        finally:
            with self.lock:
                self.remove(None, path)

    def create(self, task_id: int) -> tuple[str, Path]:
        with self.lock:
            return self._create(task_id)

    def _create(self, task_id: int) -> tuple[str, Path]:
        # "orkestra/..." olamaz: "orkestra" dalıyla ref çakışması olur
        branch = f"{self.branch}-gorev-{task_id}"
        path = self.root / f"gorev-{task_id}"
        if path.exists():
            return branch, path
        git(self.repo, "worktree", "prune")
        if git(self.repo, "branch", "--list", branch):
            git(self.repo, "worktree", "add", str(path), branch)
        else:
            git(self.repo, "worktree", "add", "-b", branch, str(path), self.branch)
        return branch, path

    def commit_all(self, path: Path, message: str) -> bool:
        """Worktree'deki tüm değişiklikleri commit'ler. Değişiklik yoksa False."""
        git(path, "add", "-A")
        if not git(path, "status", "--porcelain"):
            return False
        git(path, "-c", "user.name=Orkestra", "-c", "user.email=orkestra@localhost", "commit", "-q", "-m", message)
        return True

    def has_changes_vs_base(self, path: Path) -> bool:
        return bool(git(path, "diff", "--stat", f"{self.branch}...HEAD"))

    def diff(self, path: Path, max_chars: int = 15000) -> str:
        text = git(path, "diff", f"{self.branch}...HEAD")
        if len(text) > max_chars:
            rest = len(text) - max_chars
            text = text[:max_chars] + f"\n[... {rest} karakter daha; tamamı için: git diff {self.branch}...HEAD]"
        return text

    def discard_uncommitted(self, path: Path) -> bool:
        """Salt-okunur bir işten sonra kalan değişiklikleri siler. Silinecek bir şey varsa True."""
        dirty = bool(git(path, "status", "--porcelain"))
        if dirty:
            git(path, "reset", "-q", "--hard")
            git(path, "clean", "-q", "-fd")
        return dirty

    def merge(self, branch: str, message: str) -> bool:
        """Görev dalını entegrasyon dalına birleştirir. Çakışma olursa geri alır ve False döner."""
        with self.lock:
            proc = subprocess.run(
                ["git", "-c", "user.name=Orkestra", "-c", "user.email=orkestra@localhost",
                 "merge", "--no-ff", "-q", "-m", message, branch],
                cwd=self.integration, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                git(self.integration, "merge", "--abort", check=False)
                return False
            return True

    def remove(self, branch: str | None, path: str | Path | None) -> None:
        with self.lock:
            self._remove(branch, path)

    def _remove(self, branch: str | None, path: str | Path | None) -> None:
        if path:
            git(self.repo, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
            git(self.repo, "worktree", "prune", check=False)
        if branch:
            git(self.repo, "branch", "-D", branch, check=False)
