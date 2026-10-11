"""فحص ذاتي بلا واجهة: pdf2md.exe --selftest (أو python main.py --selftest).

يتأكد أن النسخة المبنية (exe) تعمل فعلاً، لا أنها تفتح فقط: تحميل كل وحدات البرنامج، قراءة PDF فيه صفحة نصية
وصفحة مصوّرة عبر خط المعالجة الحقيقي، وPaddleOCR (إن كان مثبّتاً) ببايثونه المضمَّن. لا يتصل بالإنترنت ولا يمس
ملفات المستخدم: يعمل في مجلد مؤقت. النتيجة في logs\\selftest.txt داخل مجلد البيانات، ورمز الخروج 0 = نجح.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path


def _modules() -> list[str]:
    import app
    bad = []
    for m in pkgutil.walk_packages(app.__path__, "app."):
        if m.name.endswith(("paddle_worker", "selftest")):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:
            bad.append(f"{m.name}: {e}")
    return bad


def _sample_pdf(path: Path) -> Path:
    import pymupdf as fitz
    doc = fitz.open()
    p = doc.new_page()
    p.insert_htmlbox(fitz.Rect(50, 50, 545, 400),
                     '<div dir="rtl" style="font-size:16px">عقد إيجار بين الطرفين بمبلغ 750 دينار شهرياً</div>')
    img = doc.new_page()
    src = fitz.open()
    sp = src.new_page()
    sp.insert_htmlbox(fitz.Rect(50, 50, 545, 400),
                      '<div dir="rtl" style="font-size:20px">القضية رقم 153 لسنة 2024<br>المبلغ المطلوب 45000 دينار</div>')
    img.insert_image(img.rect, pixmap=sp.get_pixmap(dpi=200))
    doc.save(path)
    return path


def run() -> int:
    from .config import app_data_dir
    out = app_data_dir() / "logs" / "selftest.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"pdf2md selftest {time.strftime('%Y-%m-%d %H:%M:%S')}  frozen={bool(getattr(sys, 'frozen', False))}"]
    ok = True

    def check(name, fn):
        nonlocal ok
        t = time.monotonic()
        try:
            res = fn()
            lines.append(f"✓ {name} ({time.monotonic() - t:.1f}s){': ' + str(res) if res else ''}")
        except Exception as e:
            ok = False
            lines.append(f"✗ {name}: {e}\n{traceback.format_exc()}")

    def modules():
        bad = _modules()
        if bad:
            raise RuntimeError("; ".join(bad))

    check("تحميل وحدات البرنامج", modules)

    def pipeline():
        from . import paddle_engine as pe
        from .config import Settings
        from .hashing import HashCache
        from .pipeline import BatchContext, Job, run_batch
        tmp = Path(tempfile.mkdtemp(prefix="pdf2md-selftest-"))
        pdf = _sample_pdf(tmp / "selftest.pdf")
        cache = HashCache(tmp / "cache.json")
        s = Settings()
        s.scan_engine = "paddle"
        paddle = pe.PaddleWorker().start() if pe.is_available() else None
        ctx = BatchContext(tmp / "out", s, threading.Event(), lambda *a, **k: None, cache, None, paddle=paddle,
                           md_dir=tmp / "md")
        try:
            run_batch([Job(pdf, cache.get(pdf))], ctx)
        finally:
            if ctx.paddle is not None:
                ctx.paddle.close()
        md = (tmp / "md" / "selftest.md").read_text(encoding="utf-8")
        if "<!-- page:2 -->" not in md:
            raise RuntimeError("ملف Markdown ناقص:\n" + md[:400])
        if paddle is None:
            return "ملف Markdown كُتب؛ PaddleOCR غير مثبّت فالمصوّرة بانتظار المحرّك (متوقع)"
        missing = [w for w in ("إيجار", "750", "153", "2024", "45000") if w not in md]
        if missing:
            raise RuntimeError(f"لم يُقرأ: {missing}\n" + md[-400:])
        return "الصفحتان قُرئتا (الأولى قد تُعامل كمصوّرة إن شكّ كاشف الحروف المعكوسة في نصها)"

    check("قراءة PDF (نصية ومصوّرة)", pipeline)

    def paddle():
        from . import paddle_engine as pe
        if not pe.is_installed():
            return "غير مثبّت (تخطٍّ)"
        if pe.needs_python():
            raise RuntimeError("مثبّت بلا البايثون المضمَّن: اضغط «تحديث» في الإعدادات ← داخلي")
        r = pe.quick_test()
        if not r["ok"]:
            raise RuntimeError(f"لم يُقرأ: {r['missing']}")
        return f"اختبار سريع ناجح، المفسّر: {pe.worker_python()}"

    check("PaddleOCR", paddle)
    lines.append("النتيجة: " + ("نجح" if ok else "فشل"))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        sys.stdout.write("\n".join(lines) + "\n")
    except Exception:
        pass
    return 0 if ok else 1
