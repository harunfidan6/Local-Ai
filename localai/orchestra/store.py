"""Görev kuyruğu, işçi durumu ve kullanım kaydı (SQLite).

Şef kapanıp açılsa da hiçbir şey kaybolmaz; ikinci bir terminalden görev
eklemek de aynı veritabanına yazar.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# Görev durumları
QUEUED = "sırada"            # uygulayıcı bekliyor
PLANNING = "planlanacak"     # hedef: parçalara bölünmeyi bekliyor
WAITING = "alt görevlerde"   # hedef: alt görevleri bitmeyi bekliyor
REVIEW = "denetlenecek"      # uygulama bitti, denetçi bekliyor
PLANNING_RUN = "planlanıyor"
IMPL_RUN = "uygulanıyor"
REVIEW_RUN = "denetleniyor"
DONE = "bitti"               # onaylandı ve entegrasyon dalına birleştirildi
NEEDS_HUMAN = "insan gerekli"
CANCELED = "iptal"

ACTIVE = {QUEUED, PLANNING, WAITING, REVIEW, PLANNING_RUN, IMPL_RUN, REVIEW_RUN}
# Şef çökerse/kapanırsa yarım kalan işler bu durumlara geri döner
RECOVER = {PLANNING_RUN: PLANNING, IMPL_RUN: QUEUED, REVIEW_RUN: REVIEW}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER,
    kind TEXT NOT NULL,              -- 'hedef' veya 'görev'
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    difficulty TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '[]',
    worker TEXT,
    reviewer TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    feedback TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    branch TEXT,
    worktree TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    ts REAL NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    name TEXT PRIMARY KEY,
    cooling_until REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS usage (
    ts REAL NOT NULL,
    worker TEXT NOT NULL,
    minutes REAL NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0
);
"""


@dataclass
class Task:
    id: int
    parent_id: int | None
    kind: str
    title: str
    prompt: str
    difficulty: str
    status: str
    depends_on: list[int] = field(default_factory=list)
    worker: str | None = None
    reviewer: str | None = None
    attempts: int = 0
    feedback: str = ""
    result: str = ""
    branch: str | None = None
    worktree: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self.db:
            return self.db.execute(sql, args)

    # -- görevler -------------------------------------------------------------

    def add(
        self,
        title: str,
        prompt: str,
        kind: str = "görev",
        difficulty: str = "normal",
        parent_id: int | None = None,
        depends_on: list[int] | None = None,
    ) -> int:
        now = time.time()
        status = PLANNING if kind == "hedef" else QUEUED
        cur = self._exec(
            "INSERT INTO tasks (parent_id, kind, title, prompt, difficulty, status, depends_on, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (parent_id, kind, title, prompt, difficulty, status, json.dumps(depends_on or []), now, now),
        )
        task_id = cur.lastrowid
        self.log(task_id, f"Eklendi ({kind}, {difficulty}): {title}")
        return task_id

    def get(self, task_id: int) -> Task | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def all(self, include_finished: bool = True) -> list[Task]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        tasks = [_task(r) for r in rows]
        return tasks if include_finished else [t for t in tasks if t.status in ACTIVE]

    def children(self, parent_id: int) -> list[Task]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM tasks WHERE parent_id = ? ORDER BY id", (parent_id,)).fetchall()
        return [_task(r) for r in rows]

    def update(self, task_id: int, **fields) -> None:
        if "depends_on" in fields:
            fields["depends_on"] = json.dumps(fields["depends_on"])
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))

    def claim(self, task_id: int, from_status: str, to_status: str, **fields) -> bool:
        """Durumu atomik olarak değiştirir; başka biri önce aldıysa False döner."""
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = self._exec(
            f"UPDATE tasks SET status = ?, {cols} WHERE id = ? AND status = ?",
            (to_status, *fields.values(), task_id, from_status),
        )
        return cur.rowcount == 1

    # -- olay günlüğü ---------------------------------------------------------

    def log(self, task_id: int | None, message: str) -> None:
        self._exec("INSERT INTO events (task_id, ts, message) VALUES (?, ?, ?)", (task_id, time.time(), message))

    def events(self, task_id: int | None = None, limit: int = 50) -> list[tuple[float, int | None, str]]:
        with self._lock:
            if task_id is None:
                rows = self.db.execute(
                    "SELECT ts, task_id, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT ts, task_id, message FROM events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
        return [(r[0], r[1], r[2]) for r in reversed(rows)]

    # -- işçiler ve kullanım --------------------------------------------------

    def cooling_until(self, worker: str) -> tuple[float, str]:
        with self._lock:
            row = self.db.execute("SELECT cooling_until, note FROM workers WHERE name = ?", (worker,)).fetchone()
        return (row[0], row[1]) if row else (0.0, "")

    def set_cooling(self, worker: str, until: float, note: str) -> None:
        self._exec(
            "INSERT INTO workers (name, cooling_until, note) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET cooling_until = excluded.cooling_until, note = excluded.note",
            (worker, until, note),
        )

    def record_usage(self, worker: str, minutes: float, cost_usd: float) -> None:
        self._exec("INSERT INTO usage (ts, worker, minutes, cost_usd) VALUES (?, ?, ?, ?)",
                   (time.time(), worker, minutes, cost_usd))

    def usage_since(self, worker: str, since: float) -> tuple[float, float, int]:
        """(dakika, tahmini dolar, çalıştırma sayısı)"""
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(SUM(minutes), 0), COALESCE(SUM(cost_usd), 0), COUNT(*)"
                " FROM usage WHERE worker = ? AND ts >= ?",
                (worker, since),
            ).fetchone()
        return float(row[0]), float(row[1]), int(row[2])


def _task(row: sqlite3.Row) -> Task:
    data = dict(row)
    data["depends_on"] = json.loads(data["depends_on"] or "[]")
    return Task(**data)
