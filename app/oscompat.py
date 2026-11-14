"""تكيّف البرنامج مع نظام التشغيل: فتح الملفات، ذاكرة الجهاز، الكرت، وسرّ الجهاز.

كل ما هنا يخدم ويندوز وماك ولينكس بلا فروع نظام موزّعة على بقية الكود.
القاعدة: الباقي من البرنامج لا يعرف نظام التشغيل، يسأل هذا الملف فقط.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import uuid
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------- فتح الملفات
def open_path(target: str | Path) -> bool:
    """يفتح ملفاً أو مجلداً بالبرنامج الافتراضي في النظام. يعيد True إن أُرسل الطلب."""
    p = Path(target)
    if not p.exists():
        return False
    if IS_WINDOWS:
        try:
            os.startfile(str(p))  # noqa: S606 — الغرض من الدالة فتح الملف بالمعاينة
            return True
        except OSError:
            return False
    cmd = ["open", str(p)] if IS_MAC else ["xdg-open", str(p)]
    try:
        # العملية المستقلة: فتح الملف لا يجب أن يوقف البرنامج، ولا أن تُغلق بتركه.
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- ذاكرة الجهاز
def memory_gb() -> float | None:
    """ذاكرة الجهاز بالـ GB، أو None إن تعذّرت معرفتها."""
    if IS_WINDOWS:
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(m)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.ullTotalPhys / 1024 ** 3
        except Exception:
            return None

    if IS_MAC:
        try:
            r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                               timeout=8, encoding="utf-8", errors="replace")
            if r.returncode == 0 and r.stdout.strip().isdigit():
                return int(r.stdout.strip()) / 1024 ** 3
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    try:  # لينكس: MemTotal من /proc/meminfo
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 ** 2
    except (OSError, ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------- الكرت
def gpu_rows() -> list[dict]:
    """قائمة كروت الشاشة: [{"name": str, "vram_gb": float}] كما يقرؤها نظام التشغيل."""
    if IS_MAC:
        return _gpu_macos()
    if IS_WINDOWS:
        return _gpu_windows()
    return []


def _gpu_windows() -> list[dict]:
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, encoding="utf-8",
                           errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    import json
    try:
        data = json.loads(r.stdout)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]
    rows = []
    for d in data if isinstance(data, list) else []:
        name = str(d.get("Name") or "").strip()
        ram = d.get("AdapterRAM")
        if not name:
            continue
        rows.append({"name": name, "vram_gb": float(ram) / 2 ** 30
                     if isinstance(ram, (int, float)) and ram > 0 else 0.0})
    return rows


def _gpu_macos() -> list[dict]:
    """macOS: system_profiler يعطي JSON فيه اسم الكرت والذاكرة (مشتركة أو مستقلة)."""
    try:
        r = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"],
                           capture_output=True, text=True, timeout=25, encoding="utf-8",
                           errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    import json
    try:
        data = json.loads(r.stdout)
    except (ValueError, TypeError):
        return []
    items = []
    for entry in data.get("SPDisplaysDataType", []) or []:
        name = str(entry.get("sppci_model") or entry.get("_name") or "").strip()
        if not name:
            continue
        vram = entry.get("spdisplays_vram") or entry.get("spdisplays_vram_shared") or 0
        try:
            vram_gb = round(int(str(vram).replace(",", "")) / 2 ** 30, 1)
        except (TypeError, ValueError):
            vram_gb = 0.0
        items.append({"name": name, "vram_gb": vram_gb})
    return items


# ---------------------------------------------------------------- سرّ الجهاز
def device_secret() -> bytes:
    """مادة سرية مشتقّة من الجهاز: نفس الجهاز يعطي نفس المفتاح، وجهاز آخر يعطي مفتاحاً آخر.

    تُستخدم للاحتياطي المحلي حين لا تتوفّر ذاكرة مفاتيح النظام (Keychain على ماك،
    Credential Manager على ويندوز). ليست حماية معقّدة كـ DPAPI، لكنّها تُبقي الملف
    غير مقروء على جهاز آخر.
    """
    import hashlib
    node = platform.node() or ""
    machine = platform.machine() or ""
    try:
        user = os.getlogin()
    except OSError:
        user = ""
    material = "|".join([platform.system(), node, machine, user, str(uuid.getnode())])
    return hashlib.sha256(material.encode("utf-8", "replace")).digest()
