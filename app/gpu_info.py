"""كشف كروت الشاشة في ويندوز ليعرف المستخدم هل يفيده تشغيل النماذج على الكرت.

Win32_VideoController.AdapterRAM رقم 32-بت: يُقصّ عند 4GB، فالذاكرة المعروضة لكروت أكبر منها تقريبية ("≥4").
لا يتحقق من دعم Vulkan فعلياً؛ التحقق الحقيقي هو تشغيل المحرّك، وعند فشله يعود البرنامج للمعالج تلقائياً.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

_DISCRETE = re.compile(r"nvidia|geforce|rtx|gtx|quadro|radeon (rx|pro)|\barc\b", re.I)
_VIRTUAL = re.compile(r"microsoft basic|remote|virtual|vmware|parsec|displaylink", re.I)


@dataclass(frozen=True)
class Gpu:
    name: str
    vram_gb: float
    discrete: bool


def parse(raw: str) -> list[Gpu]:
    """ناتج ConvertTo-Json لـ Name وAdapterRAM (كائن واحد أو قائمة)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data if isinstance(data, list) else []:
        name = str(d.get("Name") or "").strip()
        if not name or _VIRTUAL.search(name):
            continue
        ram = d.get("AdapterRAM")
        out.append(Gpu(name, round(ram / 2**30, 1) if isinstance(ram, (int, float)) and ram > 0 else 0.0,
                       bool(_DISCRETE.search(name))))
    return out


_cache: list[Gpu] | None = None


def detect() -> list[Gpu]:
    """يُكتشف مرة واحدة في الجلسة (استدعاء PowerShell ~1 ث) والواجهة تُعاد رسمها كثيراً."""
    global _cache
    if _cache is None:
        _cache = _detect()
    return _cache


def _detect() -> list[Gpu]:
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    return parse(r.stdout) if r.returncode == 0 else []


def advice(gpus: list[Gpu]) -> str:
    """جملة للمستخدم: هل الكرت يستحق؟"""
    if not gpus:
        return "لم يُعثر على كرت شاشة."
    best = max(gpus, key=lambda g: (g.discrete, g.vram_gb))
    mem = f"{best.vram_gb:g}GB" if best.vram_gb else "؟"
    if best.discrete:
        return f"{best.name} ({mem}) — كرت مستقل، يُتوقع تسريع كبير."
    return f"{best.name} ({mem}) — كرت مدمج، لا يُتوقع أن يسرّع (قد يبطّئ)."
