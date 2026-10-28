"""معالجة الدفعة: ملفاً بعد ملف وصفحة بعد صفحة، مع حفظ التقدّم وزر إيقاف.

تعمل في خيط خلفي وترسل الأحداث للواجهة عبر الدالة emit (لا تلمس الواجهة مباشرة).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from . import checkpoint as cp
from . import ocr_router as router
from .crosscheck import CHECK_VERSION, match_score
from .config import Settings
from .hashing import HashCache
from .fsutil import atomic_write_text
from .markdown_out import write_markdown
from .quality import OCR_CHECK_VERSION, QUALITY_VERSION, REASON_CODE, REASON_TEXT, repetition_problem, weak_ocr_reason
from .pdf_io import FORCED_OCR_CODE, FORCED_OCR_TEXT, PageKind, classify_page, page_text_to_markdown, render_page_png
from .providers.base import EngineUnavailable, OcrRequest, PageImage, Provider

log = logging.getLogger(__name__)

Emit = Callable[..., None]


@dataclass
class Job:
    path: Path
    file_hash: str
    force_ocr: bool = False  # تجاهل طبقة النص ومعاملة كل الصفحات كمصوّرة
    api_ok: bool = False     # وضع "عبر المواقع" ويوجد مزوّد مفعّل (يُحدَّد في run_batch؛ البوابة تتحقق مجدداً)
    pages: list[int] | None = None   # قراءة صفحات محددة فقط (None = كل الصفحات)
    chat: bool = False               # المستند لقطات محادثات: الصفحات لنموذج الرؤية بقواعد المحادثة

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class BatchContext:
    output_dir: Path
    settings: Settings
    stop_event: threading.Event
    emit: Emit
    hash_cache: HashCache
    provider: Provider | None = None
    api: object | None = None  # dispatcher.ApiRunner عند وجود مزوّد خارجي مفعّل
    dictionary: object | None = None  # dictionary.ArabicDictionary (يُحمَّل عند أول حاجة)
    pause_event: threading.Event = field(default_factory=threading.Event)  # "إيقاف مؤقت"
    paddle: object | None = None  # paddle_engine.PaddleWorker عند اختيار PaddleOCR وتثبيته
    beside_source: bool = False       # ملف الـ Markdown بجانب ملف الـ PDF (الافتراضي في البرنامج)
    md_dir: Path | None = None        # أو مجلد يختاره المستخدم

    def md_dir_for(self, src: Path | None) -> Path:
        if self.beside_source and src is not None:
            return Path(src).parent
        return self.md_dir or self.docs_dir

    def get_dictionary(self):
        if self.dictionary is None:
            from .dictionary import ArabicDictionary
            self.dictionary = ArabicDictionary()
        return self.dictionary

    @property
    def state_dir(self) -> Path:
        return self.output_dir / ".state"

    @property
    def docs_dir(self) -> Path:
        return self.output_dir / "docs"


class FileChangedError(Exception):
    pass


def _api_scanned(job: Job, ctx: BatchContext) -> bool:
    """الصفحات المصوّرة لهذا الملف تذهب للـ API بدل المحرّك المحلي."""
    return job.api_ok and ctx.settings.api_for_scanned


def _needs_work(status: str | None, provider: Provider | None, job: Job | None = None,
                ctx: BatchContext | None = None) -> bool:
    if status in cp.FINAL_STATUSES or status in (cp.NEEDS_REVIEW, cp.UNRELIABLE):
        return False
    no_engine = provider is None and (ctx is None or ctx.paddle is None)
    if status == cp.PENDING_ENGINE and (no_engine or (job and ctx and _api_scanned(job, ctx))):
        return False
    return True


CONVERSION_LABELS = {REASON_CODE: REASON_TEXT, FORCED_OCR_CODE: FORCED_OCR_TEXT}


def _verify_enabled(ctx: BatchContext) -> bool:
    return ctx.settings.verify_text_with_ocr and ctx.provider is not None


def _needs_reclassify(state: cp.FileState, n: int, job: Job, ctx: BatchContext) -> bool:
    """صفحة اكتملت سابقاً لكن تصنيفها قد يتغير: قواعد جودة أحدث، تغيّر خيار "فرض OCR"،
    أو طُلب الفحص المستقل بـ OCR ولم تُفحص بعد."""
    p = state.page(n)
    if not p:
        return False
    if p["status"] == cp.UNRELIABLE:
        # يُعاد فقط إذا تغيّر المحرّك (نموذج آخر) أو صار الإرسال للـ API متاحاً لهذا الملف
        engine = ctx.provider.name if ctx.provider else None
        return (engine is not None and engine != p.get("engine")) or _api_scanned(job, ctx)
    if p["status"] == cp.DONE and p.get("method") == "text_layer":
        if _verify_enabled(ctx) and p.get("ocr_check", {}).get("v") != CHECK_VERSION:
            return True
        return job.force_ocr or p.get("qv", 1) < QUALITY_VERSION
    if p["status"] == cp.PENDING_ENGINE:
        return p.get("converted_reason") == FORCED_OCR_CODE and not job.force_ocr
    return False


def process_file(job: Job, ctx: BatchContext, index: int) -> dict:
    # تأكد أن الملف لم يتغير منذ عرضه (التقدّم مرتبط بالبصمة).
    if ctx.hash_cache.get(job.path) != job.file_hash:
        raise FileChangedError("تغيّر الملف منذ تحميل القائمة. أعد تحميل المجلد.")

    doc = fitz.open(job.path)
    try:
        if doc.needs_pass:
            raise ValueError("الملف محمي بكلمة مرور")
        n_pages = doc.page_count
        state = cp.FileState(ctx.state_dir, job.file_hash, job.name, n_pages)
        state.data["source_path"] = str(job.path)  # للفهرس (تلخيص API يعيد فتح الملف عبر البوابة)
        wanted = [n for n in (job.pages or range(1, n_pages + 1)) if 1 <= n <= n_pages]
        state.data["only_pages"] = wanted if job.pages else None
        state.data["chat"] = job.chat
        ctx.emit("file_start", index=index, name=job.name, pages=len(wanted))
        since_snapshot = 0
        try:
            for n in wanted:
                while ctx.pause_event.is_set() and not ctx.stop_event.is_set():
                    ctx.stop_event.wait(0.3)
                if ctx.stop_event.is_set():
                    break
                _recheck_ocr_output(state, n, job)
                if _needs_reclassify(state, n, job, ctx) or _needs_work(state.status(n), ctx.provider, job, ctx):
                    _process_page(doc, n, state, ctx, job)
                    since_snapshot += 1
                    if since_snapshot >= SNAPSHOT_EVERY:
                        since_snapshot = 0
                        _snapshot_markdown(state, ctx, job)
                ctx.emit("page", index=index, page=n, pages=n_pages, status=state.status(n))
        finally:
            # حتى عند الإيقاف: اكتب ملف Markdown جزئياً بما اكتمل.
            final = finalize_file(state, doc, ctx, job.api_ok, job.path)
        counts = state.counts()
        log.info("ملف %s: %s", job.name, counts)
        converted = {CONVERSION_LABELS.get(k, k): v for k, v in state.converted_counts().items()}
        weak = [n for n in range(1, n_pages + 1) if (state.page(n) or {}).get("ocr_check", {}).get("ok") is False]
        return {"counts": counts, "pages": n_pages, "converted": converted, "weak_pages": weak, **final}
    finally:
        doc.close()


SNAPSHOT_EVERY = 10   # صفحات بين كل لقطة Markdown جزئية (الأجزاء نفسها تُحفظ بعد كل صفحة)


def _snapshot_markdown(state: cp.FileState, ctx: BatchContext, job: Job) -> None:
    """يكتب ما اكتمل حتى الآن في ملف الـ Markdown، فلا يبقى بلا ناتج لو انقطع البرنامج أو الكهرباء وسط ملف كبير."""
    try:
        if ctx.settings.split_parts:   # أجزاء مكتملة بدل ملف واحد يكبر؛ الملف الكامل يُبنى في النهاية
            from .markdown_out import write_ready_parts
            write_ready_parts(state, state.data["source_name"], ctx.md_dir_for(job.path), ctx.settings.split_parts_pages,
                              ctx.settings.normalize_digits, ctx.settings.strip_page_furniture)
        else:
            write_markdown(state, state.data["source_name"], ctx.md_dir_for(job.path), ctx.settings.normalize_digits,
                           ctx.settings.strip_page_furniture)
    except Exception:
        log.exception("تعذّرت اللقطة الجزئية للملف %s", job.name)


def _process_page(doc: fitz.Document, n: int, state: cp.FileState, ctx: BatchContext, job: Job) -> None:
    try:
        page = doc.load_page(n - 1)
        info = classify_page(page, ctx.settings.text_min_chars, job.force_ocr)
        meta = {"info": info.to_dict(), "qv": QUALITY_VERSION}
        # الصفحة الممسوحة ضوئياً تُخزَّن أحياناً كقطع صور كبيرة (ضغط MRC): ليست "صوراً داخل المستند"،
        # ونصها يُقرأ بـ OCR. الصور تُكتشف في الصفحات النصية فقط.
        photos = _photos_meta(page, state.page(n)) if info.kind == PageKind.TEXT else []
        if photos:
            meta["photos"] = photos
        if info.converted_reason:
            meta["converted_reason"] = info.converted_reason
        if info.kind == PageKind.TEXT:
            text_md = page_text_to_markdown(page, ctx.settings.extract_tables, ctx.settings.table_rtl_columns)
            if _verify_enabled(ctx):
                meta["ocr_check"] = _cross_check(page, n, text_md, state, ctx)
                if job.api_ok and not meta["ocr_check"]["ok"]:
                    _hold_for_review(state, n, text_md, "weak_match", meta)
                    return
            state.save_page(n, cp.DONE, "text_layer", text_md, **meta)
        elif info.kind == PageKind.BLANK:
            state.save_page(n, cp.BLANK, "none", **meta)
        elif (ctx.provider is None and ctx.paddle is None) or (_api_scanned(job, ctx) and ctx.paddle is None):
            state.save_page(n, cp.PENDING_ENGINE, "none", **meta)
        elif ctx.paddle is not None and _paddle_page(page, n, state, ctx, meta, job):
            pass
        elif ctx.provider is not None:
            _ocr_page(page, n, state, ctx, meta, job)
        else:
            state.save_page(n, cp.PENDING_ENGINE, "none", **meta)
    except EngineUnavailable:
        raise  # المحرّك نفسه توقف: لا فائدة من إكمال الصفحات
    except Exception as e:  # صفحة واحدة تالفة لا توقف الملف
        log.exception("خطأ في الصفحة %d من %s", n, state.data["source_name"])
        state.save_page(n, cp.ERROR, "none", error=str(e))
        ctx.emit("log", text=f"خطأ في الصفحة {n}: {e}")


def _photos_meta(page: fitz.Page, prev: dict | None) -> list[dict]:
    """الصور المضمّنة في الصفحة؛ الوصف السابق يبقى إن لم تتغير الصورة."""
    from .images import find_photos
    old = {tuple(p["bbox"]): p for p in (prev or {}).get("photos") or [] if p.get("bbox")}
    return [old.get(tuple(b)) or {"bbox": list(b)} for b in find_photos(page)]


def _run_ocr(page: fitz.Page, n: int, ctx: BatchContext, attempt: int = 0):
    provider = ctx.provider
    if provider.is_external:
        # المزوّد الخارجي يمر فقط عبر cloud_gate (المرحلة 3). لا مسار مباشر.
        raise PermissionError("المزوّد الخارجي غير مسموح خارج بوابة الإرسال")
    dpi = ctx.settings.render_dpi
    if attempt:
        dpi = max(120, int(dpi * (1.0 - 0.2 * attempt)))   # دقة مختلفة تغيّر مدخل النموذج وتكسر الحلقة
    req = OcrRequest(images=[PageImage(n, render_page_png(page, dpi), dpi)], attempt=attempt)
    return next(r for r in provider.ocr(req) if r.page_number == n)


def _cross_check(page: fitz.Page, n: int, text_md: str, state: cp.FileState, ctx: BatchContext) -> dict:
    """يقرأ الصفحة النصية بـ OCR أيضاً ويقارن. نتيجة OCR تُحفظ بجانب الجزء للمراجعة."""
    result = _run_ocr(page, n, ctx)
    score = match_score(text_md, result.markdown)
    ok = score >= ctx.settings.verify_min_score
    atomic_write_text(state.pages_dir / f"p{n:04d}.ocr.md", result.markdown)
    if not ok:
        ctx.emit("log", text=f"⚠ الصفحة {n}: تطابق ضعيف بين طبقة النص وOCR ({score:.0%}) — راجعها أو فعّل فرض OCR")
    return {"v": CHECK_VERSION, "score": score, "ok": ok, "engine": ctx.provider.name}


def _recheck_ocr_output(state: cp.FileState, n: int, job: Job) -> None:
    """صفحات OCR مكتملة من تشغيل سابق (قبل كاشف التكرار): تُفحص من جديد بدون إعادة تشغيل النموذج."""
    p = state.page(n)
    if not p or p["status"] != cp.DONE or not p.get("method", "").startswith("ocr:"):
        return
    if p.get("ocrq", 0) >= OCR_CHECK_VERSION:
        return
    text = state.read_fragment(n)
    problem = repetition_problem(text)
    if problem:
        _reject_unreliable(state, n, text, problem, state.carry_meta(n), p["method"][4:], job)
    else:
        state.update_meta(n, ocrq=OCR_CHECK_VERSION)


def _reject_unreliable(state, n, text, problem, meta, engine, job: Job) -> None:
    log.warning("الصفحة %d من %s: ناتج غير موثوق (%s)", n, state.data["source_name"], problem)
    if job.api_ok:
        _hold_for_review(state, n, text, "repetition", meta)
        return
    atomic_write_text(state.local_path(n), text)
    state.save_page(n, cp.UNRELIABLE, "none", **meta, engine=engine, problem=problem)


def _ocr_page(page: fitz.Page, n: int, state: cp.FileState, ctx: BatchContext, meta: dict, job: Job) -> None:
    result = _run_ocr(page, n, ctx)
    problem = repetition_problem(result.markdown)
    for attempt in (1, 2):
        if not problem:
            break
        ctx.emit("log", text=f"⚠ الصفحة {n}: حلقة تكرار ({problem}) — إعادة المحاولة {attempt}/2")
        result = _run_ocr(page, n, ctx, attempt)
        problem = repetition_problem(result.markdown)
    if problem:
        ctx.emit("log", text=f"⚠ الصفحة {n}: ناتج المحرّك غير موثوق ({problem}) — لن يُكتب نصها")
        _reject_unreliable(state, n, result.markdown, problem, {**meta, **result.meta}, ctx.provider.name, job)
        return
    meta = {**meta, "ocrq": OCR_CHECK_VERSION}
    if job.api_ok:
        from .providers.render import page_ink
        reason = weak_ocr_reason(result.markdown, page_ink(page))
        if reason:
            _hold_for_review(state, n, result.markdown, reason, {**meta, **result.meta})
            return
    state.save_page(n, cp.DONE, f"ocr:{ctx.provider.name}", result.markdown, **meta, **result.meta)


def _paddle_page(page: fitz.Page, n: int, state: cp.FileState, ctx: BatchContext, meta: dict, job: Job) -> bool:
    """يقرأ الصفحة بـ PaddleOCR. يعيد True إذا حُسمت الصفحة، وFalse لتنتقل للمحرّك المحلي
    (أو تبقى pending_engine إن لم يوجد). أي فشل في Paddle لا يوقف البرنامج."""
    from . import paddle_engine as pe
    s = ctx.settings
    try:
        png, scale = pe.page_image(page)
        res = pe.ocr_page(ctx.paddle, png, s.paddle_min_conf, mark_low=True, scale=scale)
    except Exception as e:
        log.warning("PaddleOCR فشل في الصفحة %d من %s: %s", n, state.data["source_name"], e)
        nxt = "المحرّك المحلي" if ctx.provider is not None else "تبقى بانتظار المحرّك"
        ctx.emit("log", text=f"⚠ الصفحة {n}: تعذّر PaddleOCR ({e}) — {nxt}")
        _restart_paddle(ctx)
        return False
    atomic_write_text(state.paddle_path(n), res.raw)
    meta["paddle"] = {"min_conf": round(res.min_conf, 4), "low_lines": res.low_lines, "lines": len(res.lines),
                      "ms": res.ms}
    problem = repetition_problem(res.markdown)
    if problem:
        ctx.emit("log", text=f"⚠ الصفحة {n}: ناتج PaddleOCR غير موثوق ({problem})")
        return False
    dpi = pe.scan_dpi(page)
    if dpi is not None:
        meta["paddle"]["scan_dpi"] = round(dpi)
    if getattr(ctx.paddle, "last_skew", 0.0):
        meta["paddle"]["skew"] = round(ctx.paddle.last_skew, 1)
    if pe.poor_scan_with_digits(dpi, res.markdown):
        meta["paddle"]["poor_digits"] = True
    if s.scan_engine == "paddle_local" and res.low_lines and ctx.provider is not None:
        ctx.emit("log", text=f"الصفحة {n}: {len(res.low_lines)} سطر بثقة أقل من {s.paddle_min_conf:.2f} "
                             "— تُعاد على النموذج المحلي")
        return False
    route = router.decide(res.markdown, [ln.conf for ln in res.lines], router.image_trouble(png), dpi)
    meta["paddle"]["route"] = route.to_meta()
    if route.numbers and res.strips:
        # أشرطة أسطر الأرقام تبقى في مجلد العمل (محلياً): عند اختلاف القراءتين تُرسل وحدها بدل الصفحة
        for k, png_k in res.strips.items():
            state.strip_path(n, k).parent.mkdir(parents=True, exist_ok=True)
            state.strip_path(n, k).write_bytes(png_k)
        meta["paddle"]["strips"] = sorted(res.strips)
    extra: dict = {"ocrq": OCR_CHECK_VERSION}
    if route.kind != router.LOCAL:
        # نص Paddle يُحفظ دائماً (لا تبقى الصفحة فارغة إن فشل الطرف الخارجي)؛ وcloud_need تجعل هذه الصفحة
        # وحدها تُرسل في المرحلة الثانية (dispatcher.pages_to_send) متى كان الإرسال مسموحاً لهذا الملف.
        extra["cloud_need"] = route.kind
        if job.api_ok:
            ctx.emit("log", text=f"الصفحة {n}: {route.label()} — تُرسل وحدها عبر المواقع")
        else:
            # لا طرف خارجي يؤكد: النص المحلي يبقى، والمشكوك فيه يظهر في الملف وفي رسالة نهاية الدفعة
            extra["local_warning"] = route.reasons
            if route.numbers:
                extra["unverified_numbers"] = (route.suspicious + [x for x in route.numbers
                                                                   if x not in route.suspicious])[:20]
            ctx.emit("log", text=f"⚠ الصفحة {n}: {route.label()} — لا يوجد مزوّد للتأكيد، راجعها بنفسك")
    state.save_page(n, cp.DONE, pe.METHOD, res.markdown, **meta, **extra)
    return True


def _restart_paddle(ctx: BatchContext) -> None:
    """إذا ماتت عملية Paddle تُعاد مرة واحدة في الدفعة؛ بعدها يُترك Paddle (ctx.paddle = None)."""
    w = ctx.paddle
    if w is None or getattr(w, "alive", True):
        return
    if getattr(w, "restarts", 0) >= 1:
        ctx.emit("log", text="✗ PaddleOCR متوقف — باقي الصفحات للمحرّك المحلي أو تبقى بانتظاره")
        ctx.paddle = None
        return
    w.restarts = getattr(w, "restarts", 0) + 1
    try:
        w.start()
        ctx.emit("log", text="أُعيد تشغيل PaddleOCR")
    except Exception as e:
        log.warning("تعذّرت إعادة تشغيل PaddleOCR: %s", e)
        ctx.paddle = None


def _learn_words(state: cp.FileState, ctx: BatchContext) -> None:
    """صفحات طبقة النص السليمة تغذّي القاموس المحلي (كل صفحة مرة واحدة)."""
    items = []
    for n in range(1, state.page_count + 1):
        p = state.page(n) or {}
        if p.get("status") != cp.DONE or p.get("method") != "text_layer":
            continue
        if (p.get("ocr_check") or {}).get("ok") is False:
            continue
        items.append((f"{state.file_hash}:{n}", state.read_fragment(n)))
    if items:
        try:
            ctx.get_dictionary().add_pages(items)
        except OSError as e:
            log.warning("تعذّر تحديث القاموس: %s", e)


_DROP_ON_HOLD = ("status", "method", "updated", "score", "review_reason")


def finalize_file(state: cp.FileState, doc: fitz.Document, ctx: BatchContext, api_ok: bool,
                  src: Path | None = None) -> dict:
    """بعد معالجة صفحات الملف (أو إيقافها): القاموس، درجات الصفحات، تعليق صفحات OCR الضعيفة في
    ملفات وضع "عبر المواقع" لنافذة المراجعة، كتابة الـ Markdown (مع دمج الجداول)، وتقرير المراجعة."""
    from . import review_report
    from .markdown_out import merged_fragments
    from .page_score import score_state, weak_pages
    from .providers.render import page_ink

    name = state.data["source_name"]
    try:
        _learn_words(state, ctx)
        score_state(state, ctx.settings, ctx.get_dictionary(), lambda n: page_ink(doc.load_page(n - 1)))
        if api_ok:
            for n, reasons in weak_pages(state).items():
                p = state.page(n) or {}
                if (p.get("paddle") or {}).get("route"):
                    continue   # قرّر الموجّه مصير صفحة Paddle (محلية أو تُرسل وحدها) — لا تعليق للمراجعة
                if p.get("method", "").startswith("ocr:"):
                    meta = {k: v for k, v in p.items() if k not in _DROP_ON_HOLD}
                    _hold_for_review(state, n, state.read_fragment(n), reasons[0], meta)
    except Exception:
        log.exception("تعذّر حساب درجات الصفحات للملف %s", name)
    out = write_markdown(state, name, ctx.md_dir_for(src), ctx.settings.normalize_digits,
                         ctx.settings.strip_page_furniture)
    if ctx.settings.split_parts and all(state.status(n) in cp.FINAL_STATUSES
                                        for n in (state.data.get("only_pages") or range(1, state.page_count + 1))):
        from .markdown_out import remove_parts
        remove_parts(ctx.md_dir_for(src), name)   # اكتمل الملف ودُمج في الملف النهائي: الأجزاء لم تعد لازمة
    docx_path = ""
    if ctx.settings.export_docx:
        try:
            from .docx_out import export_markdown_file
            docx_path = str(export_markdown_file(out))
        except Exception:
            log.exception("تعذّر إنشاء ملف Word للملف %s", name)
            ctx.emit("log", text=f"✗ تعذّر إنشاء ملف Word لـ {name}")
    _merged, tstats = merged_fragments(state)
    try:
        entries = review_report.update_file(ctx.output_dir, state, name, doc, tstats.mismatch_pages)
    except Exception:
        log.exception("تعذّر تحديث تقرير المراجعة للملف %s", name)
        entries = []
    return {"markdown": str(out), "docx": docx_path, "tables": tstats.to_dict(), "weak_count": len(weak_pages(state)),
            "review_count": len(entries), "local_warnings": local_warnings(state)}


def local_warnings(state: cp.FileState) -> dict[int, str]:
    """{الصفحة: ما يُشك فيه} لصفحات قرأها OCR الداخلي ولم يؤكدها طرف خارجي — تُعرض للمستخدم في نهاية الدفعة."""
    out = {}
    for n in (state.data.get("only_pages") or range(1, state.page_count + 1)):
        p = state.page(n) or {}
        if p.get("status") == cp.DONE and p.get("local_warning") and p.get("method") == "ocr:paddle":
            out[n] = "، ".join(router.LABELS.get(r, r) for r in p["local_warning"])
    return out


def _finalize_path(path: Path, file_hash: str, ctx: BatchContext, api_ok: bool) -> tuple[cp.FileState, dict]:
    with fitz.open(path) as doc:
        state = cp.FileState(ctx.state_dir, file_hash, path.name, doc.page_count)
        return state, finalize_file(state, doc, ctx, api_ok, path)


def _hold_for_review(state: cp.FileState, n: int, local_md: str, reason: str, meta: dict) -> None:
    """صفحة ضعيفة في وضع "عبر المواقع": تُعلَّق ويكمل البرنامج. الناتج المحلي يُحفظ جانباً فقط."""
    atomic_write_text(state.local_path(n), local_md)
    state.save_page(n, cp.NEEDS_REVIEW, "none", **meta, review_reason=reason)


def run_batch(jobs: list[Job], ctx: BatchContext) -> None:
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    if ctx.api is not None:
        for job in jobs:
            job.api_ok = ctx.api.gate.allowed(job.file_hash)
    for i, job in enumerate(jobs):
        if ctx.stop_event.is_set():
            break
        try:
            summary = process_file(job, ctx, i)
            ctx.emit("file_done", index=i, name=job.name, **summary)
        except EngineUnavailable as e:
            log.error("توقف المحرّك: %s", e)
            ctx.emit("file_error", index=i, name=job.name, error=f"توقف المحرّك المحلي: {e}")
            ctx.stop_event.set()
            break
        except Exception as e:
            log.exception("فشل الملف %s", job.name)
            ctx.emit("file_error", index=i, name=job.name, error=str(e))
    quota_stopped = False
    if ctx.api is not None and not ctx.stop_event.is_set():
        quota_stopped = run_api_phase(jobs, ctx)
    ctx.hash_cache.save()
    if ctx.api is not None:
        items = review_items(jobs, ctx)
        if items:
            ctx.emit("review_needed", items=items)
    if ctx.settings.auto_build_index:
        build_index_safe(ctx)
    ctx.emit("finished", stopped=ctx.stop_event.is_set() or quota_stopped)


def build_index_safe(ctx: BatchContext, full: bool = False) -> dict | None:
    """يبني INDEX.md. التلخيص عبر API فقط إذا فُعّل الخيار ويوجد مزوّد (ctx.api)؛ والقرار لكل ملف
    في index_builder عبر gate.allowed، والبوابة تتحقق مجدداً عند الإرسال."""
    from .index_builder import build_index
    from .summarize import ApiSummarizer
    api = None
    if ctx.settings.summary_via_api and ctx.api is not None:
        slot = next((s for s in ctx.api.slots if not s.down and not s.is_ocr), None)  # OCR لا يلخّص
        if slot is not None:
            api = ApiSummarizer(ctx.api.gate, slot.provider, getattr(slot.provider, "model", ""))
    try:
        res = build_index(ctx.output_dir, ctx.settings, full=full, api=api)
    except Exception as e:
        log.exception("تعذّر بناء الفهرس")
        ctx.emit("log", text=f"✗ تعذّر بناء الفهرس: {e}")
        return None
    ctx.emit("log", text=f"الفهرس: {res['files']} ملف (أُعيد بناء {res['built']}، دون تغيير {res['reused']}) — {res['path']}")
    ctx.emit("index_built", **res)
    return res


def _state_for(path: Path, file_hash: str, ctx: BatchContext) -> cp.FileState:
    with fitz.open(path) as doc:
        return cp.FileState(ctx.state_dir, file_hash, path.name, doc.page_count)


def run_api_phase(jobs: list[Job], ctx: BatchContext, only_index: set[int] | None = None) -> bool:
    """المرحلة الثانية: إرسال الصفحات المطلوبة للمزوّدات.
    يعيد True إذا توقف لنفاد حصص كل المزوّدات."""
    from .dispatcher import ApiJob, pages_to_send
    api_jobs, photo_jobs = [], []
    for i, job in enumerate(jobs):
        if not job.api_ok or (only_index is not None and i not in only_index):
            continue
        state = _state_for(job.path, job.file_hash, ctx)
        aj = ApiJob(job.path, job.file_hash, i, chat=job.chat)
        if pages_to_send(state, ctx.settings):
            api_jobs.append(aj)
        if ctx.settings.describe_images and _photos_pending(state):
            photo_jobs.append(aj)
    if not api_jobs and not photo_jobs:
        return False
    live = len([s for s in ctx.api.slots if not s.down])
    report = None
    if api_jobs:
        ctx.emit("log", text=f"إرسال صفحات {len(api_jobs)} ملف للمزوّدات ({live} مزوّد متاح)")
        report = ctx.api.run(api_jobs)
    for aj in photo_jobs:
        if ctx.stop_event.is_set():
            break
        ctx.api.describe_photos(aj)
    seen = set()
    for aj in api_jobs + photo_jobs:
        if aj.index in seen:
            continue
        seen.add(aj.index)
        state, final = _finalize_path(aj.path, aj.file_hash, ctx, True)
        ctx.emit("file_done", index=aj.index, name=aj.name, counts=state.counts(), pages=state.page_count,
                 converted={}, weak_pages=[], providers=state.providers_summary(), **final)
    return bool(report and report.quota_stopped)


def _photos_pending(state: cp.FileState) -> bool:
    only = state.data.get("only_pages")
    return any(not ph.get("desc") for n in (only or range(1, state.page_count + 1))
               for ph in (state.page(n) or {}).get("photos") or [])


def review_items(jobs: list[Job], ctx: BatchContext, require_api: bool = True) -> list[dict]:
    """الصفحات المعلّقة للمراجعة (وضع "عبر المواقع")."""
    items = []
    for i, job in enumerate(jobs):
        if require_api and not job.api_ok:
            continue
        try:
            state = _state_for(job.path, job.file_hash, ctx)
        except Exception:
            continue
        for n in range(1, state.page_count + 1):
            p = state.page(n) or {}
            if p.get("status") == cp.NEEDS_REVIEW:
                try:
                    seen = state.local_path(n).read_text(encoding="utf-8")
                except OSError:
                    seen = ""
                items.append({"index": i, "path": str(job.path), "hash": job.file_hash, "page": n,
                              "reason": p.get("review_reason", ""), "text": seen})
    return items


def apply_review_decisions(decisions: list[dict], ctx: BatchContext) -> set[int]:
    """قرارات نافذة المراجعة: "leave" ⇒ [غير مقروء] نهائياً، "accept" ⇒ يُعتمد النص المعدَّل يدوياً، "send" ⇒ تُرسل مع قصاصة اختيارية.
    يعيد أرقام (index) الملفات التي فيها صفحات للإرسال."""
    to_send: set[int] = set()
    by_file: dict[str, list[dict]] = {}
    for d in decisions:
        by_file.setdefault(d["path"], []).append(d)
    for path, ds in by_file.items():
        state = _state_for(Path(path), ds[0]["hash"], ctx)
        for d in ds:
            n = d["page"]
            if state.status(n) != cp.NEEDS_REVIEW:
                continue
            if d["action"] == "leave":
                state.save_page(n, cp.ILLEGIBLE, "none", **state.carry_meta(n), review_reason=d.get("reason", ""))
            elif d["action"] == "accept" and (d.get("text") or "").strip():
                state.save_page(n, cp.DONE, "manual", d["text"].strip(), **state.carry_meta(n))
            elif d["action"] == "send":
                state.update_meta(n, review_action="send", clip=list(d["clip"]) if d.get("clip") else None)
                to_send.add(d["index"])
        _finalize_path(Path(path), ds[0]["hash"], ctx, False)
    return to_send


def read_page_now(api, path: str, page: int, settings, slot_id: str = "") -> str:
    """قراءة صفحة واحدة بنموذج الرؤية عند الطلب (زر «اقرأها الآن» في نافذة المراجعة).
    api: ApiRunner. يرفع استثناءً برسالة مفهومة إن تعذّر."""
    from .providers.base import OcrRequest
    from .providers.batching import build_prompt, split_and_check
    from .providers.render import render_page_jpeg
    from .quality import looks_like_refusal
    if slot_id:   # اختيار المستخدم لمزوّد بعينه (نموذج رؤية أو محرّك OCR مثل Google Vision)
        slot = next((sl for sl in api.slots if sl.id == slot_id), None)
        if slot is None:
            raise RuntimeError("المزوّد المختار غير مفعّل أو بلا مفتاح")
    else:
        slot = next(iter(api.llm_slots()), None)
    if slot is None:
        raise RuntimeError("لا يوجد نموذج رؤية متاح (راجع المزوّدات في الإعدادات)")
    try:
        ticket = api.gate.open_file(Path(path))
        with fitz.open(path) as doc:
            img = render_page_jpeg(doc[page - 1], page, settings.api_dpi, settings.api_max_dim, settings.api_jpeg_quality)
        text = slot.limiter.call(lambda: api.gate.send(ticket, slot.provider, build_prompt([page]), OcrRequest([img]),
                                                       slot.provider.model))
        if slot.is_ocr:   # محرّك OCR يعيد النص بلا علامات صفحات ولا تعليمات
            body = text.strip()
            if not body:
                raise RuntimeError(f"{slot.spec.name} لم يُخرج نصاً لهذه الصفحة")
            return body
        chk = split_and_check(text, [img])
        body = chk.pages.get(page, "").strip()
        if not body or body == "<!-- blank -->" or looks_like_refusal(body):
            raise RuntimeError("النموذج لم يُرجع نصاً لهذه الصفحة" + (f" ({'؛ '.join(chk.problems)})" if chk.problems else ""))
        return body
    except Exception:
        raise
