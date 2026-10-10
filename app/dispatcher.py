"""وضع "توزيع": كل مزوّد مفعّل يأخذ ملفات كاملة بالتوازي (عامل واحد لكل مزوّد).

- التوزيع بالملف كاملاً لتبقى جودة المستند الواحد متسقة.
- إذا سقط مزوّد في منتصف ملف (حصة منتهية، مفتاح مرفوض، أخطاء متكررة) يعود الملف لرأس
  الطابور ويكمله مزوّد آخر من الصفحة التي توقف عندها (checkpoint).
- إذا سقطت كل المزوّدات تتوقف الدفعة والتقدّم محفوظ للاستئناف لاحقاً.
- كل إرسال يمر عبر cloud_gate.
- سلسلة OCR (إن وُجد مزوّد نوعه "ocr" مفعّل): OCR.space لكل صفحة ← مصحّح نص بأول نموذج لغة
  متاح ← الصفحات الضعيفة صورة + نص لنموذج رؤية. إذا سقطت كل مزوّدات OCR تكمل نماذج الرؤية
  ما تبقى من الملفات بالطريقة المعتادة، وإن لم يوجد شيء تتوقف الدفعة وتُستأنف لاحقاً.
"""

from __future__ import annotations

import collections
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from . import checkpoint as cp
from . import ocr_router as router
from .cloud_gate import CloudGate, GateTicket
from .config import Settings
from . import corrector, layout
from .fsutil import atomic_write_json, atomic_write_text
from .markdown_out import write_markdown
from .page_score import score_text
from .providers.base import AuthError, ExternalProvider, OcrRequest, PageImage, QuotaExhausted
from .providers.batching import MARKER_RE, build_prompt, chunk_pages, normalize_page, run_with_split, split_and_check, tail_lines
from .quality import weak_ocr_reason
from .providers.catalog import ProviderSpec, ProviderState
from .providers.render import render_page_jpeg
from .rate_limit import GiveUp, RateLimiter, Stopped, UsageCounter

log = logging.getLogger(__name__)

Emit = Callable[..., None]


@dataclass
class Slot:
    spec: ProviderSpec
    state: ProviderState
    provider: ExternalProvider
    limiter: RateLimiter
    down: str = ""
    down_kind: str = ""          # "quota" أو "busy" أو "auth" أو "errors"
    picker: object = None        # model_picker.ModelPicker: النموذج الأقل ازدحاماً تلقائياً

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def is_ocr(self) -> bool:
        return self.spec.is_ocr

    def batch_size(self, settings: Settings) -> int:
        return max(1, min(settings.api_pages_per_call, self.spec.max_pages_per_call,
                          self.provider.max_pages_per_call))


@dataclass
class ApiJob:
    path: Path
    file_hash: str
    index: int = 0
    ticket: GateTicket | None = None
    chat: bool = False

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class RunReport:
    quota_stopped: bool = False
    down: dict[str, str] = field(default_factory=dict)
    file_errors: dict[str, str] = field(default_factory=dict)


class _Defer(Exception):
    """الملف لا يناسب هذا المزوّد (محادثات على OCR): يُؤجَّل لمرحلة نماذج الرؤية."""


class _SlotDown(Exception):
    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def pages_to_send(state: cp.FileState, settings: Settings) -> list[int]:
    out = []
    only = state.data.get("only_pages")
    for n in (only or range(1, state.page_count + 1)):
        p = state.page(n) or {}
        s = p.get("status")
        if s in (cp.PENDING_ENGINE, cp.ERROR, cp.UNRELIABLE) and settings.api_for_scanned:
            out.append(n)
        elif s == cp.DONE and p.get("cloud_need") and not p.get("cloud_tried"):
            out.append(n)   # قرأها OCR الداخلي وقرر الموجّه أنها تحتاج تأكيداً (ocr_router): هذه الصفحة وحدها
        elif s == cp.NEEDS_REVIEW:
            if p.get("review_action") == "send":
                out.append(n)
            elif settings.auto_send_weak and not p.get("api_tried"):
                out.append(n)
    return out


class ApiRunner:
    def __init__(self, gate: CloudGate, slots: list[Slot], settings: Settings, state_dir: Path, docs_dir: Path,
                 emit: Emit, stop: threading.Event, md_dir_for: Callable[[Path], Path] | None = None,
                 pause: threading.Event | None = None):
        self.gate = gate
        self.slots = slots
        self.settings = settings
        self.state_dir = state_dir
        self.docs_dir = docs_dir
        self.md_dir_for = md_dir_for or (lambda _src: docs_dir)
        self.emit = emit
        self.stop = stop
        self._cond = threading.Condition()
        self._queue: collections.deque[ApiJob] = collections.deque()
        self._busy = 0
        self.report = RunReport()
        self.pause = pause or threading.Event()    # "إيقاف مؤقت": العمل ينتظر بعد الطلبات الجارية
        self._dict = None
        self._ocr_sem = threading.Semaphore(OCR_CONCURRENCY)
        self._e3_warned = False
        if settings.fast_corrector:   # المصحّح نصي فقط: نسخة أخف وأسرع من نفس المزوّد إن وُجدت
            for sl in slots:
                lite = next((m for m in sl.spec.fallback_models if "lite" in m), "")
                if lite and hasattr(sl.provider, "text_model"):
                    sl.provider.text_model = lite

    # ------------------------------------------------------------------ التشغيل
    def ocr_slots(self) -> list[Slot]:
        if self.settings.ocr_engine == "vision":
            return []
        return [s for s in self.slots if s.is_ocr and not s.down]

    def llm_slots(self) -> list[Slot]:
        return [s for s in self.slots if not s.is_ocr and not s.down]

    def run(self, jobs: list[ApiJob]) -> RunReport:
        self._queue = collections.deque(jobs)
        # المرحلة 1: سلسلة OCR + المصحّح. المرحلة 2: ما تبقى (أو كل شيء) لنماذج الرؤية.
        self._deferred: list[ApiJob] = []
        for phase in (self.ocr_slots, self.llm_slots):
            if phase == self.llm_slots and self._deferred:
                self._queue.extend(self._deferred)
                self._deferred = []
            live = phase()
            if not live or not self._queue or self.stop.is_set():
                continue
            threads = [threading.Thread(target=self._worker, args=(s,), name=f"api-{s.id}", daemon=True)
                       for s in live]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        if self._queue and not self.stop.is_set():
            self.report.quota_stopped = True
            names = "، ".join(j.name for j in self._queue)
            kinds = {s.down_kind for s in self.slots if s.down}
            if kinds == {"quota"}:
                why = "انتهت الحصة اليومية لكل المزوّدات"
            elif "busy" in kinds:
                why = "خوادم المزوّدات مشغولة حالياً (ضغط مؤقت من جهتهم، ليس خطأ في ملفك)"
            else:
                why = "لا يوجد مزوّد متاح"
            self.emit("log", text=f"⏸ {why}. التقدّم محفوظ؛ اضغط ابدأ لاحقاً للإكمال من نفس الصفحة: {names}")
        return self.report

    def _next_job(self) -> ApiJob | None:
        with self._cond:
            while True:
                if self.stop.is_set():
                    return None
                if self._queue:
                    self._busy += 1
                    return self._queue.popleft()
                if self._busy == 0:
                    self._cond.notify_all()
                    return None
                self._cond.wait(0.5)

    def _done_job(self, requeue: ApiJob | None = None) -> None:
        with self._cond:
            self._busy -= 1
            if requeue is not None:
                self._queue.appendleft(requeue)
            self._cond.notify_all()

    def _worker(self, slot: Slot) -> None:
        while True:
            job = self._next_job()
            if job is None:
                return
            try:
                self.process(job, slot)
            except _Defer:
                self._deferred.append(job)
                self._done_job()
                continue
            except _SlotDown as e:
                slot.down = str(e)
                slot.down_kind = e.kind
                self.report.down[slot.id] = str(e)
                self.emit("provider_down", provider=slot.id, reason=str(e))
                self.emit("log", text=f"⚠ المزوّد {slot.spec.name} خرج من الدفعة: {e}. ينتقل {job.name} لمزوّد آخر.")
                self._done_job(requeue=job)
                return
            except Stopped:
                self._done_job()
                return
            except Exception as e:
                log.exception("فشل إرسال الملف %s", job.name)
                self.report.file_errors[job.name] = str(e)
                self.emit("log", text=f"✗ {job.name}: {e}")
                self._done_job()
                continue
            self._done_job()

    # ------------------------------------------------------------------ الملف
    def process(self, job: ApiJob, slot: Slot) -> None:
        if slot.is_ocr and job.chat:   # لقطات المحادثات: نموذج الرؤية وحده يميّز المتحدثين
            raise _Defer()
        if slot.is_ocr:
            return self.process_ocr(job, slot)
        if job.ticket is None:
            job.ticket = self.gate.open_file(job.path)  # البصمة مرة واحدة عند بدء الملف
        if job.ticket.file_hash != job.file_hash:
            raise PermissionError("تغيّر الملف منذ تحميل القائمة")
        s = self.settings
        with fitz.open(job.path) as doc:
            state = cp.FileState(self.state_dir, job.file_hash, job.name, doc.page_count)
            pages = pages_to_send(state, s)
            if not pages:
                return
            size = slot.batch_size(s)
            self.emit("api_file", index=job.index, name=job.name, provider=slot.id, pages=len(pages), batch=size)
            try:
                for chunk in chunk_pages(pages, size):
                    if self.stop.is_set():
                        raise Stopped()
                    self._send_chunk(doc, chunk, state, job, slot)
                    self.emit("api_progress", provider=slot.id, count=slot.limiter.usage.get(slot.id))
            finally:
                write_markdown(state, job.name, self.md_dir_for(job.path), s.normalize_digits, s.strip_page_furniture)

    def _send_chunk(self, doc, chunk: list[int], state: cp.FileState, job: ApiJob, slot: Slot) -> None:
        s = self.settings
        images = [
            render_page_jpeg(doc[n - 1], n, s.api_dpi, s.api_max_dim, s.api_jpeg_quality,
                             clip=tuple((state.page(n) or {}).get("clip") or ()) or None)
            for n in chunk
        ]
        tail = ""
        prev = chunk[0] - 1
        if prev >= 1 and state.status(prev) == cp.DONE and s.context_tail_lines > 0:
            tail = tail_lines(state.read_fragment(prev), s.context_tail_lines)
        picker = slot.picker

        def attempt(prompt, req):
            if picker is not None:
                slot.provider.model = picker.current
            text = self.gate.send(job.ticket, slot.provider, prompt, req, slot.provider.model)
            if picker is not None:
                picker.ok(slot.provider.model)
            return text

        def on_switch(kind):
            if picker is None:
                return False
            moved, old, new = picker.switch(kind)
            if moved:
                why = {"busy": "مزدحم", "limited": "بلغ حد الدقيقة", "quota": "انتهت حصته اليوم"}[kind]
                self.emit("log", text=f"↪ {slot.spec.name}: {old} {why} — التحويل تلقائياً إلى {new}")
            return moved

        def send(prompt, req):
            return slot.limiter.call(lambda: attempt(prompt, req), log=lambda t: self.emit("log", text=t),
                                     on_switch=on_switch)

        def on_retry(pages, problems):
            self.emit("log", text=f"↻ {job.name} ص{pages} ({slot.id}): فحص الاكتمال فشل ({'؛ '.join(problems)}) — إعادة صفحةً صفحة")

        try:
            from .images import CHAT_PAGE_RULES
            outcomes = run_with_split(send, images, tail, on_retry, CHAT_PAGE_RULES if job.chat else "")
        except GiveUp as e:
            raise _SlotDown(str(e), "busy" if e.busy else "errors") from None
        except QuotaExhausted as e:
            raise _SlotDown(str(e), "quota") from None
        except AuthError as e:
            raise _SlotDown(str(e), "auth") from None
        model = slot.provider.model
        for o in outcomes:
            meta = state.carry_meta(o.page)
            if o.markdown is not None:
                state.save_page(o.page, cp.DONE, f"api:{slot.id}", normalize_page(o.markdown),
                                **meta, model=model, batch_size=o.batch_size)
                state.local_path(o.page).unlink(missing_ok=True)
            else:
                state.save_page(o.page, cp.NEEDS_REVIEW, "none", **meta, review_reason="api_incomplete",
                                api_tried=True, problems=o.problems[:5], tried_provider=slot.id)
                self.emit("log", text=f"⚠ {job.name} ص{o.page}: ناتج {slot.id} ناقص — عُلّقت للمراجعة")
            self.emit("page", index=job.index, page=o.page, pages=state.page_count, status=state.status(o.page))


    # ------------------------------------------------------------------ سلسلة OCR
    def _corrector_slot(self) -> Slot | None:
        if not self.settings.corrector_enabled:
            return None
        live = self.llm_slots()
        return live[0] if live else None

    def process_ocr(self, job: ApiJob, slot: Slot) -> None:
        """دفعات من 3 صفحات متتالية؛ settings.ocr_parallel دفعات تعمل بالتوازي. في كل دفعة:
        OCR للصفحات الثلاث معاً ← طلب تصحيح واحد للصفحات الجيدة ← الصفحات الضعيفة صورة + نص."""
        if job.ticket is None:
            job.ticket = self.gate.open_file(job.path)
        if job.ticket.file_hash != job.file_hash:
            raise PermissionError("تغيّر الملف منذ تحميل القائمة")
        with fitz.open(job.path) as doc:
            state = cp.FileState(self.state_dir, job.file_hash, job.name, doc.page_count)
            pages = pages_to_send(state, self.settings)
            if not pages:
                return
            self.emit("api_file", index=job.index, name=job.name, provider=slot.id, pages=len(pages),
                      batch=CHAIN_BATCH)
            report: dict[int, dict] = {}
            locks = {"doc": threading.Lock(), "save": threading.Lock()}
            abort = threading.Event()
            try:
                workers = max(1, min(self.settings.ocr_parallel, 4))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"ocr-{slot.id}") as pool:
                    futures = [pool.submit(self._ocr_chunk, doc, chunk, state, job, slot, locks, abort)
                               for chunk in chunk_pages(pages, CHAIN_BATCH)]
                    first_error: BaseException | None = None
                    for f in futures:
                        try:
                            report.update(f.result())
                        except BaseException as e:   # _SlotDown / Stopped: أوقف بقية الدفعات وارفع الأول
                            abort.set()
                            first_error = first_error or e
                    if first_error is not None:
                        raise first_error
            finally:
                write_markdown(state, job.name, self.md_dir_for(job.path), self.settings.normalize_digits,
                               self.settings.strip_page_furniture)
                if report:
                    self._write_report(state, job.name, report)

    def _wait_if_paused(self, abort: threading.Event | None = None) -> None:
        while self.pause.is_set() and not self.stop.is_set():
            self.stop.wait(0.3)
        if self.stop.is_set() or (abort is not None and abort.is_set()):
            raise Stopped()

    def _ocr_engine_for(self, slot: Slot) -> None:
        """المحرّك المختار (3 افتراضياً)، و2 بعد بلوغ 3 حده الشهري. المحرّك 1 لا يقبل العربية."""
        p = slot.provider
        if not hasattr(p, "set_engine"):
            return
        want = self.settings.ocrspace_engine
        if want == 3 and self._engine3_spent(slot):
            if not self._e3_warned:
                self._e3_warned = True
                self.emit("log", text=f"↪ {slot.spec.name}: بلغ المحرّك 3 حده الشهري — التحويل إلى المحرّك 2")
            want = 2
        p.set_engine(want)

    @staticmethod
    def _engine3_spent(slot: Slot) -> bool:
        from .providers.ocrspace import ENGINE3_MONTHLY
        limit = slot.spec.free_tier.get("engine3_monthly") or ENGINE3_MONTHLY
        return slot.limiter.usage.get_month(f"{slot.id}:e3") >= limit

    def _ocr_send(self, job: ApiJob, slot: Slot, img) -> str:
        try:
            text = slot.limiter.call(lambda: self.gate.send(job.ticket, slot.provider, "", OcrRequest([img]),
                                                            slot.provider.model),
                                     log=lambda t: self.emit("log", text=t))
        except GiveUp as e:
            raise _SlotDown(str(e), "busy" if e.busy else "errors") from None
        except QuotaExhausted as e:
            raise _SlotDown(str(e), "quota") from None
        except AuthError as e:
            raise _SlotDown(str(e), "auth") from None
        if getattr(slot.provider, "engine", 0) == 3:
            slot.limiter.usage.add_month(f"{slot.id}:e3")
        return text

    def _ocr_one(self, job: ApiJob, slot: Slot, img, abort: threading.Event) -> str:
        """صفحة واحدة بـ OCR (في خيطها). المحرّك 3 يعيد أحياناً صفحة فارغة: تُعاد بالمحرّك الآخر مرة."""
        self._wait_if_paused(abort)
        n = img.page_number
        self._ocr_engine_for(slot)
        text = self._ocr_send(job, slot, img)
        p = slot.provider
        if hasattr(p, "set_engine") and (img.ink or 0) >= 0.006 and not split_and_check(
                text, [img]).pages.get(n, "").replace("<!-- blank -->", "").strip():
            other = 2 if p.engine == 3 else 3
            if other == 2 or not self._engine3_spent(slot):
                first = p.engine
                p.set_engine(other)
                self.emit("log", text=f"↻ {job.name} ص{n}: المحرّك {first} أعاد نصاً فارغاً — إعادة بالمحرّك {other}")
                try:
                    text = self._ocr_send(job, slot, img)
                finally:
                    p.set_engine(first)
        return text

    def _ocr_limited(self, job: ApiJob, slot: Slot, img, abort: threading.Event) -> str:
        # OCR.space يرد 429 على الطلبات المتزامنة الكثيرة (تجربة 2026-10-10): طلبان معاً كحد أقصى
        with self._ocr_sem:
            return self._ocr_one(job, slot, img, abort)

    def _good_enough(self, body: str) -> bool:
        """تخطّي المصحّح: القاموس المحلي (يتعلم من طبقات النص) يعرف كل كلمات الصفحة تقريباً."""
        d = self._dictionary()
        if d is None or len(d) < self.settings.dict_min_words:
            return False
        unk, n_ar, _ = d.unknown_ratio(body)
        return n_ar >= 30 and unk <= SKIP_UNKNOWN_RATIO

    def _dictionary(self):
        if self._dict is None:
            try:
                from .dictionary import ArabicDictionary
                self._dict = ArabicDictionary()
            except Exception:
                self._dict = False
        return self._dict or None

    def _ocr_chunk(self, doc, chunk: list[int], state: cp.FileState, job: ApiJob, slot: Slot,
                   locks: dict, abort: threading.Event) -> dict[int, dict]:
        s = self.settings
        self._wait_if_paused(abort)
        rng = f"{chunk[0]}-{chunk[-1]}" if len(chunk) > 1 else str(chunk[0])
        with locks["doc"]:   # pymupdf ليس آمناً للخيوط
            images = [render_page_jpeg(doc[n - 1], n, s.api_dpi, s.api_max_dim, s.api_jpeg_quality,
                                       clip=tuple((state.page(n) or {}).get("clip") or ()) or None) for n in chunk]
        self.emit("stage", name=job.name, pages=rng, step="ocr")
        with ThreadPoolExecutor(max_workers=len(images)) as pool:
            futs = [pool.submit(self._ocr_limited, job, slot, im, abort) for im in images]
        done, failure = [], None
        for img, f in zip(images, futs):
            try:
                done.append((img, f.result()))
            except BaseException as e:   # الحصة انتهت في منتصف الدفعة: احفظ ما قُرئ ثم ارفع
                failure = failure or e
        if not done:
            raise failure
        chunk = [img.page_number for img, _ in done]
        info: dict[int, dict] = {}
        for img, text in done:
            n = img.page_number
            atomic_write_text(state.pages_dir / f"p{n:04d}.raw.md", text)   # خام كما خرج، بلا توحيد أرقام
            chk = split_and_check(text, [img])
            body = re.sub(r"<!--\s*orientation\s*:\s*\d+\s*-->\s*", "", chk.pages.get(n, "")).strip()
            weak = (not chk.ok or bool(weak_ocr_reason(body, img.ink))
                    or score_text(body, f"api:{slot.id}", s, None, img.ink).weak)
            why = ("rotated" if layout.rotation(text) or layout.is_sideways(img.png)
                   else "table" if layout.has_table(img.png)
                   else "weak" if weak else "")
            # صفحة قرأها OCR الداخلي (ocr_router): OCR الخارجي قراءة ثانية مستقلة لأرقامها. التطابق يكفي (بلا
            # Gemini)، والاختلاف وحده يرسل الصفحة لنموذج الرؤية.
            pg = state.page(n) or {}
            need = pg.get("cloud_need")
            local = state.read_fragment(n) if need else ""
            diff = None
            if need == router.VERIFY:
                diff = router.compare_numbers(local, body)
                # نص Paddle هو المعتمد هنا؛ OCR الخارجي شاهد على الأرقام فقط، فضعف نصه لا يهم. والجدول/الدوران
                # قرره الموجّه بكاشفه الأدق (layout.has_table يخدعه إطار الصفحة)
                why = ""
                if diff and not why:
                    why = "numbers"
            elif need == router.VISION and not why:
                why = (((pg.get("paddle") or {}).get("route") or {}).get("reasons") or ["table"])[0]
            info[n] = {"img": img, "chk": chk, "body": body, "weak": weak, "full_why": why,
                       "need": need, "local": local, "diff": diff}

        llm = self._corrector_slot()
        results: dict[int, tuple[str, corrector.Correction | None, bool]] = {}
        full: dict[int, dict] = {}
        strips: dict[int, dict] = {}
        verified = {n for n, i in info.items() if i["need"] == router.VERIFY and not i["diff"] and not i["full_why"]}
        for n in verified:
            results[n] = (info[n]["local"], None, False)
        if llm is None:
            for n, i in info.items():
                results.setdefault(n, (i["body"], None, False))
        else:
            # جداول وصفحات مدوّرة وصفحات ضعيفة: نموذج الرؤية يقرأ الصفحة كاملة، والأرقام تُفحص مقابل OCR
            if s.send_hard_images:
                for n, i in info.items():
                    if i["full_why"] == "numbers":
                        # أشرطة الأسطر المختلف عليها فقط؛ الصفحة كاملة إن تعذّر ذلك
                        self.emit("stage", name=job.name, pages=str(n), step="numbers")
                        got = self._strip_numbers(llm, job, state, n, i)
                        if got is not None:
                            strips[n] = got
                            results[n] = (got["text"], None, True)
                            continue
                    if i["full_why"]:
                        self.emit("stage", name=job.name, pages=str(n), step="vision")
                        got = self._vision_full(llm, job, i)
                        if got is not None:
                            full[n] = got
                            results[n] = (got["text"], None, True)
            # صفحة "تحقق" لم تتطابق أرقامها ولم يقرأها نموذج الرؤية: لا تصحيح نصي (المصحّح لا يمس الأرقام أصلاً)
            skip = verified | {n for n, i in info.items() if i["need"] == router.VERIFY}
            hard = [n for n, i in info.items() if i["weak"] and s.send_hard_images and n not in full and n not in skip]
            easy = [n for n in info if n not in hard and n not in skip and not self._good_enough(info[n]["body"])]
            easy = [n for n in easy if n not in full]
            for n in info:
                if n not in hard and n not in easy and n not in full:
                    results[n] = (info[n]["body"], None, False)          # جيدة بما يكفي: بلا تصحيح
            self.emit("stage", name=job.name, pages=rng, step="correct")
            if easy:
                raw = "\n\n".join(f"<!-- page:{n} -->\n{info[n]['body']}" for n in easy)
                res = self._correct(llm, job, raw, None)
                fixed = _split_pages(res.text)
                for n in easy:
                    results[n] = (fixed.get(n, info[n]["body"]), res, False)
            for n in hard:
                res = self._correct(llm, job, f"<!-- page:{n} -->\n{info[n]['body']}", info[n]["img"])
                results[n] = (_split_pages(res.text).get(n, info[n]["body"]), res, True)

        entries: dict[int, dict] = {}
        with locks["save"]:
            for n in chunk:
                i, (text, res, used_image) = info[n], results[n]
                meta = state.carry_meta(n)
                fixed = normalize_page(text)
                if i["need"] == router.VERIFY and n not in full:
                    # نص Paddle يبقى. تطابق ⇒ الأرقام مؤكدة؛ أشرطة ⇒ أرقام نموذج الرؤية في نص Paddle؛
                    # اختلاف دون نموذج رؤية ⇒ تُعلَّم بالقراءتين للمستخدم
                    keep = {k: v for k, v in (state.page(n) or {}).items()
                            if k not in ("status", "method", "updated", "unverified_numbers", "local_warning",
                                         "review_reason")}
                    if n in strips:
                        g = strips[n]
                        flags = ({"unverified_numbers": g["flags"][:20], "review_reason": "numbers_unverified"}
                                 if g["flags"] else {})
                        state.save_page(n, cp.DONE, "ocr:paddle", g["text"], **keep, cloud_tried=True,
                                        numbers_by=llm.id, strips_sent=g["sent"], **flags)
                        self.emit("log", text=f"✓ {job.name} ص{n}: {g['sent']} سطر أرقام فقط إلى {llm.spec.name}"
                                              + (f" — ما زال مشكوكاً: {'، '.join(g['flags'][:6])}" if g["flags"] else ""))
                    elif n in verified:
                        state.save_page(n, cp.DONE, "ocr:paddle", i["local"], **keep, cloud_tried=True,
                                        verified_by=slot.id)
                        self.emit("log", text=f"✓ {job.name} ص{n}: أرقام OCR الداخلي طابقت {slot.spec.name} — بلا Gemini")
                    else:
                        state.save_page(n, cp.DONE, "ocr:paddle", i["local"], **keep, cloud_tried=True,
                                        unverified_numbers=i["diff"][:20], local_warning=["numbers"],
                                        review_reason="numbers_unverified")
                        self.emit("log", text=f"⚠ {job.name} ص{n}: أرقام مختلفة بين القراءتين "
                                              f"({'، '.join(i['diff'][:6])}) ولا نموذج رؤية متاح — راجعها")
                elif n in full:
                    f = full[n]
                    extra = ({"review_reason": "numbers_unverified", "unverified_numbers": f["missing"][:20]}
                             if f["missing"] else {})
                    state.save_page(n, cp.DONE, f"api:{llm.id}", fixed, **meta, model=llm.provider.model,
                                    ocr_by=slot.id, vision_full=i["full_why"], ocr_weak=i["weak"], **extra)
                    if f["missing"]:
                        self.emit("log", text=f"⚠ {job.name} ص{n}: أرقام من نموذج الرؤية لم تظهر في OCR: "
                                              f"{'، '.join(f['missing'][:8])} — عُلّمت للمراجعة")
                elif not fixed and not i["chk"].ok:
                    state.save_page(n, cp.NEEDS_REVIEW, "none", **meta, review_reason="api_incomplete",
                                    api_tried=True, problems=i["chk"].problems[:5], tried_provider=slot.id)
                elif res is None:
                    state.save_page(n, cp.DONE, f"api:{slot.id}", fixed, **meta, model=slot.provider.model,
                                    corrector="skipped" if llm else "off", ocr_weak=i["weak"])
                else:
                    mine = [e for e in res.edits if e.original in i["body"]]
                    extra = {} if res.ok else {"review_reason": "corrector_failed"}
                    state.save_page(n, cp.DONE, f"api:{slot.id}", fixed, **meta, model=slot.provider.model,
                                    corrector=llm.id, corrector_ok=res.ok,
                                    edits_applied=sum(e.applied for e in mine),
                                    edits_rejected=sum(not e.applied for e in mine),
                                    ocr_weak=i["weak"], with_image=used_image, **extra)
                    if not res.ok:
                        self.emit("log", text=f"⚠ {job.name} ص{n}: {res.problem} — تُركت خاماً وعُلّمت للمراجعة")
                    entries[n] = {"edits": [e.to_dict() for e in mine], "ok": res.ok, "problem": res.problem,
                                  "image": used_image}
                    atomic_write_json(state.pages_dir / f"p{n:04d}.edits.json", entries[n])
                self.emit("page", index=job.index, page=n, pages=state.page_count, status=state.status(n))
            self.emit("api_progress", provider=slot.id, count=slot.limiter.usage.get(slot.id))
        if failure is not None:
            raise failure
        return entries

    def _strip_numbers(self, llm: Slot, job: ApiJob, state: cp.FileState, n: int, i: dict) -> dict | None:
        """اختلفت أرقام الصفحة بين OCR الداخلي والخارجي: صورة واحدة فيها أسطر الأرقام المختلف عليها فقط لنموذج
        الرؤية، وأرقامه تُوضع في نص Paddle بترتيبها. None ⇒ الطريق المعتاد (الصفحة كاملة)."""
        if llm.down:
            return None
        keys = ((state.page(n) or {}).get("paddle") or {}).get("strips") or []
        sel = [k for k in router.disputed_lines(i["local"], keys, i["diff"]) if state.strip_path(n, k).is_file()]
        if not sel:
            return None
        items = [(j, state.strip_path(n, k).read_bytes()) for j, k in enumerate(sel, 1)]
        img = PageImage(n, router.compose_strips(items), 150, "image/jpeg")
        try:
            reply = llm.limiter.call(
                lambda: self.gate.send(job.ticket, llm.provider, router.STRIP_PROMPT, OcrRequest([img]),
                                       llm.provider.model),
                log=lambda m: self.emit("log", text=m))
        except (GiveUp, QuotaExhausted, AuthError) as e:
            llm.down, llm.down_kind = f"نموذج الرؤية: {e}", "quota" if isinstance(e, QuotaExhausted) else "errors"
            self.emit("log", text=f"⚠ نموذج الرؤية غير متاح ({e}) — ص{n} تبقى بأرقامها المعلّمة")
            return None
        got = router.parse_strip_reply(reply)
        if got is None:
            self.emit("log", text=f"⚠ {job.name} ص{n}: رد غير مفهوم على أسطر الأرقام — تُقرأ الصفحة كاملة")
            return None
        lines, doubt = i["local"].split("\n\n"), []
        for j, k in enumerate(sel, 1):
            if j in got:
                lines[k], d = router.apply_numbers(lines[k], got[j])
                doubt += d
        text = "\n\n".join(lines)
        flags = list(dict.fromkeys(doubt + router.remaining_doubts(text, i["local"], i["body"])))
        return {"text": text, "flags": flags, "sent": len(items)}

    def _vision_full(self, llm: Slot, job: ApiJob, i: dict) -> dict | None:
        """الصفحة كاملة بنموذج الرؤية (نفس تعليمات النسخ الحرفي). None عند الفشل ⇒ المسار المعتاد."""
        if llm.down:
            return None
        img = i["img"]
        n = img.page_number
        try:
            text = llm.limiter.call(
                lambda: self.gate.send(job.ticket, llm.provider, build_prompt([n]), OcrRequest([img]),
                                       llm.provider.model),
                log=lambda m: self.emit("log", text=m))
        except (GiveUp, QuotaExhausted, AuthError) as e:
            llm.down, llm.down_kind = f"نموذج الرؤية: {e}", "quota" if isinstance(e, QuotaExhausted) else "errors"
            self.emit("log", text=f"⚠ نموذج الرؤية غير متاح ({e}) — ص{n} بنص OCR")
            return None
        chk = split_and_check(text, [img])
        if not chk.ok:
            self.emit("log", text=f"⚠ {job.name} ص{n}: ناتج الرؤية ناقص ({'؛ '.join(chk.problems)}) — بنص OCR")
            return None
        body = chk.pages.get(n, "")
        # الرقم مقبول إن ظهر في أي قراءة أخرى للصفحة نفسها (OCR الخارجي أو الداخلي)
        return {"text": body, "missing": layout.unverified_numbers(body, f"{i['body']}\n{i.get('local', '')}")}

    def _correct(self, llm: Slot, job: ApiJob, raw: str, img) -> corrector.Correction:
        def ask(raw_text: str) -> str:
            if img is not None:
                prompt = f"{corrector.VISION_PROMPT}\n\n<<<\n{raw_text}\n>>>"
                return self.gate.send(job.ticket, llm.provider, prompt, OcrRequest([img]), llm.provider.model)
            return self.gate.send_text(job.ticket, llm.provider, corrector.TEXT_PROMPT, raw_text,
                                       getattr(llm.provider, "text_model", "") or llm.provider.model,
                                       purpose="correct")
        if llm.down:
            return corrector.Correction(raw, ok=False, problem="المصحّح غير متاح")
        try:
            res = corrector.correct(raw, lambda t: llm.limiter.call(lambda: ask(t),
                                                                     log=lambda m: self.emit("log", text=m)))
        except (GiveUp, QuotaExhausted, AuthError) as e:
            llm.down, llm.down_kind = f"المصحّح: {e}", "quota" if isinstance(e, QuotaExhausted) else "errors"
            self.emit("log", text=f"⚠ المصحّح {llm.spec.name} غير متاح ({e}) — الصفحات تبقى خاماً")
            return corrector.Correction(raw, ok=False, problem="المصحّح غير متاح")
        res.used_image = img is not None
        return res

    # ------------------------------------------------------------------ الصور داخل المستند
    def describe_photos(self, job: ApiJob) -> int:
        """يصف كل صورة مضمّنة لم توصف بعد (ويكتب نص المحادثات). يعيد عدد الصور الموصوفة."""
        from .images import PHOTO_PROMPT
        llm = next((s for s in self.llm_slots()), None)
        if llm is None:
            return 0
        if job.ticket is None:
            job.ticket = self.gate.open_file(job.path)
        count = 0
        with fitz.open(job.path) as doc:
            state = cp.FileState(self.state_dir, job.file_hash, job.name, doc.page_count)
            for n in (state.data.get("only_pages") or range(1, state.page_count + 1)):
                page = state.page(n) or {}
                photos = page.get("photos") or []
                if not any(not ph.get("desc") for ph in photos):
                    continue
                self.emit("stage", name=job.name, pages=str(n), step="photos")
                for ph in photos:
                    if ph.get("desc") or self.stop.is_set():
                        continue
                    self._wait_if_paused()
                    img = render_page_jpeg(doc[n - 1], n, 150, self.settings.api_max_dim, 85, clip=tuple(ph["bbox"]))
                    try:
                        ph["desc"] = llm.limiter.call(
                            lambda: self.gate.send(job.ticket, llm.provider, PHOTO_PROMPT, OcrRequest([img]),
                                                   llm.provider.model),
                            log=lambda t: self.emit("log", text=t)).strip()
                        count += 1
                    except (GiveUp, QuotaExhausted, AuthError) as e:
                        self.emit("log", text=f"⚠ وصف الصور متوقف ({e}) — تبقى [صورة]")
                        state.update_meta(n, photos=photos)
                        return count
                state.update_meta(n, photos=photos)
        if count:
            self.emit("log", text=f"🖼 {job.name}: وُصفت {count} صورة")
        return count

    def _write_report(self, state: cp.FileState, name: str, report: dict[int, dict]) -> None:
        for f in state.pages_dir.glob("p*.edits.json"):   # صفحات صُحّحت في دفعات سابقة
            try:
                report.setdefault(int(f.name[1:5]), json.loads(f.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        target = self.state_dir.parent / "corrections" / f"{Path(name).stem}.md"
        atomic_write_text(target, corrector.render_report(name, report))


_MARKER_HEAD = re.compile(r"^\s*<!--\s*page\s*:\s*\d+\s*-->\s*")
OCR_CONCURRENCY = 1            # OCR.space المجاني يرد 429 حتى على طلبين متزامنين (تجربتان 2026-10-10)
CHAIN_BATCH = 3                 # صفحات في كل دفعة OCR + طلب تصحيح واحد
SKIP_UNKNOWN_RATIO = 0.03       # كلمات غير معروفة للقاموس أقل من 3% ⇒ لا حاجة للمصحّح


def _split_pages(text: str) -> dict[int, str]:
    """نص فيه علامات <!-- page:N --> ← {N: متن الصفحة}."""
    found = list(MARKER_RE.finditer(text or ""))
    return {int(m.group(1)): text[m.end():(found[i + 1].start() if i + 1 < len(found) else len(text))].strip()
            for i, m in enumerate(found)}


# ---------------------------------------------------------------------- البناء
def make_provider(spec: ProviderSpec, state: ProviderState, key: str, model: str = "") -> ExternalProvider:
    model = model or state.model or spec.default_model
    if not model:
        raise ValueError(f"{spec.name}: لم يُحدد اسم النموذج")
    if spec.kind == "ocr":
        from .providers.ocrspace import OcrSpaceProvider
        return OcrSpaceProvider(spec.id, key, engine=3, endpoint=spec.base_url)
    if spec.kind == "gemini":
        from .providers.gemini import GeminiProvider
        return GeminiProvider(key, model, spec.max_pages_per_call)
    from .providers.openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(spec.id, spec.resolved_base_url(state.extra), key, model, spec.max_pages_per_call)


def build_slots(config, secrets, usage: UsageCounter, stop: threading.Event,
                emit: Emit = lambda *a, **k: None) -> list[Slot]:
    slots = []
    for spec, st, key in config.active(secrets):
        from .model_picker import models_for, picker_for
        picker = picker_for(spec.id, models_for(spec, st))
        try:
            provider = make_provider(spec, st, key, picker.current)
        except Exception as e:
            emit("log", text=f"⚠ تعذّر تجهيز {spec.name}: {e}")
            continue
        limiter = RateLimiter(spec.id, st.rpm, usage, rpd=spec.rpd, stop=stop, rpmonth=spec.rpmonth)
        slot = Slot(spec, st, provider, limiter, picker=picker)
        if usage.is_exhausted(spec.id):
            slot.down, slot.down_kind = "الحصة اليومية منتهية", "quota"
        slots.append(slot)
    return slots
