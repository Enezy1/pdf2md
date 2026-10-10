"""تصدير معلومات التشخيص: ملف zip فيه سجل التشغيل والإعدادات وحالة المزوّدات والنماذج.

لا يحتوي مفاتيح (secrets_store منفصل، والنص يمر بـ redact) ولا محتوى مستندات
(السجلات لا تكتب إلا أسماء الملفات وأرقام الصفحات).
"""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import app_data_dir
from .secrets_store import redact

FILES = ("settings.json", "providers_state.json", "usage.json", "logs/app.log", "lab/results.jsonl")
MAX_LOG = 2_000_000


def export(target: Path) -> Path:
    data = app_data_dir()
    target = Path(target)
    info = {
        "version": __version__, "time": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version, "platform": platform.platform(), "frozen": bool(getattr(sys, "frozen", False)),
    }
    try:
        from . import model_manager as mm
        from . import models_catalog as cat
        info["llama_server"] = bool(mm.llama_exe())
        info["models_installed"] = [m.id for m in cat.MODELS if mm.is_installed(m)]
    except Exception as e:
        info["models_error"] = str(e)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=2))
        for rel in FILES:
            p = data / rel
            if not p.is_file():
                continue
            raw = p.read_bytes()[-MAX_LOG:].decode("utf-8", errors="replace")
            z.writestr(rel, redact(raw))
    return target
