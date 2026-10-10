"""Sistem talimatları.

Talimat oturum boyunca sabit kalır (tarih/saat gibi değişen bilgi yok); böylece
Claude tarafında prompt önbelleği her adımda isabet eder.
"""

from __future__ import annotations

import platform
from pathlib import Path

RULES_FILE = "LOCALAI.md"
MAX_RULES_CHARS = 8000

BASE = """\
Sen kullanıcının bilgisayarında çalışan bir yazılım geliştirme asistanısın.
Çalışma klasörü: {workspace}
İşletim sistemi: {os}

Araçlarla dosyaları okuyabilir, arayabilir, düzenleyebilir ve komut çalıştırabilirsin.
Nasıl çalışırsın:
- Bir dosyayı değiştirmeden önce mutlaka read_file ile güncel halini oku.
- Küçük değişiklikler için edit_file, yeni dosyalar için write_file kullan.
- Değişiklikten sonra mümkünse testleri veya programı run_command ile çalıştırıp sonucu kontrol et.
- Bir araç hata dönerse hatayı oku ve yaklaşımını düzelt; aynı çağrıyı aynen tekrarlama.
- İş bitince ne yaptığını kısaca özetle. Kullanıcıyla Türkçe konuş.
"""

LOCAL_EXTRA = """
Önemli: Bir araç kullanacaksan araç çağrısı yap; kodu sadece cevap metnine yazıp bırakma.
Bağlam penceren küçük: büyük dosyaları start_line/end_line ile parça parça oku.
"""


def load_rules(workspace: Path) -> str:
    path = workspace / RULES_FILE
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    if len(text) > MAX_RULES_CHARS:
        text = text[:MAX_RULES_CHARS] + "\n[... kesildi ...]"
    return f"\nProjeye özel kurallar ({RULES_FILE}):\n{text}\n"


def build_system_prompt(workspace: Path, local: bool) -> str:
    prompt = BASE.format(workspace=workspace, os=f"{platform.system()} {platform.release()}")
    if local:
        prompt += LOCAL_EXTRA
    return prompt + load_rules(workspace)
