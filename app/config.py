"""الإعدادات ومسارات بيانات البرنامج.

كل بيانات البرنامج (الإعدادات، ذاكرة البصمات، السجلات) في
%APPDATA%\\pdf2md — لا تحتاج صلاحيات admin. يمكن تغيير المسار بمتغير البيئة
PDF2MD_DATA_DIR (مفيد للاختبارات أو للتشغيل من فلاشة).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .fsutil import atomic_write_json, read_json

APP_NAME = "pdf2md"

log = logging.getLogger(__name__)


def app_data_dir() -> Path:
    override = os.environ.get("PDF2MD_DATA_DIR")
    if override:
        d = Path(override)
    else:
        base = os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir() -> Path:
    """ملفات العمل (حالة الصفحات للاستئناف، النص الخام، تقارير التصحيح والمراجعة، الفهرس) في مجلد
    مؤقت لا بجانب ملفاتك. بجانب الـ PDF يُكتب ملف الـ Markdown فقط. يمكن تغييره بـ PDF2MD_WORK_DIR."""
    import tempfile
    d = Path(os.environ.get("PDF2MD_WORK_DIR") or (Path(tempfile.gettempdir()) / APP_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d


WORK_KEEP_DAYS = 7


def cleanup_work_dir(days: int = WORK_KEEP_DAYS) -> int:
    """يحذف ملفات العمل التي لم تُلمس منذ days أيام (أسبوع افتراضياً) (الأثر الوحيد: إعادة معالجة الملف لو فُتح لاحقاً)."""
    import shutil
    import time
    root, limit, n = work_dir(), time.time() - days * 86400, 0
    for p in list(root.rglob("*")):
        try:
            if p.is_file() and p.stat().st_mtime < limit:
                p.unlink()
                n += 1
        except OSError:
            continue
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            next(d.iterdir())
        except StopIteration:
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
    return n


API_BATCH_SIZES = (1, 2, 3, 5)
ENGINE_MODES = ("auto", "local", "api")
SCAN_ENGINES = ("local", "paddle", "paddle_local")


@dataclass
class Settings:
    last_folder: str = ""
    output_dir: str = ""                 # مجلد ملفات الـ Markdown ("" = بجانب كل ملف PDF)
    # دقة رسم الصفحة المصوّرة قبل إرسالها للنموذج البصري.
    render_dpi: int = 200
    # عدد الصفحات في كل طلب للنموذج (1 أو 2) — يُستخدم من المرحلة 2.
    pages_per_call: int = 1
    # أقل عدد حروف مرئية حتى نعتبر الصفحة "نصية" ونستخرجها بدون ذكاء اصطناعي.
    text_min_chars: int = 30
    # عدد أسطر نهاية الصفحة السابقة المرسلة كسياق (للجداول الممتدة) — المرحلة 4.
    context_tail_lines: int = 8
    # المحرّك المحلي: معرّف النموذج من models_catalog ("" = بدون محرّك)
    local_model: str = ""
    use_local_engine: bool = False
    # طريقة قراءة الصفحات المصوّرة: "auto" (البرنامج يختار لكل صفحة: داخلي ← OCR ← ذكاء اصطناعي) أو "local" أو "api"
    engine_mode: str = ""
    # فحص الصفحات النصية "السليمة ظاهرياً" بـ OCR ومقارنة النتيجتين
    verify_text_with_ocr: bool = False
    # أقل نسبة تطابق مقبولة بين طبقة النص وOCR (0..1)
    verify_min_score: float = 0.7
    # ---- مزوّدات الـ API (المرحلة 3) — المفاتيح لا تُحفظ هنا أبداً (secrets_store)
    api_pages_per_call: int = 1          # 1 أو 2 أو 3 أو 5، ويُقيَّد بحد كل مزوّد
    api_dpi: int = 150
    api_max_dim: int = 2000              # أقصى طول/عرض للصورة بالبكسل
    api_jpeg_quality: int = 80
    api_for_scanned: bool = False        # الـ API بدل المحرّك المحلي للصفحات المصوّرة
    auto_send_weak: bool = False         # إرسال الصفحات الضعيفة تلقائياً
    # ---- المرحلة 4: الجداول ومؤشرات الصفحة الضعيفة والفهرس
    extract_tables: bool = True          # استخراج جداول طبقة النص كجداول Markdown
    table_rtl_columns: bool = True       # الجدول العربي: أول عمود في Markdown = العمود الأيمن في الـ PDF
    # درجة الصفحة 0..1؛ أقل من العتبة أو تجاوز أي مؤشر لحدّه = صفحة ضعيفة (قيم محافظة عمداً:
    # الإنذار الكاذب أهون من تفويت صفحة سيئة)
    weak_score_threshold: float = 0.75
    bracket_max_ratio: float = 0.01      # علامات [ ] لكل كلمة
    bracket_min_count: int = 2
    unknown_max_ratio: float = 0.30      # الكلمات العربية غير الموجودة في القاموس المحلي
    dict_min_words: int = 3000           # لا يُطبّق مؤشر القاموس قبل أن يبلغ هذا الحجم
    short_fraction: float = 0.15         # الناتج أقل من هذه النسبة مما تتوقعه كثافة الحبر
    auto_build_index: bool = False       # بناء INDEX.md تلقائياً بعد كل دفعة
    summary_via_api: bool = False        # التلخيص عبر مزوّد API (cloud_gate)
    # ---- توحيد الأرقام ومسار OCR + المصحّح ("عبر المواقع")
    describe_images: bool = True         # الصور داخل المستند: وصف (ونص المحادثات) عبر المواقع، وإلا [صورة]
    strip_page_furniture: bool = True    # حذف ذيل الصفحة المتكرر (العنوان) وأرقام الصفحات وبقايا التوقيع
    fast_corrector: bool = True          # المصحّح بنسخة Gemini أخف وبلا "تفكير" (أسرع بكثير)
    ocr_parallel: int = 2                # دفعات من 3 صفحات تُعالج بالتوازي في سلسلة OCR
    normalize_digits: bool = True        # تحويل ٠-٩ و۰-۹ إلى 0-9 في كل Markdown (ملفات raw لا تُحوَّل)
    ocr_engine: str = "auto"             # "auto" أو "ocrspace" أو "vision" (نموذج الرؤية مباشرة)
    ocrspace_engine: int = 3             # محرّك OCR.space الأساسي: 3 (الافتراضي) أو 2؛ والآخر احتياطي. 1 لا يقبل العربية
    corrector_enabled: bool = True       # تصحيح نص OCR بنموذج نصي
    send_hard_images: bool = True        # الصفحات الضعيفة: صورة + نص لنموذج رؤية
    # ---- PaddleOCR (تجريبي، app/paddle_engine.py) — لا أثر لهما إن لم يكن مثبّتاً
    scan_engine: str = "local"           # الصفحات المصوّرة: "local" | "paddle" | "paddle_local" (Paddle ثم المحلي للضعيفة)
    paddle_min_conf: float = 0.8         # أقل ثقة مقبولة لسطر Paddle
    # ---- الواجهة
    cloud_notice_dismissed: bool = False # لا تعرض تنبيه "عبر المواقع" مرة أخرى
    force_ocr_all: bool = False          # فرض OCR على كل الملفات (إضافة لفرضه لملف بعينه)
    include_subfolders: bool = False     # البحث عن PDF في المجلدات الفرعية أيضاً
    theme: str = "system"                # "system" أو "light" أو "dark"
    palette: str = ""                    # ثيم الألوان من ui/themes.py ("" = الأصلي حتى يختار المستخدم)
    extra_files: list = dataclasses.field(default_factory=list)  # ملفات أُضيفت من خارج المجلد

    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = read_json(cls.path(), default={}) or {}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("تعذّرت قراءة الإعدادات، سيتم استخدام الافتراضي: %s", e)
            raw = {}
        s = cls()
        for f in dataclasses.fields(cls):
            if f.name in raw and isinstance(raw[f.name], type(getattr(s, f.name))):
                setattr(s, f.name, raw[f.name])
        s.render_dpi = min(max(s.render_dpi, 72), 400)
        s.pages_per_call = min(max(s.pages_per_call, 1), 2)
        s.text_min_chars = max(s.text_min_chars, 1)
        if s.theme not in ("system", "light", "dark"):
            s.theme = "system"
        if s.palette not in ("", "classic", "teal", "blue", "indigo", "studio"):
            s.palette = ""
        s.extra_files = [str(x) for x in s.extra_files if isinstance(x, str)]
        if s.engine_mode not in ENGINE_MODES:
            s.engine_mode = "local"
        if s.api_pages_per_call not in API_BATCH_SIZES:
            s.api_pages_per_call = 1
        if s.ocr_engine not in ("auto", "ocrspace", "vision"):
            s.ocr_engine = "auto"
        if s.ocrspace_engine not in (2, 3):
            s.ocrspace_engine = 3
        if s.scan_engine not in SCAN_ENGINES:
            s.scan_engine = "local"
        v = raw.get("paddle_min_conf")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s.paddle_min_conf = min(max(float(v), 0.0), 1.0)
        s.api_dpi = min(max(s.api_dpi, 72), 300)
        s.api_max_dim = min(max(s.api_max_dim, 800), 4000)
        s.api_jpeg_quality = min(max(s.api_jpeg_quality, 40), 95)
        for name, lo, hi in (("weak_score_threshold", 0.0, 1.0), ("bracket_max_ratio", 0.0, 1.0),
                             ("unknown_max_ratio", 0.0, 1.0), ("short_fraction", 0.0, 1.0)):
            v = raw.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                setattr(s, name, min(max(float(v), lo), hi))
        return s

    def save(self) -> None:
        atomic_write_json(self.path(), dataclasses.asdict(self))


def setup_logging() -> Path:
    """سجل تشغيل محلي. لا يُكتب فيه محتوى المستندات، فقط أسماء الملفات وأرقام الصفحات."""
    log_dir = app_data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "app.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    from .secrets_store import install_log_redaction
    install_log_redaction()
    return log_file
