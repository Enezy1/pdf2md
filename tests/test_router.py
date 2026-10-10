"""اختبارات موجّه الصفحات المصوّرة (app/ocr_router.py): OCR الداخلي أولاً، والطرف الخارجي للصفحة المشكوك فيها
وحدها. كل الطلبات لخوادم وهمية على 127.0.0.1 و worker وهمي لـ Paddle."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))
os.environ["PDF2MD_SECRETS_BACKEND"] = "memory"

from app import checkpoint as cp  # noqa: E402
from app import ocr_router as router  # noqa: E402
from app.config import Settings  # noqa: E402
from app.dispatcher import pages_to_send  # noqa: E402
from app.pipeline import BatchContext, Job, local_warnings, run_batch  # noqa: E402
from fake_ocrspace_server import ok_payload  # noqa: E402
from test_ocr_correct import ChainEnv  # noqa: E402
from test_paddle import box, fake_worker  # noqa: E402

WITH_NUMBERS = [
    {"text": "في القضية رقم ٢٠٢٥/١٣٨٨٠ تجاري كلي", "conf": 0.93, "box": box(400, 100, 900, 140)},
    {"text": "بالجلسة المنعقدة علناً في يوم ٢٢/١٠/٢٠٢٥", "conf": 0.91, "box": box(400, 200, 900, 240)},
]
TEXT_ONLY = [
    {"text": "حكمت المحكمة وقبل الفصل في الموضوع بندب إدارة الخبراء", "conf": 0.95, "box": box(300, 100, 900, 140)},
    {"text": "وأبقت الفصل في المصروفات", "conf": 0.94, "box": box(500, 200, 900, 240)},
]
WEAK = [{"text": "ل و المكمة ا", "conf": 0.35, "box": box(300, 100, 900, 140)},
        {"text": "٣r٧eهل", "conf": 0.30, "box": box(300, 200, 900, 240)}]


# ------------------------------------------------------------ القرار نفسه
class RouterUnitTests(unittest.TestCase):
    def test_numbers_extracted_in_english_digits(self):
        self.assertEqual(router.numbers("القضية ٢٠٢٥/١٣٨٨٠ بتاريخ 22/10/2025 المادة ٢"),
                         ["2025/13880", "22/10/2025"])          # رقم من خانة واحدة ليس "مهماً"

    def test_implausible(self):
        self.assertTrue(router.implausible("33/9/2025"))         # يوم مستحيل
        self.assertTrue(router.implausible("17/6/25"))           # الصفر الضائع: سنة من خانتين
        self.assertFalse(router.implausible("17/6/2025"))
        self.assertFalse(router.implausible("2024/9/17"))
        self.assertFalse(router.implausible("2025/13880"))       # رقم قضية، ليس تاريخاً

    def test_noise_ratio(self):
        self.assertLess(router.noise_ratio("State Of Kuwait محكمة الاستئناف شركة ش م ك م المُستشار"), 0.1)
        self.assertGreater(router.noise_ratio("I جر I ء I ت مL u = = = ="), 0.5)

    def test_apply_numbers_in_order(self):
        self.assertEqual(router.apply_numbers("القضية رقم 2025/13889 كلي 44", ["2025/13880", "44"]),
                         ("القضية رقم 2025/13880 كلي 44", []))
        line = "القضية رقم 2025/13889 كلي 44"
        self.assertEqual(router.apply_numbers(line, ["2025/13880"]), (line, ["2025/13880"]))   # عدد مختلف: لا محاذاة
        self.assertEqual(router.apply_numbers(line, ["2025/1388?", "44"]), (line, ["2025/1388"]))  # شك النموذج

    def test_parse_reply_and_disputed_lines(self):
        self.assertEqual(router.parse_strip_reply('```json\n{"1": ["٢٠٢٥/١٣٨٨٠"], "#2": []}\n```'),
                         {1: ["2025/13880"], 2: []})
        self.assertIsNone(router.parse_strip_reply("لا أستطيع"))
        frag = "عنوان\n\nرقم 2025/13889\n\nيوم 22/10/2025"
        self.assertEqual(router.disputed_lines(frag, [1, 2], ["2025/13889", "2025/13880"]), [1])
        self.assertEqual(router.disputed_lines(frag, [1, 2], ["44"]), [1, 2])   # موضعه غير معروف: كل الأسطر

    def test_remaining_doubts_two_of_three(self):
        self.assertEqual(router.remaining_doubts("رقم 2025/13880", "رقم 2025/13889", "رقم 2025/13880"), [])
        self.assertEqual(router.remaining_doubts("رقم 2025/13881", "رقم 2025/13889", "رقم 2025/13880"),
                         ["2025/13881", "2025/13880"])

    def test_compare_by_digits_only(self):
        self.assertEqual(router.compare_numbers("رقم 2025/13880", "رقم 2025-13880"), [])
        self.assertEqual(router.compare_numbers("سنة 198", "سنة 1980"), ["198", "1980"])
        self.assertEqual(router.compare_numbers("القضية", "القضية 44"), ["44"])   # رقم أسقطه Paddle كلياً

    def test_decide(self):
        good = [0.95, 0.9, 0.92]
        self.assertEqual(router.decide("نص بلا أرقام", good).kind, router.LOCAL)
        self.assertEqual(router.decide("رقم 153 لسنة 2024", good).kind, router.VERIFY)
        self.assertEqual(router.decide("رقم 153", good, dpi=100).kind, router.REREAD)   # قرار المستخدم: <150
        self.assertEqual(router.decide("نص بلا أرقام", good, dpi=100).kind, router.LOCAL)
        self.assertEqual(router.decide("ل و المكمة ٣r٧eهل = ~", good).kind, router.REREAD)     # ضجيج
        self.assertEqual(router.decide("نص سليم", [0.4, 0.5, 0.6]).kind, router.REREAD)         # ثقة منهارة
        self.assertEqual(router.decide("نص سليم", [0.6, 0.7, 0.9]).kind, router.LOCAL)          # الختم يخفض الثقة فقط
        r = router.decide("نص 33/9/2025", good, trouble="table")
        self.assertEqual((r.kind, r.reasons, r.suspicious), (router.VISION, ["table"], ["33/9/2025"]))
        # "مدوّرة" لا تُصدَّق إن قرأ Paddle الصفحة جيداً، وتُصدَّق إن فشل
        self.assertEqual(router.decide("نص بلا أرقام", good, trouble="rotated").kind, router.LOCAL)
        self.assertEqual(router.decide("نص", [0.3, 0.4], trouble="rotated").kind, router.VISION)

    def test_page_frame_is_not_a_table(self):
        import pymupdf as fitz

        def page_png(draw):
            doc = fitz.open()
            p = doc.new_page(width=420, height=595)
            draw(p)
            return p.get_pixmap(dpi=100).tobytes("png")

        def frame(p):                        # إطار الحكم: خطوط الحواف وخطوط أفقية فقط
            for x in (25, 395):
                p.draw_line((x, 30), (x, 565), width=1.5)
            for y in (30, 120, 300, 480, 565):
                p.draw_line((25, y), (395, y), width=1.5)

        def grid(p):
            frame(p)
            for x in (120, 210, 300):         # أعمدة داخلية
                p.draw_line((x, 120), (x, 565), width=1.5)
        self.assertFalse(router.table_inside(page_png(frame)))
        self.assertTrue(router.table_inside(page_png(grid)))


# ------------------------------------------------------------ الوضع الداخلي (بلا مزوّد)
def at_dpi(case, dpi):
    """صورة الاختبار 100 نقطة؛ الدقة "المقيسة" تُثبَّت لاختبار كل مسار على حدة."""
    patcher = mock.patch("app.paddle_engine.scan_dpi", return_value=dpi)
    patcher.start()
    case.addCleanup(patcher.stop)


class LocalModeTests(unittest.TestCase):
    def setUp(self):
        at_dpi(self, 300)
        from test_cloud import make_scanned_pdf
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = make_scanned_pdf(self.tmp / "حكم.pdf", 1)
        self.out = self.tmp / "out"
        from app.hashing import HashCache
        self.cache = HashCache(self.tmp / "c.json")
        self.events = []

    def _run(self, lines):
        s = Settings()
        s.scan_engine = "paddle"
        ctx = BatchContext(self.out, s, threading.Event(), lambda k, **kw: self.events.append((k, kw)), self.cache,
                           None, paddle=fake_worker(lines))
        try:
            run_batch([Job(self.pdf, self.cache.get(self.pdf))], ctx)
        finally:
            ctx.paddle.close()
        return cp.FileState(self.out / ".state", self.cache.get(self.pdf), self.pdf.name, 1)

    def test_numbers_kept_but_flagged_for_user(self):
        st = self._run(WITH_NUMBERS)
        p = st.page(1)
        self.assertEqual((p["status"], p["method"], p["cloud_need"]), (cp.DONE, "ocr:paddle", router.VERIFY))
        self.assertIn("2025/13880", p["unverified_numbers"])
        self.assertEqual(local_warnings(st), {1: "أرقام تحتاج تأكيداً"})
        md = (self.out / "docs" / "حكم.md").read_text(encoding="utf-8")
        self.assertIn("في القضية رقم 2025/13880", md)                   # النص لم يُحذف
        self.assertIn("⚠ أرقام تحتاج مراجعة", md)
        done = [kw for k, kw in self.events if k == "file_done"][0]
        self.assertEqual(done["local_warnings"], {1: "أرقام تحتاج تأكيداً"})

    def test_clean_text_page_stays_silent(self):
        st = self._run(TEXT_ONLY)
        self.assertEqual(st.page(1)["paddle"]["route"]["route"], router.LOCAL)
        self.assertNotIn("cloud_need", st.page(1))
        self.assertEqual(local_warnings(st), {})

    def test_weak_text_flagged_in_markdown(self):
        st = self._run(WEAK)
        self.assertEqual(st.page(1)["cloud_need"], router.REREAD)
        md = (self.out / "docs" / "حكم.md").read_text(encoding="utf-8")
        self.assertIn("check:local_unverified weak_text", md)


# ------------------------------------------------------------ "عبر المواقع": الصفحة المشكوك فيها وحدها
class CloudRoutingTests(unittest.TestCase):
    def setUp(self):
        at_dpi(self, 300)
        self.env = ChainEnv()

    def tearDown(self):
        self.env.close()

    def _run(self, paddle_lines, slots, pages=1):
        pdf = self.env.pdf("حكم.pdf", pages)
        s = self.env.settings()
        s.api_for_scanned = False            # الصفحات المصوّرة لا تُرسل كلها: الموجّه وحده يقرر
        s.scan_engine = "paddle"
        ctx = self.env.ctx(slots, s)
        ctx.paddle = fake_worker(paddle_lines)
        try:
            run_batch([Job(pdf, self.env.hash_cache.get(pdf))], ctx)
        finally:
            ctx.paddle.close()
        return pdf, self.env.state(pdf)

    def test_numbers_confirmed_by_external_ocr_without_gemini(self):
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("في القضية رقم 2025/13880 تجاري\nيوم 22/10/2025")))
        llm = self.env.slot("llm", lambda pages, i: (200, {}, "لا يُستدعى"))
        pdf, st = self._run(WITH_NUMBERS, [ocr, llm])
        p = st.page(1)
        self.assertEqual((p["method"], p["verified_by"], p["cloud_tried"]), ("ocr:paddle", "ocrspace", True))
        self.assertNotIn("unverified_numbers", p)
        self.assertEqual(len(ocr.fake.calls), 1)
        self.assertEqual(llm.fake.calls, [])                              # لا Gemini إطلاقاً
        md = (self.env.out / "docs" / "حكم.md").read_text(encoding="utf-8")
        self.assertNotIn("⚠", md)

    def test_disagreement_sends_only_number_strips(self):
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", (400, 40), 255).save(buf, "PNG")
        strip = __import__("base64").b64encode(buf.getvalue()).decode()
        lines = [dict(x, strip=strip) for x in WITH_NUMBERS]
        lines[0]["text"] = "في القضية رقم ٢٠٢٥/١٣٨٨٩ تجاري كلي"          # Paddle أخطأ الرقم الأخير
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("في القضية رقم 2025/13880 تجاري\nيوم 22/10/2025")))
        llm = self.env.slot("llm", lambda pages, i: (200, {}, '{"1": ["2025/13880"]}'))
        pdf, st = self._run(lines, [ocr, llm])
        p = st.page(1)
        self.assertEqual((p["method"], p["numbers_by"], p["strips_sent"]), ("ocr:paddle", "llm", 1))
        self.assertNotIn("unverified_numbers", p)                       # الرؤية + OCR الخارجي اتفقا
        self.assertIn("2025/13880", st.read_fragment(1))
        self.assertEqual([c["images"] for c in llm.fake.calls], [1])     # صورة أشرطة واحدة، لا الصفحة

    def test_disagreement_sends_only_that_page_to_vision(self):
        from fake_openai_server import good_text
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("في القضية رقم 2025/13889 تجاري\nيوم 22/10/2025")))
        llm = self.env.slot("llm", lambda pages, i: (200, {}, good_text(pages)))
        pdf, st = self._run(WITH_NUMBERS, [ocr, llm])
        self.assertEqual(st.page(1)["method"], "api:llm")
        self.assertEqual([c["images"] for c in llm.fake.calls], [1])     # صورة هذه الصفحة فقط، مرة واحدة

    def test_disagreement_without_vision_keeps_local_text_and_both_readings(self):
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("في القضية رقم 2025/13889 تجاري\nيوم 22/10/2025")))
        pdf, st = self._run(WITH_NUMBERS, [ocr])
        p = st.page(1)
        self.assertEqual(p["method"], "ocr:paddle")
        self.assertEqual(p["unverified_numbers"], ["2025/13880", "2025/13889"])
        self.assertEqual(local_warnings(st), {1: "أرقام تحتاج تأكيداً"})
        self.assertEqual(pages_to_send(st, self.env.settings()), [])     # لا يُعاد إرسالها في كل تشغيل

    def test_poor_scan_with_numbers_reread_by_the_usual_chain(self):
        at_dpi(self, 100)                    # قرار المستخدم: أقل من 150 نقطة + أرقام ⇒ قراءة خارجية كاملة
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("في القضية رقم 2025/13880")))
        pdf, st = self._run(WITH_NUMBERS, [ocr])
        self.assertEqual(st.page(1)["method"], "api:ocrspace")
        self.assertIn("2025/13880", (self.env.out / "docs" / "حكم.md").read_text(encoding="utf-8"))

    def test_clean_pages_never_leave_the_device(self):
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("نص")))
        llm = self.env.slot("llm", lambda pages, i: (200, {}, "لا يُستدعى"))
        pdf, st = self._run(TEXT_ONLY, [ocr, llm], pages=3)
        self.assertEqual([st.page(n)["method"] for n in (1, 2, 3)], ["ocr:paddle"] * 3)
        self.assertEqual((ocr.fake.calls, llm.fake.calls), ([], []))
        self.assertFalse(self.env.audit.path.exists())                    # لم يُسجَّل أي إرسال


if __name__ == "__main__":
    unittest.main()
