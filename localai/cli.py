"""Komut satırı arayüzü.

Kullanım:
    python -m localai                 # etkileşimli oturum
    python -m localai "soru"          # tek seferlik görev
    python -m localai --mode local    # sadece yerel model
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .agent import Events, TaskOutcome
from .config import Config, cloud_credentials_present, load_config
from .prompts import build_system_prompt
from .providers.base import ProviderError, StepResult, ToolCall, ToolResult
from .providers.claude import ClaudeProvider
from .providers.ollama import OllamaProvider
from .session import Session, default_ledger
from .tools import TOOL_SPECS, ToolBox

HELP = """\
Komutlar:
  /mod auto|local|cloud   Yönlendirme modunu değiştir (şu an: {mode})
  /yukselt                Son isteği bulut modelde tekrar çalıştır
  /gerial                 Son görevin dosya değişikliklerini geri al
  /maliyet                Bu ayki kullanım ve harcama
  /sifirla                Konuşmayı temizle
  /yardim                 Bu yardım
  /cikis                  Çık

İsteğin içine @bulut yazarsan o istek buluta, @yerel yazarsan yerel modele gider.
Satır sonuna \\ koyarak çok satırlı istek yazabilirsin.
"""


# -- renkler ------------------------------------------------------------------

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        os.system("")  # Windows 10+ terminalinde ANSI renklerini açar
    return True


COLOR = _supports_color()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def dim(t: str) -> str:
    return c(t, "2")


def green(t: str) -> str:
    return c(t, "32")


def yellow(t: str) -> str:
    return c(t, "33")


def red(t: str) -> str:
    return c(t, "31")


def cyan(t: str) -> str:
    return c(t, "36")


# -- arayüz olayları ----------------------------------------------------------

class ConsoleEvents(Events):
    def __init__(self):
        self.streaming = False
        self.last_streamed = False

    def on_delta(self, text: str) -> None:
        if not self.streaming:
            self.streaming = True
            print()
            text = text.lstrip()
        print(text, end="", flush=True)

    def on_step(self, step: StepResult) -> None:
        self.last_streamed = step.streamed
        if self.streaming:
            print("\n")
            self.streaming = False

    def on_text(self, text: str) -> None:
        if not self.last_streamed:
            print(f"\n{text.strip()}\n")

    def on_tool_call(self, call: ToolCall) -> None:
        args = json.dumps(call.arguments, ensure_ascii=False)
        if len(args) > 160:
            args = args[:160] + "…"
        print(cyan(f"  → {call.name} {args}"))

    def on_tool_result(self, result: ToolResult) -> None:
        first = result.content.strip().splitlines()[0] if result.content.strip() else ""
        if result.is_error:
            print(red(f"    ✗ {first[:200]}"))
        else:
            print(dim(f"    ✓ {first[:200]}"))


class Approver:
    def __init__(self, auto: bool):
        self.always = auto

    def __call__(self, tool: str, summary: str) -> bool:
        if self.always:
            return True
        title, _, body = summary.partition("\n")
        print(yellow(f"\n  Onay gerekiyor — {title}"))
        for line in body.splitlines():
            if line.startswith("+"):
                print(green(f"    {line}"))
            elif line.startswith("-"):
                print(red(f"    {line}"))
            elif line.startswith("@@"):
                print(cyan(f"    {line}"))
            else:
                print(dim(f"    {line}"))
        while True:
            try:
                answer = input(yellow("  İzin veriyor musun? [e]vet / [h]ayır / [t]ümüne evet: ")).strip().lower()
            except EOFError:
                return False
            if answer in {"e", "evet", "y", "yes"}:
                return True
            if answer in {"h", "hayır", "hayir", "n", "no", ""}:
                return False
            if answer in {"t", "tümü", "tumu", "a", "all"}:
                self.always = True
                return True


# -- kurulum ------------------------------------------------------------------

def check_ollama(cfg: Config) -> str | None:
    """Sorun varsa kullanıcıya gösterilecek uyarıyı döner."""
    try:
        with urllib.request.urlopen(cfg.ollama_host.rstrip("/") + "/api/tags", timeout=3) as resp:
            models = [m.get("name", "") for m in json.loads(resp.read().decode()).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return f"Ollama'ya ulaşılamadı ({cfg.ollama_host}). Yerel model için Ollama'yı başlat."
    wanted = cfg.local_model if ":" in cfg.local_model else cfg.local_model + ":latest"
    if wanted not in models:
        return f"'{cfg.local_model}' modeli indirilmemiş. Çalıştır: ollama pull {cfg.local_model}"
    return None


def build_session(cfg: Config, workspace: Path, approver: Approver) -> Session:
    tools = TOOL_SPECS
    providers = {
        "local": OllamaProvider(
            model=cfg.local_model,
            system_prompt=build_system_prompt(workspace, local=True),
            tools=tools,
            host=cfg.ollama_host,
            num_ctx=cfg.num_ctx,
        ),
        "cloud": ClaudeProvider(
            model=cfg.cloud_model,
            system_prompt=build_system_prompt(workspace, local=False),
            tools=tools,
            effort=cfg.effort,
        ),
    }
    toolboxes = {
        "local": ToolBox(workspace, approver, cfg.local_tool_output_chars),
        "cloud": ToolBox(workspace, approver, cfg.cloud_tool_output_chars),
    }
    cloud_ok = cloud_credentials_present() and cfg.monthly_budget_usd != 0
    return Session(cfg, providers, toolboxes, default_ledger(), cloud_ok, ConsoleEvents())


def format_cost(cost: float | None) -> str:
    if cost is None:
        return "fiyat bilinmiyor"
    if cost == 0:
        return "ücretsiz"
    return f"${cost:.4f}"


def report(outcome: TaskOutcome, provider: str) -> None:
    u = outcome.usage
    tokens = f"{u.input_tokens + u.cache_read_tokens + u.cache_write_tokens:,} girdi / {u.output_tokens:,} çıktı token"
    line = f"[{provider}: {outcome.model} · {outcome.steps} adım · {tokens} · {format_cost(outcome.cost)}]"
    print(dim(line))
    if not outcome.ok:
        print(yellow(f"Görev tamamlanamadı: {outcome.problem}."))


# -- ana döngü ----------------------------------------------------------------

def handle(session: Session, text: str) -> None:
    decision = session.route(text)
    label = "yerel" if decision.provider == "local" else "bulut"
    print(dim(f"[{label} → {decision.reason}]"))
    try:
        outcome = session.run_on(decision.provider, decision.text)
    except ProviderError as e:
        print(red(f"Hata: {e}"))
        return
    report(outcome, decision.provider)
    if not outcome.ok and decision.provider == "local" and session.cloud_available:
        print(yellow("İpucu: /yukselt yazarsan aynı isteği bulut modelde denerim."))


def command(session: Session, line: str) -> bool:
    """Komutu işler. Çıkılması gerekiyorsa False döner."""
    parts = line.split()
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in {"/cikis", "/çıkış", "/exit", "/quit"}:
        return False
    if cmd in {"/yardim", "/yardım", "/help"}:
        print(HELP.format(mode=session.mode))
    elif cmd == "/mod":
        if args and args[0] in {"auto", "local", "cloud"}:
            session.mode = args[0]
            print(f"Mod: {session.mode}")
            if session.mode == "cloud" and not session.cloud_available:
                print(yellow("Uyarı: ANTHROPIC_API_KEY yok veya bütçe 0; istekler yine yerel modele gider."))
        else:
            print("Kullanım: /mod auto|local|cloud")
    elif cmd in {"/yukselt", "/yükselt", "/escalate"}:
        if not session.cloud_available:
            print(yellow("Bulut kapalı: ANTHROPIC_API_KEY ayarlı değil veya bütçe 0."))
            return True
        try:
            outcome = session.escalate()
        except ProviderError as e:
            print(red(f"Hata: {e}"))
            return True
        if outcome is None:
            print("Yükseltilecek bir istek yok.")
        else:
            report(outcome, "cloud")
    elif cmd in {"/gerial", "/geri-al", "/undo"}:
        restored = session.undo()
        if restored:
            print("Geri alındı: " + ", ".join(restored))
            print(dim("Not: run_command ile yapılan değişiklikler (ör. paket kurulumu) geri alınmaz."))
        else:
            print("Geri alınacak dosya değişikliği yok.")
    elif cmd in {"/maliyet", "/cost"}:
        summary = session.ledger.month_summary()
        limit = session.cfg.budget_limit
        spent = f"Bu ay bulut harcaması: ${session.ledger.month_usd():.4f}"
        print(spent + (f" / ${limit:.2f}" if limit is not None else ""))
        for key, value in sorted(summary.items()):
            if key != "usd" and value:
                print(dim(f"  {key}: {int(value):,}"))
    elif cmd in {"/sifirla", "/sıfırla", "/reset"}:
        session.reset()
        print("Konuşma temizlendi.")
    else:
        print(f"Bilinmeyen komut: {cmd}. /yardim yaz.")
    return True


def read_request() -> str:
    lines = []
    prompt = green("› ")
    while True:
        line = input(prompt)
        if line.endswith("\\"):
            lines.append(line[:-1])
            prompt = green("  ")
            continue
        lines.append(line)
        return "\n".join(lines).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localai", description="Yerel + bulut hibrit kodlama asistanı")
    parser.add_argument("request", nargs="*", help="Tek seferlik istek (boş bırakılırsa etkileşimli mod)")
    parser.add_argument("-w", "--workspace", default=".", help="Çalışma klasörü (varsayılan: bulunduğun klasör)")
    parser.add_argument("-m", "--mode", choices=["auto", "local", "cloud"], help="Yönlendirme modu")
    parser.add_argument("-y", "--yes", action="store_true", help="Dosya yazma ve komutlar için onay sorma")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(red(f"Klasör bulunamadı: {workspace}"))
        return 2
    try:
        cfg = load_config(workspace)
    except (ValueError, OSError) as e:
        print(red(f"Ayar hatası: {e}"))
        return 2
    if args.mode:
        cfg.mode = args.mode

    session = build_session(cfg, workspace, Approver(cfg.auto_approve or args.yes))

    print(dim(f"Çalışma klasörü: {workspace}"))
    cloud = cfg.cloud_model if session.cloud_available else "kapalı"
    print(dim(f"Yerel: {cfg.local_model} · Bulut: {cloud} · Mod: {session.mode}"))
    if warning := check_ollama(cfg):
        print(yellow(warning))

    if args.request:
        handle(session, " ".join(args.request))
        return 0

    print(dim("Yardım için /yardim, çıkmak için /cikis yaz.\n"))
    while True:
        try:
            text = read_request()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        try:
            if text.startswith("/"):
                if not command(session, text):
                    return 0
            else:
                handle(session, text)
        except KeyboardInterrupt:
            print(yellow("\nİptal edildi."))
