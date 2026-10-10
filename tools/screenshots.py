"""لقطات شاشة للواجهة (الرئيسية + تبويبات الإعدادات) في الوضعين الفاتح والداكن.

    python tools/screenshots.py [مجلد الإخراج] [ثيم الألوان: teal | blue | indigo]

يعمل ببيانات مؤقتة معزولة (PDF2MD_DATA_DIR) فلا يلمس إعداداتك ولا مفاتيحك.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PDF2MD_DATA_DIR"] = tempfile.mkdtemp(prefix="pdf2md-shots-")


class _NoSecrets:
    """بدون مدير بيانات الاعتماد الحقيقي."""

    def has_key(self, pid):
        return False

    def get_key(self, pid):
        return None


def grab(win, path: Path) -> None:
    """PrintWindow: يرسم هذه النافذة وحدها إلى صورة حتى لو غطّتها نافذة أخرى —
    فلا يمكن أن يظهر في اللقطة شيء من خارج البرنامج."""
    import ctypes.wintypes as wt
    from PIL import Image
    for _ in range(8):
        win.update()
        win.after(60)
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = u32.GetParent(win.winfo_id())
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u32.GetWindowDC(hwnd)
    mdc = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mdc, bmp)
    PW_RENDERFULLCONTENT = 2
    if not u32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
        raise RuntimeError("PrintWindow failed")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    g32.DeleteObject(bmp)
    g32.DeleteDC(mdc)
    u32.ReleaseDC(hwnd, hdc)
    # إطار ويندوز 11 الشفاف (حواف الظل) خارج حدود النافذة الفعلية
    f = wt.RECT()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(f), ctypes.sizeof(f))
    img.crop((f.left - r.left, f.top - r.top, f.right - r.left, f.bottom - r.top)).save(path)


def main() -> None:
    import ctypes.wintypes  # noqa: F401
    import tkinter as tk

    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    pal = sys.argv[2] if len(sys.argv) > 2 else ""
    tag = f"{pal}-" if pal else ""
    folder = Path(tempfile.mkdtemp(prefix="pdf2md-shots-pdfs-"))
    for p in (ROOT / "samples").glob("*.pdf"):
        shutil.copy(p, folder / p.name)
    shutil.copy(ROOT / "samples" / "sample_mixed.pdf", folder / "عقد إيجار 2024.pdf")
    shutil.copy(ROOT / "samples" / "sample_tables.pdf", folder / "كشف حساب.pdf")

    from app.config import Settings
    from app.ui import main_window as mw

    for mode in ("light", "dark"):
        s = Settings.load()
        s.theme, s.last_folder, s.palette = mode, str(folder), pal
        s.save()
        root = tk.Tk()
        app = mw.MainWindow(root)
        app.secrets = _NoSecrets()
        app._refresh_mode()
        root.geometry("+40+40")
        for _ in range(30):
            root.update()
            root.after(50)
        grab(root, out / f"{tag}main-{mode}.png")

        # بعد الانتهاء (محاكاة): تقدّم كامل، "تم"، سطر المراجعة، والتفاصيل
        app.bar.configure(value=1000)
        app.status_var.set("")
        from app import review_report
        orig = review_report.pending_count
        review_report.pending_count = lambda _o: 3
        app._show_done(True)
        review_report.pending_count = orig
        app.log("✓ عقد إيجار 2024.pdf — نص: 3، غير مقروء: 1")
        grab(root, out / f"{tag}main-done-{mode}.png")

        app.open_settings("providers")
        sw = app.settings_win
        sw.win.geometry("+80+60")
        for tab in ("providers", "local", "advanced"):
            sw.show(tab)
            grab(sw.win, out / f"{tag}settings-{tab}-{mode}.png")
        sw.shutdown()
        root.destroy()
    print(out)


if __name__ == "__main__":
    main()
