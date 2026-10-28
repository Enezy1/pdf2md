"""حفظ التقدّم بعد كل صفحة، والاستئناف من آخر صفحة.

لكل ملف (حسب بصمته) داخل <output>/.state/:
  <hash>.json        حالة كل صفحة
  <hash>/p0001.md    ناتج كل صفحة
الكتابة ذرّية: الجزء يُكتب أولاً ثم الحالة، فإذا انقطع البرنامج بينهما تُعاد الصفحة فقط.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import atomic_write_json, atomic_write_text, read_json

_WRITE_LOCK = threading.RLock()   # مساران يكتبان صفحات نفس الملف معاً (نماذج Gemini المتوازية) لا يتداخلان

log = logging.getLogger(__name__)

# حالات الصفحة
DONE = "done"                      # استُخرجت (طبقة نص أو نموذج)
BLANK = "blank"                    # فارغة
PENDING_ENGINE = "pending_engine"  # مصوّرة ولا يوجد محرّك متاح بعد
ERROR = "error"                    # فشلت — تُعاد في التشغيل القادم
NEEDS_REVIEW = "needs_review"      # ضعيفة أو ناتج المزوّد ناقص — بانتظار قرار المستخدم
ILLEGIBLE = "illegible"            # قرر المستخدم تركها [غير مقروء]
UNRELIABLE = "unreliable"          # ناتج المحرّك المحلي حلقة تكرار — لا يُكتب نصه


FINAL_STATUSES = {DONE, BLANK, ILLEGIBLE}
STATE_VERSION = 1


class FileState:
    def __init__(self, state_dir: Path, file_hash: str, source_name: str, page_count: int):
        self.state_dir = Path(state_dir)
        self.file_hash = file_hash
        self.json_path = self.state_dir / f"{file_hash}.json"
        self.pages_dir = self.state_dir / file_hash
        data = None
        try:
            data = read_json(self.json_path)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("حالة تالفة للملف %s، سيبدأ من جديد: %s", source_name, e)
        if not data or data.get("version") != STATE_VERSION or data.get("page_count") != page_count:
            data = {
                "version": STATE_VERSION,
                "file_hash": file_hash,
                "source_name": source_name,
                "page_count": page_count,
                "created": _now(),
                "pages": {},
            }
        data["source_name"] = source_name
        self.data = data

    @property
    def page_count(self) -> int:
        return self.data["page_count"]

    def page(self, n: int) -> dict | None:
        return self.data["pages"].get(str(n))

    def status(self, n: int) -> str | None:
        p = self.page(n)
        return p["status"] if p else None

    def fragment_path(self, n: int) -> Path:
        return self.pages_dir / f"p{n:04d}.md"

    def read_fragment(self, n: int) -> str:
        try:
            return self.fragment_path(n).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def local_path(self, n: int) -> Path:
        """ناتج المحرّك المحلي لصفحة ضعيفة (للاطلاع فقط، لا يدخل الـ Markdown)."""
        return self.pages_dir / f"p{n:04d}.local.md"

    def carry_meta(self, n: int) -> dict:
        """بيانات الصفحة التي تبقى عند إعادة معالجتها (التصنيف وسبب التحويل)."""
        p = self.page(n) or {}
        return {k: v for k, v in p.items() if k in ("info", "qv", "converted_reason", "clip", "photos")}

    def update_meta(self, n: int, **extra) -> None:
        with _WRITE_LOCK:
            p = self.data["pages"].get(str(n))
            if p is None:
                return
            p.update(extra)
            self.data["updated"] = _now()
            atomic_write_json(self.json_path, self.data)

    def flush(self) -> None:
        """حفظ تعديلات مباشرة على data (مثل درجات الجودة لعدة صفحات دفعة واحدة)."""
        with _WRITE_LOCK:
            self.data["updated"] = _now()
            atomic_write_json(self.json_path, self.data)

    def providers_summary(self) -> dict[str, list[int]]:
        """{المزوّد: [الصفحات]} للصفحات التي عالجها مزوّد خارجي."""
        out: dict[str, list[int]] = {}
        for k, p in self.data["pages"].items():
            m = p.get("method", "")
            if p.get("status") == DONE and m.startswith("api:"):
                out.setdefault(m[4:], []).append(int(k))
        return {k: sorted(v) for k, v in sorted(out.items())}

    def engines_summary(self) -> dict[str, list[int]]:
        """{محرّك OCR المحلي: [الصفحات]} — "paddle" أو معرّف النموذج المحلي."""
        out: dict[str, list[int]] = {}
        for k, p in self.data["pages"].items():
            m = p.get("method", "")
            if p.get("status") == DONE and m.startswith("ocr:"):
                out.setdefault(m[4:], []).append(int(k))
        return {k: sorted(v) for k, v in sorted(out.items())}

    def paddle_path(self, n: int) -> Path:
        """النص الخام من PaddleOCR مع ثقة كل سطر (مرجع فقط)."""
        return self.pages_dir / f"p{n:04d}.paddle.md"

    def strip_path(self, n: int, k: int) -> Path:
        """صورة السطر k (من 0) من أسطر الأرقام في نص Paddle للصفحة n (تُرسل وحدها عند اختلاف القراءتين)."""
        return self.pages_dir / f"p{n:04d}.s{k:03d}.png"

    def save_page(self, n: int, status: str, method: str, markdown: str = "", **extra) -> None:
        with _WRITE_LOCK:
            if markdown:
                atomic_write_text(self.fragment_path(n), markdown)
            else:
                self.fragment_path(n).unlink(missing_ok=True)
            self.data["pages"][str(n)] = {"status": status, "method": method, "updated": _now(), **extra}
            self.data["updated"] = _now()
            atomic_write_json(self.json_path, self.data)

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for n in range(1, self.page_count + 1):
            s = self.status(n) or "todo"
            c[s] = c.get(s, 0) + 1
        return c

    def converted_counts(self) -> dict[str, int]:
        """عدد الصفحات المحوّلة من نصية إلى مصوّرة، حسب رمز السبب."""
        c: dict[str, int] = {}
        for p in self.data["pages"].values():
            code = p.get("converted_reason")
            if code:
                c[code] = c.get(code, 0) + 1
        return c


def progress(state_dir: Path, file_hash: str) -> tuple[int, int] | None:
    """(صفحات منتهية، المطلوبة) لملف عولج جزئياً سابقاً، أو None إن لم يبدأ. قراءة فقط."""
    try:
        data = read_json(Path(state_dir) / f"{file_hash}.json")
    except (json.JSONDecodeError, OSError):
        return None
    if not data or data.get("version") != STATE_VERSION:
        return None
    want = data.get("only_pages") or range(1, data.get("page_count", 0) + 1)
    done = sum(1 for n in want if (data["pages"].get(str(n)) or {}).get("status") in FINAL_STATUSES)
    return (done, len(want)) if done else None


def reset_file(state_dir: Path, file_hash: str) -> bool:
    """يحذف حالة ملف واحد (ملفات العمل فقط: التقدّم والنص الخام والأوصاف) لتُعاد معالجته من الصفر.
    ملف الـ PDF وملف الـ Markdown لا يُلمسان."""
    import shutil
    found = False
    j = Path(state_dir) / f"{file_hash}.json"
    if j.exists():
        j.unlink()
        found = True
    d = Path(state_dir) / file_hash
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        found = True
    return found


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
