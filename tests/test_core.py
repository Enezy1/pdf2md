"""اختبارات النواة: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

_DATA = tempfile.mkdtemp(prefix="pdf2md-test-data-")
os.environ["PDF2MD_DATA_DIR"] = _DATA

import pymupdf as fitz  # noqa: E402

from app import checkpoint as cp  # noqa: E402
from app.config import Settings  # noqa: E402
from app.hashing import HashCache  # noqa: E402
from app.pdf_io import PageKind, classify_page, fix_mirrored_brackets, normalize_arabic, page_text_to_markdown, text_layer_problems  # noqa: E402
from app.file_options import FileOptions  # noqa: E402
from app.quality import detect_reversed_letters  # noqa: E402
from app.pipeline import BatchContext, Job, run_batch  # noqa: E402
from app.providers.base import PageResult, Provider  # noqa: E402
from make_sample_pdf import build  # noqa: E402



class PdfIoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = build(Path(tempfile.mkdtemp()) / "sample.pdf")

    def test_classification(self):
        with fitz.open(self.pdf) as doc:
            kinds = [classify_page(p, 30).kind for p in doc]
        self.assertEqual(kinds, [PageKind.TEXT, PageKind.SCANNED, PageKind.BLANK, PageKind.SCANNED, PageKind.SCANNED])

    def test_arabic_extraction_normalized(self):
        with fitz.open(self.pdf) as doc:
            md = page_text_to_markdown(doc[0])
        self.assertIn("عقد إيجار", md)
        self.assertIn("750", md)
        self.assertNotRegex(md, "[ﭐ-﷿ﹰ-﻿]")

    def test_garbled_layer_detected(self):
        self.assertTrue(text_layer_problems("ﻫﺬﺍ ﻧﺺ ﻋﺮﺑﻲ Ǆǁﺘﺠﺮﺑﺔ ﺍǄﻔﻘﺮﺓ ﺍǄﺜﺎﻧﻴﺔ ﺍǄﻌﻘﺪ ﻣﻦ"))
        self.assertFalse(text_layer_problems("هذا نص عربي سليم للتجربة والمبلغ 500 دينار"))

    def test_private_use_chars_detected(self):
        self.assertTrue(text_layer_problems("عقد   إيجار"))

    def test_reversed_presentation_forms_detected(self):
        good = "ﻫﺬﺍ ﻧﺺ ﻋﺮﺑﻲ ﻟﻠﺘﺠﺮﺑﺔ ﻭﺍﻟﻤﺒﻠﻎ ﺍﻟﻔﻘﺮﺓ"
        bad = " ".join(w[::-1] for w in good.split())
        self.assertFalse(text_layer_problems(good))
        self.assertTrue(any("معكوس" in p for p in text_layer_problems(bad)))

    def test_paragraphs_and_brackets(self):
        with fitz.open(self.pdf) as doc:
            md = page_text_to_markdown(doc[0])
        self.assertIn("(المؤجر)", md)
        self.assertIn("مقابل مبلغ قدره 750\nدينار", md)  # نفس الفقرة
        self.assertIn("غير حقيقي\n\nإنه", md)            # فقرتان منفصلتان

    def test_fix_mirrored_brackets(self):
        self.assertEqual(fix_mirrored_brackets("السيد )المؤجر( و"), "السيد (المؤجر) و")
        self.assertEqual(fix_mirrored_brackets("السيد (المؤجر) و"), "السيد (المؤجر) و")
        self.assertEqual(fix_mirrored_brackets("see )x( here"), "see )x( here")  # ليس عربياً

    def test_normalize(self):
        self.assertEqual(normalize_arabic("ﻫﺬﺍ ﻧﺺ"), "هذا نص")


class ReversedLettersTests(unittest.TestCase):
    CLEAN = "عمل كامل شامل أمل الخدمة العاصمة الكمبيوتر المهمات المدنية الإدارة لمن الاستخدام"

    def test_clean_words_not_flagged_even_together(self):
        for w in ("عمل", "كامل", "شامل", "أمل", "الخدمة", "العاصمة", "الكمبيوتر", "إملاء", "مليون", "ملف"):
            self.assertFalse(detect_reversed_letters(w).flagged, w)
        self.assertFalse(detect_reversed_letters(self.CLEAN).flagged)

    def test_user_examples_flagged(self):
        for w in ("املهمات", "املدنية", "اإلدارة", "ملن", "االستخدام", "األحكام", "اآلن"):
            r = detect_reversed_letters(f"{w} {w}")
            self.assertTrue(r.flagged, w)

    def test_prefixed_forms(self):
        self.assertTrue(detect_reversed_letters("واملهمات باإلدارة").flagged)
        self.assertTrue(detect_reversed_letters("وملن بااالستخدام").flagged)

    def test_threshold_long_page_needs_two(self):
        filler = " ".join(["الخدمة العاصمة الكمبيوتر كامل شامل"] * 10)
        self.assertFalse(detect_reversed_letters(filler + " املهمات").flagged)  # كلمة واحدة في صفحة طويلة
        self.assertTrue(detect_reversed_letters(filler + " املهمات املدنية").flagged)

    def test_short_page_single_hit(self):
        self.assertTrue(detect_reversed_letters("هذه املهمات").flagged)

    def test_diacritics_and_presentation_forms(self):
        self.assertTrue(detect_reversed_letters("اﻹلدارة املَهمات املهمات").flagged)
        self.assertTrue(detect_reversed_letters("ﺍﻣﻠﻬﻤﺎﺕ ﺍﻣﻠﺪﻧﻴﺔ").flagged)

    def test_page_classified_scanned_with_reason(self):
        pdf = Path(tempfile.mkdtemp()) / "rev.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_htmlbox(
            fitz.Rect(60, 70, 535, 780),
            '<p dir="rtl">تم تكليف املهمات املدنية لمن يتولى اإلدارة في الجهة.</p>',
            css="@font-face {font-family: ar; src: url(arial.ttf);} p {font-family: ar; font-size: 15px;}",
            archive=fitz.Archive("C:/Windows/Fonts"),
        )
        doc.save(pdf)
        doc.close()
        with fitz.open(pdf) as d:
            info = classify_page(d[0], 30)
            self.assertEqual(info.kind, PageKind.SCANNED)
            self.assertEqual(info.converted_reason, "reversed_letters")
            self.assertIn("ترتيب الحروف", info.reasons[0])


class FakeLocalProvider(Provider):
    name = "fake"

    def __init__(self):
        self.calls = 0

    def ocr(self, request):
        self.calls += 1
        return [PageResult(img.page_number, f"نص مقروء من الصفحة {img.page_number}") for img in request.images]


class StopAfter:
    """يضغط "إيقاف" بعد عدد معين من الصفحات."""

    def __init__(self, stop_event, after_pages):
        self.stop_event, self.after, self.pages, self.events = stop_event, after_pages, 0, []

    def __call__(self, kind, **kw):
        self.events.append((kind, kw))
        if kind == "page":
            self.pages += 1
            if self.pages >= self.after:
                self.stop_event.set()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = build(self.tmp / "in" / "عقد تجريبي.pdf")
        self.out = self.tmp / "out"
        self.cache = HashCache(self.tmp / "cache.json")
        self.job = Job(self.pdf, self.cache.get(self.pdf))

    def _ctx(self, emit, stop=None, provider=None):
        return BatchContext(self.out, Settings(), stop or threading.Event(), emit, self.cache, provider)

    def test_full_run_and_markers(self):
        events = []
        run_batch([self.job], self._ctx(lambda k, **kw: events.append((k, kw))))
        md = (self.out / "docs" / "عقد تجريبي.md").read_text(encoding="utf-8")
        for n in range(1, 6):
            self.assertIn(f"<!-- page:{n} -->", md)
            self.assertIn(f"**[صفحة {n}]**", md)
        self.assertIn("عقد إيجار", md)
        self.assertIn("<!-- status:pending_engine -->", md)
        self.assertIn("<!-- status:blank -->", md)
        done = [kw for k, kw in events if k == "file_done"][0]
        self.assertEqual(done["counts"], {"done": 1, "pending_engine": 3, "blank": 1})
        self.assertEqual(events[-1], ("finished", {"stopped": False}))

    def test_resume_after_stop(self):
        stop = threading.Event()
        emit = StopAfter(stop, after_pages=2)
        run_batch([self.job], self._ctx(emit, stop))
        state = json.loads((self.out / ".state" / f"{self.job.file_hash}.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(state["pages"]), ["1", "2"])
        md = (self.out / "docs" / "عقد تجريبي.md").read_text(encoding="utf-8")
        self.assertIn("<!-- incomplete:", md)

        # التشغيل الثاني يكمل من الصفحة 3 ولا يعيد 1 و2
        before = {k: v["updated"] for k, v in state["pages"].items()}
        run_batch([self.job], self._ctx(lambda *a, **k: None))
        state2 = json.loads((self.out / ".state" / f"{self.job.file_hash}.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(state2["pages"]), ["1", "2", "3", "4", "5"])
        self.assertEqual({k: state2["pages"][k]["updated"] for k in before}, before)

    def test_pending_pages_processed_when_engine_arrives(self):
        run_batch([self.job], self._ctx(lambda *a, **k: None))
        provider = FakeLocalProvider()
        run_batch([self.job], self._ctx(lambda *a, **k: None, provider=provider))
        self.assertEqual(provider.calls, 3)  # الصفحات 2 و4 و5 فقط
        md = (self.out / "docs" / "عقد تجريبي.md").read_text(encoding="utf-8")
        self.assertIn("نص مقروء من الصفحة 4", md)
        self.assertNotIn("pending_engine", md)

    def test_force_ocr_converts_text_pages_and_is_reversible(self):
        forced = Job(self.pdf, self.job.file_hash, force_ocr=True)
        events = []
        run_batch([forced], self._ctx(lambda k, **kw: events.append((k, kw))))
        done = [kw for k, kw in events if k == "file_done"][0]
        self.assertEqual(done["counts"], {"pending_engine": 4, "blank": 1})
        self.assertEqual(done["converted"], {"فرض OCR (تجاهل طبقة النص)": 1})
        md = (self.out / "docs" / "عقد تجريبي.md").read_text(encoding="utf-8")
        self.assertNotIn("عقد إيجار", md)
        run_batch([self.job], self._ctx(lambda *a, **k: None))  # إلغاء الفرض يعيد الصفحة النصية
        md = (self.out / "docs" / "عقد تجريبي.md").read_text(encoding="utf-8")
        self.assertIn("عقد إيجار", md)

    def test_old_state_reclassified_when_rules_are_newer(self):
        run_batch([self.job], self._ctx(lambda *a, **k: None))
        sp = self.out / ".state" / f"{self.job.file_hash}.json"
        st = json.loads(sp.read_text(encoding="utf-8"))
        st["pages"]["1"].pop("qv", None)  # كأنها معالَجة قبل الكاشف
        sp.write_text(json.dumps(st), encoding="utf-8")
        before = json.loads(sp.read_text(encoding="utf-8"))["pages"]["1"]["updated"]
        import app.pipeline as pl
        calls = []
        orig = pl._process_page
        pl._process_page = lambda *a, **k: (calls.append(a[1]), orig(*a, **k))
        try:
            run_batch([self.job], self._ctx(lambda *a, **k: None))
        finally:
            pl._process_page = orig
        self.assertEqual(calls, [1])

    def test_file_options_persist(self):
        fo = FileOptions(self.tmp / "fo.json")
        self.assertFalse(fo.force_ocr("a" * 64))
        fo.set_force_ocr([("a" * 64, "x.pdf")], True)
        self.assertTrue(FileOptions(self.tmp / "fo.json").force_ocr("a" * 64))

    def test_external_provider_refused_outside_gate(self):
        class External(FakeLocalProvider):
            is_external = True
        provider = External()
        run_batch([self.job], self._ctx(lambda *a, **k: None, provider=provider))
        self.assertEqual(provider.calls, 0)

    def test_changed_file_refused(self):
        events = []
        stale = Job(self.pdf, "0" * 64)
        run_batch([stale], self._ctx(lambda k, **kw: events.append((k, kw))))
        self.assertEqual(events[0][0], "file_error")


if __name__ == "__main__":
    unittest.main()
