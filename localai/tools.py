"""Ajanın kullanabildiği araçlar: dosya okuma/yazma, arama, komut çalıştırma.

Tüm dosya yolları çalışma klasörüne (workspace) sabitlenir; model bu klasörün
dışına çıkamaz. Dosya yazan ve komut çalıştıran araçlar kullanıcı onayı ister.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .providers.base import ToolCall, ToolResult

# İçine girilmeyecek klasörler (aramayı ve listelemeyi hızlı ve temiz tutar)
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".idea", ".vscode",
}

# Onay fonksiyonu: (araç adı, kullanıcıya gösterilecek özet) -> izin verildi mi
ConfirmFn = Callable[[str, str], bool]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    needs_approval: bool = False


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="list_files",
        description="Bir klasördeki dosya ve klasörleri ağaç halinde listeler.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Çalışma klasörüne göre yol. Varsayılan: '.'"},
                "max_depth": {"type": "integer", "description": "En fazla kaç seviye inilecek. Varsayılan: 2"},
            },
            "required": [],
        },
    ),
    ToolSpec(
        name="read_file",
        description="Bir metin dosyasını satır numaralarıyla okur. Büyük dosyalarda start_line/end_line kullan.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "description": "1'den başlar"},
                "end_line": {"type": "integer"},
            },
            "required": ["path"],
        },
    ),
    ToolSpec(
        name="search",
        description="Dosya içeriklerinde regex araması yapar ve eşleşen satırları dosya:satır biçiminde döner.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex deseni"},
                "path": {"type": "string", "description": "Aranacak klasör. Varsayılan: '.'"},
                "glob": {"type": "string", "description": "Dosya adı filtresi, ör. '*.py'"},
            },
            "required": ["pattern"],
        },
    ),
    ToolSpec(
        name="write_file",
        description="Bir dosyayı verilen içerikle oluşturur veya tamamen değiştirir.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        needs_approval=True,
    ),
    ToolSpec(
        name="edit_file",
        description=(
            "Dosyada old_text metnini new_text ile değiştirir. old_text dosyada tam olarak "
            "bir kez geçmeli; gerekirse çevresindeki satırları da ekleyerek benzersiz yap."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
        needs_approval=True,
    ),
    ToolSpec(
        name="run_command",
        description="Çalışma klasöründe bir kabuk komutu çalıştırır (test, derleme, git vb.) ve çıktısını döner.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Saniye. Varsayılan: 120"},
            },
            "required": ["command"],
        },
        needs_approval=True,
    ),
]

SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


class ToolError(Exception):
    pass


_JSON_TYPES = {"string": str, "integer": int, "object": dict, "array": list, "boolean": bool}


def validate_args(spec: ToolSpec, args: Any) -> str | None:
    """Argümanları şemaya göre kontrol eder; sorun varsa açıklamasını döner.

    Model argümanları eksik veya yanlış tipte üretebilir (küçük yerel modeller
    sık yapar, akış sırasında kesilen çıktılar da). Böyle bir çağrı asla
    çalıştırılmaz; hata modele geri gönderilir ki düzeltip tekrar denesin.
    """
    if not isinstance(args, dict):
        return "argümanlar bir JSON nesnesi olmalı"
    props = spec.parameters.get("properties", {})
    for key in spec.parameters.get("required", []):
        if key not in args:
            return f"'{key}' alanı eksik"
    for key, value in args.items():
        if key not in props:
            return f"bilinmeyen alan '{key}'"
        expected = _JSON_TYPES.get(props[key].get("type", ""))
        # bool, int'in alt sınıfı; "integer" beklenen yerde True/False kabul etme
        if expected and (not isinstance(value, expected) or (expected is int and isinstance(value, bool))):
            return f"'{key}' alanı {props[key]['type']} olmalı"
    return None


class ToolBox:
    def __init__(self, workspace: Path, confirm: ConfirmFn, max_output_chars: int = 12_000):
        self.workspace = workspace.resolve()
        self.confirm = confirm
        self.max_output_chars = max_output_chars

    # -- genel giriş noktası -------------------------------------------------

    def execute(self, call: ToolCall) -> ToolResult:
        spec = SPECS_BY_NAME.get(call.name)
        if spec is None:
            return ToolResult(call.id, call.name, f"Bilinmeyen araç: {call.name}", is_error=True)
        if isinstance(call.arguments, dict):
            # Küçük modeller sayıları sık sık metin olarak yollar: "2" -> 2
            for key, prop in spec.parameters.get("properties", {}).items():
                value = call.arguments.get(key)
                if prop.get("type") == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    call.arguments[key] = int(value)
        problem = validate_args(spec, call.arguments)
        if problem:
            return ToolResult(call.id, call.name, f"Geçersiz argümanlar: {problem}", is_error=True)
        if spec.needs_approval and not self.confirm(call.name, self._describe(call)):
            return ToolResult(call.id, call.name, "Kullanıcı bu işlemi reddetti.", is_error=True)
        try:
            output = getattr(self, f"_{call.name}")(**call.arguments)
        except ToolError as e:
            return ToolResult(call.id, call.name, str(e), is_error=True)
        except TypeError as e:
            return ToolResult(call.id, call.name, f"Geçersiz argümanlar: {e}", is_error=True)
        except OSError as e:
            return ToolResult(call.id, call.name, f"Dosya sistemi hatası: {e}", is_error=True)
        return ToolResult(call.id, call.name, self._truncate(output))

    # -- yardımcılar ---------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        target = (self.workspace / path).resolve()
        if target != self.workspace and self.workspace not in target.parents:
            raise ToolError(f"'{path}' çalışma klasörünün dışında, erişim reddedildi.")
        return target

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix() or "."

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        half = self.max_output_chars // 2
        cut = len(text) - self.max_output_chars
        return f"{text[:half]}\n\n[... {cut} karakter kesildi ...]\n\n{text[-half:]}"

    def _describe(self, call: ToolCall) -> str:
        args = call.arguments
        if call.name == "run_command":
            return f"Komut çalıştır: {args.get('command')}"
        if call.name == "write_file":
            content = args.get("content", "")
            return f"Dosya yaz: {args.get('path')} ({len(content.splitlines())} satır)"
        if call.name == "edit_file":
            return (
                f"Dosya düzenle: {args.get('path')}\n"
                f"--- eski ---\n{args.get('old_text', '')}\n"
                f"--- yeni ---\n{args.get('new_text', '')}"
            )
        return f"{call.name} {args}"

    def _walk(self, root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                yield Path(dirpath) / name

    # -- araçlar -------------------------------------------------------------

    def _list_files(self, path: str = ".", max_depth: int = 2) -> str:
        root = self._resolve(path)
        if not root.is_dir():
            raise ToolError(f"Klasör bulunamadı: {path}")
        lines: list[str] = []

        def walk(directory: Path, depth: int) -> None:
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except PermissionError:
                return
            for entry in entries:
                if entry.name in SKIP_DIRS:
                    continue
                indent = "  " * depth
                if entry.is_dir():
                    lines.append(f"{indent}{entry.name}/")
                    if depth + 1 < max_depth:
                        walk(entry, depth + 1)
                else:
                    lines.append(f"{indent}{entry.name}")

        walk(root, 0)
        return "\n".join(lines) or "(boş klasör)"

    def _read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Dosya bulunamadı: {path}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"{path} bir metin dosyası değil (UTF-8 okunamadı).")
        lines = text.splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or len(lines))
        if not lines:
            return "(boş dosya)"
        width = len(str(end))
        body = "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))
        return f"{self._rel(target)} ({len(lines)} satır)\n{body}"

    def _search(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        root = self._resolve(path)
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Geçersiz regex: {e}")
        files = [root] if root.is_file() else self._walk(root)
        matches: list[str] = []
        for file in files:
            if glob and not fnmatch.fnmatch(file.name, glob):
                continue
            try:
                with file.open(encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        if regex.search(line):
                            matches.append(f"{self._rel(file)}:{lineno}: {line.rstrip()[:300]}")
                            if len(matches) >= 200:
                                matches.append("[... ilk 200 eşleşme gösterildi ...]")
                                return "\n".join(matches)
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(matches) or "Eşleşme bulunamadı."

    def _write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        verb = "güncellendi" if existed else "oluşturuldu"
        return f"{self._rel(target)} {verb} ({len(content.splitlines())} satır)."

    def _edit_file(self, path: str, old_text: str, new_text: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Dosya bulunamadı: {path}")
        text = target.read_text(encoding="utf-8")
        count = text.count(old_text) if old_text else 0
        if count == 0:
            raise ToolError("old_text dosyada bulunamadı. Önce read_file ile dosyanın güncel halini oku.")
        if count > 1:
            raise ToolError(f"old_text dosyada {count} kez geçiyor; çevre satırları ekleyerek benzersiz yap.")
        target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"{self._rel(target)} düzenlendi."

    def _run_command(self, command: str, timeout: int = 120) -> str:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"Komut {timeout} saniyede bitmedi ve durduruldu.")
        out = proc.stdout
        if proc.stderr:
            out += ("\n" if out else "") + "[stderr]\n" + proc.stderr
        return f"[çıkış kodu: {proc.returncode}]\n{out}".rstrip()
