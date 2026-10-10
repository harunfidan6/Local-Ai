"""İşçiler: Claude Code, Antigravity CLI ve yerel model için ortak arayüz.

Claude Code ve Antigravity, bilgisayarında kurulu ve kendi aboneliğinle giriş
yapılmış resmi komut satırı araçları olarak çalıştırılır. Bu kod hiçbir
kimlik bilgisine dokunmaz; sadece `claude -p` ve `agy -p` komutlarını çağırır.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import OrchestraConfig


@dataclass
class WorkResult:
    ok: bool
    text: str = ""
    structured: dict | None = None
    limited: bool = False  # abonelik/kota limiti doldu
    reset_at: float | None = None  # limitin sıfırlanacağı zaman (biliniyorsa)
    unavailable: bool = False  # kurulu değil, giriş yapılmamış, bağlantı yok
    minutes: float = 0.0
    cost_usd: float = 0.0
    error: str = ""


class Worker:
    name = "işçi"
    vendor = ""
    can_edit = True

    def run(self, prompt: str, cwd: Path, *, edit: bool, schema: dict | None = None,
            timeout_s: float = 1800) -> WorkResult:
        raise NotImplementedError


# -- limit ve JSON ayrıştırma yardımcıları --------------------------------------

_LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|hit your .{0,20}limit|limit (?:reached|exceeded)|quota|resource[ _]exhausted"
    r"|too many requests|\b429\b|kota|sınır",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(r"/login|not logged in|authenticat|invalid api key|sign in|oturum aç", re.IGNORECASE)


def looks_limited(text: str) -> bool:
    return bool(_LIMIT_RE.search(text or ""))


def parse_reset(text: str, now: float | None = None) -> float | None:
    """Limit mesajından sıfırlanma zamanını çıkarır (unix zamanı). Bulamazsa None."""
    now = time.time() if now is None else now
    text = text or ""
    if m := re.search(r"\|(\d{10})\b", text):  # "Claude AI usage limit reached|1760000000"
        return float(m.group(1))
    if m := re.search(r"(\d+)\s*h(?:ours?|rs?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?", text, re.IGNORECASE):
        if re.search(r"\bin\b|sonra|reset", text, re.IGNORECASE):
            return now + int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60
    if m := re.search(r"in\s+(\d+)\s*(?:minutes?|mins?|m)\b", text, re.IGNORECASE):
        return now + int(m.group(1)) * 60
    if m := re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.IGNORECASE):
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour < 24 and minute < 60:
            base = datetime.fromtimestamp(now)
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= base:
                target += timedelta(days=1)
            return target.timestamp()
    return None


def extract_json(text: str) -> dict | None:
    """Serbest metnin içindeki (tercihen son) JSON nesnesini bulur."""
    if not text:
        return None
    for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    # İç içe nesnelerde içtekini değil, en dıştakini al; birden çok varsa sonuncusunu
    decoder = json.JSONDecoder()
    found, i = None, 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            found = obj
        i = end
    return found


def _last_json_object(stdout: str) -> dict | None:
    stdout = (stdout or "").strip()
    try:
        obj = json.loads(stdout)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def resolve_command(command: list[str]) -> list[str] | None:
    if not command:
        return None
    exe = shutil.which(command[0])
    return [exe, *command[1:]] if exe else None


SCHEMA_HINT = (
    "\n\nCevabının EN SONUNDA, başka hiçbir şey eklemeden, şu JSON şemasına uyan tek bir JSON nesnesi ver:\n"
)


# -- komut satırı işçileri ------------------------------------------------------

class CLIWorker(Worker):
    def __init__(self, command: list[str]):
        self.command = command

    def _invoke(self, argv: list[str], cwd: Path, stdin: str | None, timeout_s: float):
        start = time.time()
        try:
            proc = subprocess.run(
                argv, cwd=cwd, input=stdin, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_s,
                stdin=None if stdin is not None else subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return None, f"{int(timeout_s / 60)} dakikada bitmedi, durduruldu", (time.time() - start) / 60
        except OSError as e:
            return None, f"çalıştırılamadı: {e}", 0.0
        return proc, "", (time.time() - start) / 60

    def _finish(self, ok: bool, text: str, stderr: str, minutes: float, cost: float,
                structured: dict | None) -> WorkResult:
        if ok:
            return WorkResult(True, text, structured, minutes=minutes, cost_usd=cost)
        message = (text or stderr or "bilinmeyen hata").strip()
        combined = f"{text}\n{stderr}"
        if re.search(r"unknown option|is invalid\. Allowed choices", stderr or ""):
            return WorkResult(False, text, unavailable=True, minutes=minutes,
                              error=f"komut satırı seçeneği reddedildi (CLI'yi güncelle): {stderr.strip()[:300]}")
        if looks_limited(combined):
            return WorkResult(False, text, limited=True, reset_at=parse_reset(combined),
                              minutes=minutes, cost_usd=cost, error=message[:500])
        if _AUTH_RE.search(combined):
            return WorkResult(False, text, unavailable=True, minutes=minutes,
                              error=f"giriş yapılmamış görünüyor: {message[:300]}")
        return WorkResult(False, text, minutes=minutes, cost_usd=cost, error=message[:1000])


class ClaudeCodeWorker(CLIWorker):
    name = "claude"
    vendor = "anthropic"

    READONLY_TOOLS = "Read,Grep,Glob,Bash(git diff *),Bash(git log *),Bash(git status *),Bash(git show *)"

    def __init__(self, cfg: OrchestraConfig):
        super().__init__(cfg.claude_command)
        self.cfg = cfg

    def build_argv(self, exe: list[str], edit: bool, schema: dict | None) -> list[str]:
        argv = [*exe, "-p", "--output-format", "json", "--permission-prompts", "none"]
        if edit:
            argv += ["--permission-mode", "acceptEdits"]
            allowed = ["Read", "Edit", "Write", "Grep", "Glob",
                       "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)"]
            if self.cfg.check_command:
                allowed.append(f"Bash({self.cfg.check_command}*)")
            argv += ["--allowedTools", ",".join(allowed)]
        else:
            argv += ["--permission-mode", "dontAsk", "--allowedTools", self.READONLY_TOOLS]
        if schema:
            argv += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        if self.cfg.claude_model:
            argv += ["--model", self.cfg.claude_model]
        return argv + list(self.cfg.claude_extra_args)

    def run(self, prompt, cwd, *, edit, schema=None, timeout_s=1800):
        exe = resolve_command(self.command)
        if exe is None:
            return WorkResult(False, unavailable=True, error=f"'{self.command[0]}' bulunamadı (Claude Code kurulu mu?)")
        # İstem stdin ile gider: Windows'ta komut satırı uzunluk sınırı ve
        # .cmd dosyalarının özel karakter sorunları olmaz.
        proc, err, minutes = self._invoke(self.build_argv(exe, edit, schema), cwd, prompt, timeout_s)
        if proc is None:
            return WorkResult(False, minutes=minutes, error=err)
        data = _last_json_object(proc.stdout)
        if data is None:
            return self._finish(False, proc.stdout, proc.stderr, minutes, 0.0, None)
        ok = proc.returncode == 0 and not data.get("is_error") and data.get("subtype", "success") == "success"
        return self._finish(ok, str(data.get("result") or ""), proc.stderr, minutes,
                            float(data.get("total_cost_usd") or 0.0), data.get("structured_output"))


class AntigravityWorker(CLIWorker):
    name = "antigravity"
    vendor = "google"

    def __init__(self, cfg: OrchestraConfig):
        super().__init__(cfg.antigravity_command)
        self.cfg = cfg
        self.can_edit = cfg.antigravity_can_edit

    def run(self, prompt, cwd, *, edit, schema=None, timeout_s=1800):
        exe = resolve_command(self.command)
        if exe is None:
            return WorkResult(False, unavailable=True,
                              error=f"'{self.command[0]}' bulunamadı (Antigravity CLI kurulu mu?)")
        if not edit:
            prompt += "\n\nÖNEMLİ: Bu iş salt-okunur. Hiçbir dosyayı değiştirme, oluşturma veya silme."
        if schema:
            prompt += SCHEMA_HINT + json.dumps(schema, ensure_ascii=False)
        argv = [*exe, "-p", prompt, "--output-format", "json"]
        if self.cfg.antigravity_model:
            argv += ["--model", self.cfg.antigravity_model]
        argv += list(self.cfg.antigravity_extra_args)
        proc, err, minutes = self._invoke(argv, cwd, None, timeout_s)
        if proc is None:
            return WorkResult(False, minutes=minutes, error=err)
        data = _last_json_object(proc.stdout)
        if data is None:
            return self._finish(False, proc.stdout, proc.stderr, minutes, 0.0, None)
        text = str(data.get("response") or data.get("result") or "")
        status = str(data.get("status", "SUCCESS")).upper()
        ok = proc.returncode == 0 and status == "SUCCESS"
        structured = data.get("structured_output") or (extract_json(text) if schema and ok else None)
        return self._finish(ok, text, proc.stderr, minutes, 0.0, structured)


class LocalWorker(Worker):
    """Ollama'daki yerel model; LocalAI'nin kendi ajan döngüsünü kullanır. Ücretsiz."""

    name = "yerel"
    vendor = "yerel"

    def __init__(self, cfg: OrchestraConfig, local_cfg: Any):
        self.cfg = cfg
        self.local_cfg = local_cfg

    def run(self, prompt, cwd, *, edit, schema=None, timeout_s=1800):
        from ..agent import run_task
        from ..prompts import build_system_prompt
        from ..providers.base import ProviderError
        from ..providers.ollama import OllamaProvider
        from ..tools import TOOL_SPECS, ToolBox

        check = self.cfg.check_command.strip()

        def approve(tool: str, summary: str) -> bool:
            # Worktree yalıtılmış ve sonradan denetleniyor; yine de komutları kısıtla
            if tool in {"write_file", "edit_file"}:
                return edit
            command = summary.removeprefix("Komut çalıştır: ").strip()
            allowed = ("git status", "git diff", "git log") + ((check,) if check and edit else ())
            return any(command.startswith(a) for a in allowed)

        if schema:
            prompt += SCHEMA_HINT + json.dumps(schema, ensure_ascii=False)
        provider = OllamaProvider(
            model=self.local_cfg.local_model,
            system_prompt=build_system_prompt(cwd, local=True),
            tools=TOOL_SPECS,
            host=self.local_cfg.ollama_host,
            num_ctx=self.local_cfg.num_ctx,
            timeout=timeout_s,
        )
        toolbox = ToolBox(cwd, approve, self.local_cfg.local_tool_output_chars)
        start = time.time()
        try:
            outcome = run_task(provider, toolbox, prompt, max_steps=self.local_cfg.max_steps)
        except ProviderError as e:
            return WorkResult(False, unavailable=True, error=str(e))
        minutes = (time.time() - start) / 60
        if not outcome.ok:
            return WorkResult(False, outcome.text, minutes=minutes, error=outcome.problem)
        structured = extract_json(outcome.text) if schema else None
        return WorkResult(True, outcome.text, structured, minutes=minutes)


def build_workers(cfg: OrchestraConfig, local_cfg: Any) -> dict[str, Worker]:
    workers: dict[str, Worker] = {}
    if cfg.claude_enabled:
        workers["claude"] = ClaudeCodeWorker(cfg)
    if cfg.antigravity_enabled:
        workers["antigravity"] = AntigravityWorker(cfg)
    if cfg.local_enabled:
        workers["yerel"] = LocalWorker(cfg, local_cfg)
    return workers
