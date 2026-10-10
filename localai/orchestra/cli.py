"""`localai orkestra ...` komutları.

    localai orkestra ekle "Kullanıcı girişine şifre sıfırlama ekle"   # hedef ekler (şef parçalar)
    localai orkestra ekle --direkt --zorluk basit "README'deki yazım hatalarını düzelt"
    localai orkestra calistir     # şefi başlatır; durmadan çalışır
    localai orkestra durum        # görevler, işçiler, kotalar
    localai orkestra gunluk [ID]  # olay günlüğü
    localai orkestra tekrar ID    # takılan görevi yeniden sıraya al
    localai orkestra iptal ID
"""

from __future__ import annotations

import argparse
import threading
import time
from datetime import datetime
from pathlib import Path

from ..config import load_config
from . import store as S
from .conductor import Conductor
from .config import load_orchestra_config
from .policy import Policy
from .store import Store
from .workers import build_workers, resolve_command
from .workspace import GitError, Workspace


def _open(workspace: Path):
    cfg = load_orchestra_config(workspace)
    ws = Workspace(workspace, cfg.data_dir(workspace), cfg.integration_branch)
    store = Store(cfg.data_dir(ws.repo) / "orkestra.db")
    return cfg, ws, store


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m %H:%M")


def cmd_add(args, cfg, ws, store) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("Görev metni boş olamaz.")
        return 2
    title = args.baslik or (text.splitlines()[0][:80])
    kind = "görev" if args.direkt else "hedef"
    task_id = store.add(title, text, kind=kind, difficulty=args.zorluk)
    what = "Görev" if args.direkt else "Hedef (şef alt görevlere bölecek)"
    print(f"{what} #{task_id} eklendi. Şef çalışmıyorsa: localai orkestra calistir")
    return 0


def cmd_status(args, cfg, ws, store) -> int:
    tasks = store.all(include_finished=args.hepsi)
    if not tasks:
        print("Aktif görev yok." if not args.hepsi else "Hiç görev yok.")
    for t in tasks:
        indent = "   " if t.parent_id else ""
        who = f" · {t.worker}" if t.worker else ""
        tries = f" · deneme {t.attempts}" if t.attempts else ""
        print(f"{indent}#{t.id:<4} [{t.status}] {t.title}  ({t.kind}, {t.difficulty}{who}{tries})")
        if t.status == S.NEEDS_HUMAN and t.feedback:
            print(f"{indent}       ↳ {t.feedback.splitlines()[0][:150]}")

    print("\nİşçiler:")
    workers = build_workers(cfg, load_config(ws.repo))
    policy = Policy(cfg, store, workers)
    since = time.time() - cfg.balance_window_hours * 3600
    for name, worker in workers.items():
        until, note = store.cooling_until(name)
        minutes, cost, runs = store.usage_since(name, since)
        if name != "yerel":
            installed = resolve_command(worker.command) is not None
        else:
            installed = True
        state = "hazır" if installed else "KURULU DEĞİL"
        if until > time.time():
            state = f"dinleniyor ({_fmt_time(until)}'e kadar: {note[:60]})"
        role = "uygular+denetler" if worker.can_edit else "sadece planlar+denetler"
        # Abonelikte gerçek ödeme değil; Claude Code'un "API'den olsa ne tutardı" tahmini
        extra = f", API karşılığı ~${cost:.2f}" if cost else ""
        print(f"  {name:<12} {state:<20} son {cfg.balance_window_hours:g} saat: {runs} iş, "
              f"{minutes:.0f} dk{extra} · yük {policy.load(name):.1f} · {role}")
    print(f"\nOnaylanan işler '{ws.branch}' dalında birikir: git log {ws.branch}")
    return 0


def cmd_log(args, cfg, ws, store) -> int:
    for ts, task_id, message in store.events(args.id, limit=args.son):
        prefix = f"#{task_id} " if task_id else ""
        print(f"{_fmt_time(ts)} {prefix}{message}")
    return 0


def cmd_retry(args, cfg, ws, store) -> int:
    task = store.get(args.id)
    if not task:
        print(f"#{args.id} bulunamadı.")
        return 1
    status = S.PLANNING if task.kind == "hedef" and not store.children(task.id) else (
        S.WAITING if task.kind == "hedef" else S.QUEUED)
    store.update(task.id, status=status, attempts=0)
    for child in store.children(task.id):
        if child.status == S.NEEDS_HUMAN:
            store.update(child.id, status=S.QUEUED, attempts=0)
    store.log(task.id, "Kullanıcı tekrar sıraya aldı")
    print(f"#{task.id} tekrar sıraya alındı.")
    return 0


def cmd_cancel(args, cfg, ws, store) -> int:
    task = store.get(args.id)
    if not task:
        print(f"#{args.id} bulunamadı.")
        return 1
    for t in [task, *store.children(task.id)]:
        if t.status in S.ACTIVE or t.status == S.NEEDS_HUMAN:
            store.update(t.id, status=S.CANCELED)
            ws.remove(t.branch, t.worktree)
    store.log(task.id, "Kullanıcı iptal etti")
    print(f"#{task.id} iptal edildi.")
    return 0


def cmd_run(args, cfg, ws, store) -> int:
    workers = build_workers(cfg, load_config(ws.repo))
    if not workers:
        print("Hiç işçi açık değil. localai.toml [orkestra] bölümünü kontrol et.")
        return 2
    for name, worker in workers.items():
        if name != "yerel" and resolve_command(worker.command) is None:
            print(f"Uyarı: '{worker.command[0]}' bulunamadı; {name} kullanılamayacak.")
    conductor = Conductor(cfg, store, ws, workers)
    stop = threading.Event()
    try:
        conductor.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
        print("\nDurduruluyor; çalışan işler bitince kapanacak (hemen kapatmak için tekrar Ctrl+C).")
        try:
            conductor.wait_idle()
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="localai orkestra", description="Claude Code + Antigravity + yerel model şefi"
    )
    parser.add_argument("-w", "--workspace", default=".", help="Proje klasörü (git deposu)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ekle", help="Hedef veya görev ekle")
    p.add_argument("text", nargs="+")
    p.add_argument("--direkt", action="store_true", help="Parçalamadan tek görev olarak ekle")
    p.add_argument("--zorluk", choices=["basit", "normal", "zor"], default="normal")
    p.add_argument("--baslik")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("durum", help="Görevleri ve işçileri göster")
    p.add_argument("--hepsi", action="store_true", help="Bitenleri de göster")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("gunluk", help="Olay günlüğü")
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--son", type=int, default=40)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("tekrar", help="Takılan görevi tekrar sıraya al")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("iptal", help="Görevi iptal et")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("calistir", help="Şefi başlat (durmadan çalışır)")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    try:
        cfg, ws, store = _open(Path(args.workspace).resolve())
    except (GitError, ValueError) as e:
        print(f"Hata: {e}")
        return 2
    return args.func(args, cfg, ws, store)

