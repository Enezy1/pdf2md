"""فحص سريع لبيئة ماك: الخطوط والأيقونات والمفاتيح ومسار البيانات وخط المعالجة.

شغّله على جهاز ماك قبل بناء الحزمة أو بعد تنزيلها:

    python3 tools/mac_check.py

يطبع تقريراً واضحاً: ✔ ما يعمل، ✘ ما لا يعمل.
يعمل على ويندوز أيضاً (الفحص نفس المنطق) — مفيد قبل شدّ أي تعديل.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK = "✔"
BAD = "✘"


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def check_env() -> None:
    _section("البيئة")
    print(f"{OK} النظام: {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"{OK} بايثون: {sys.version.split()[0]}  ({sys.executable})")
    print(f"{OK} منفذ: {sys.platform}")


def check_imports() -> None:
    _section("الوحدات")
    for mod in ("fitz", "PIL", "numpy", "httpx", "google.genai", "keyring", "customtkinter", "sv_ttk", "darkdetect"):
        try:
            __import__(mod)
            print(f"{OK} {mod}")
        except Exception as e:
            print(f"{BAD} {mod}: {type(e).__name__}: {e}")


def check_data_dir() -> None:
    _section("مسار البيانات")
    from app.config import app_data_dir
    d = app_data_dir()
    print(f"{OK} {d}")
    if str(d).startswith(str(Path.home() / "pdf2md")) and platform.system() != "Windows":
        print(f"{BAD} المسار ليس في Location Application Support — تحقّق من app/config.py")
    print(f"{OK} ملفات العمل: {Path(tempfile.gettempdir()) / 'pdf2md'}")


def check_fonts() -> None:
    _section("الخطوط والأيقونات")
    import tkinter as tk
    import tkinter.font as tkfont
    from app.ui import ctk_ui, theme

    root = tk.Tk()
    root.withdraw()
    try:
        picked = theme.resolve_font(root)
        print(f"{OK} خط النص: {picked}")
        ctk_ui._fam.clear()
        fam = ctk_ui.icon_family()
        print(f"{OK} خط الأيقونات: {fam}")
        have = set(tkfont.families())
        for name in ("Geeza Pro", "Apple Symbols", "SF Arabic", "Helvetica Neue"):
            print(f"    {'✔' if name in have else '✘'} مثبّت: {name}")

        missing = []
        for key, glyph in ctk_ui.ICON.items():
            if not ctk_ui._draws(fam, glyph):
                missing.append(key)
        if missing:
            print(f"{BAD} أيقونات لا يرسمها النظام: {', '.join(missing)}")
        else:
            print(f"{OK} كل الأيقونات ({len(ctk_ui.ICON)}) تُرسم")
        for key, glyph in sorted(ctk_ui.ICON.items()):
            print(f"    {key:<9} U+{ord(glyph):04X}")
    finally:
        root.destroy()


def check_secrets() -> None:
    _section("حفظ المفاتيح")
    from app.secrets_store import _default_backend
    backend = _default_backend()
    print(f"{OK} الخلفية: {type(backend).__name__}")
    if platform.system() == "Darwin" and type(backend).__name__ != "_KeyringBackend":
        print(f"{BAD} Keychain غير مُستخدم — سيُحفظ المفتاح في ملف مشفّر بدل Keychain")


def check_pipeline() -> None:
    _section("خط المعالجة (نص PDF → Markdown)")
    try:
        import fitz
        from app.pdf_io import page_text_to_markdown
    except Exception as e:
        print(f"{BAD} تعذّر الاستيراد: {e}")
        return
    tmp = Path(tempfile.mkdtemp())
    pdf = tmp / "t.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Hello PDF2MD", fontsize=20)
    doc.save(str(pdf))
    doc.close()
    doc = fitz.open(str(pdf))
    md = page_text_to_markdown(doc[0])
    doc.close()
    if "Hello" in md:
        print(f"{OK} استخراج النص يعمل ({len(md)} حرف)")
    else:
        print(f"{BAD} الناتج لا يحتوي على النص: {md[:120]!r}")


def main() -> int:
    print("فحص pdf2md —", ROOT)
    check_env()
    check_imports()
    check_data_dir()
    check_fonts()
    check_secrets()
    check_pipeline()
    print("\nانتهى الفحص.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
