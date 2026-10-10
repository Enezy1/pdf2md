"""اختبارات توحيد الأرقام وسلسلة OCR.space + المصحّح.

كل الطلبات لخوادم وهمية على 127.0.0.1 — لا يُرسل أي طلب حقيقي.
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))
os.environ["PDF2MD_SECRETS_BACKEND"] = "memory"

from PIL import Image  # noqa: E402

from app import checkpoint as cp  # noqa: E402
from app import corrector  # noqa: E402
from app.digits import count_changes, normalize_digits  # noqa: E402
from app.dispatcher import Slot  # noqa: E402
from app.fsutil import write_markdown_file  # noqa: E402
from app.providers.base import OcrRequest, PageImage, QuotaExhausted  # noqa: E402
from app.providers.catalog import ProviderSpec, ProviderState, load_catalog  # noqa: E402
from app.providers.ocrspace import MAX_BYTES, OcrSpaceProvider, classify, fit_jpeg  # noqa: E402
from app.rate_limit import RateLimiter, UsageCounter  # noqa: E402
from app.secrets_store import RedactingFilter, register_secret  # noqa: E402
from fake_ocrspace_server import FakeOcrSpace, error_payload, ok_payload  # noqa: E402
from test_cloud import SECRET, Env  # noqa: E402

OCR_KEY = "K8-OCRSPACE-TEST-KEY-77665544"

_WORDS = ("العقد الطرف الالتزام الشرط البند التعويض الضرر المسؤولية الإخلال التنفيذ الفسخ الإيجار المستأجر "
          "المؤجر العين الأجرة السداد المهلة الإنذار الرسمي الدفع الدفاع الطلب الإثبات الشهود الخبير التقرير "
          "المعاينة الواقعة السبب النتيجة القرار الحكم الطعن الاستئناف التمييز النفاذ المعجل الكفالة الضمان "
          "الفائدة القانونية المصروفات الأتعاب الموعد التاريخ الجهة المختصة الولاية الاختصاص النوعي المكاني").split()
_rnd = random.Random(7)
ARABIC_PAGE = ("قدم المدعي مذكرته رقم ٣٤٥/٢٠٢٤ تجاري وطالب بمبلغ ١٬٢٥٠٫٥٠٠ د.ك وقد تاجلت الجلسة للمرافعة\n"
               + "\n".join(" ".join(_rnd.sample(_WORDS, 7)) for _ in range(30)))


# ---------------------------------------------------------------- (أ) الأرقام
class DigitsTests(unittest.TestCase):
    def test_arabic_and_persian_paragraph(self):
        self.assertEqual(normalize_digits("في ٢٠٢٤/٠٣/١٥ دفع ۱۲۳۴ ديناراً"), "في 2024/03/15 دفع 1234 ديناراً")

    def test_amount_separators(self):
        self.assertEqual(normalize_digits("١٬٢٥٠٫٥٠٠ د.ك"), "1,250.500 د.ك")

    def test_case_number_mixed_letters(self):
        self.assertEqual(normalize_digits("الدعوى رقم ٤٥٦/٢٠٢٣ تجاري كلي/٧"), "الدعوى رقم 456/2023 تجاري كلي/7")
        self.assertEqual(normalize_digits("AB-٧٨٩"), "AB-789")

    def test_arabic_comma_and_text_untouched(self):
        s = "أولاً، نطلب الحكم – بالإلزام؛ «نهائياً»"
        self.assertEqual(normalize_digits(s), s)

    def test_order_kept(self):
        self.assertEqual(normalize_digits("٩٨٧٦٥"), "98765")

    def test_bidi_marks_removed(self):
        self.assertEqual(normalize_digits("‏١٢‎/⁧٣⁩"), "12/3")

    def test_table_cells(self):
        md = "| البند | المبلغ |\n| --- | --- |\n| إيجار | ٢٬٥٠٠٫٧٥ |"
        self.assertEqual(normalize_digits(md), "| البند | المبلغ |\n| --- | --- |\n| إيجار | 2,500.75 |")

    def test_count_changes(self):
        self.assertEqual(count_changes("١٬٢٥٠ abc 12"), 5)

    def test_single_write_point_and_toggle(self):
        d = Path(tempfile.mkdtemp())
        write_markdown_file(d / "a.md", "رقم ٣")
        write_markdown_file(d / "b.md", "رقم ٣", normalize=False)
        self.assertEqual((d / "a.md").read_text(encoding="utf-8"), "رقم 3")
        self.assertEqual((d / "b.md").read_text(encoding="utf-8"), "رقم ٣")

    def test_prompts_carry_rule(self):
        from app.providers.batching import build_prompt
        from app.providers.local_llama import PROMPT
        from app.summarize import API_PROMPT
        self.assertIn("0-9", build_prompt([1]))
        self.assertIn("0-9", PROMPT)
        self.assertIn("0-9", API_PROMPT)
        self.assertIn("0-9", corrector.TEXT_PROMPT)


# ---------------------------------------------------------------- (ب-2) المصحّح
def reply(*edits) -> str:
    return json.dumps({"edits": [{"original": o, "corrected": c, "reason": "ocr"} for o, c in edits]},
                      ensure_ascii=False)


class CorrectorTests(unittest.TestCase):
    RAW = ("<!-- page:3 -->\nقدم المدعى السيد أحمد علي الكندري مذكرة بتاريح ١٥/٣/٢٠٢٤ وطالب بمبلغ ١٬٢٥٠ "
           "د.ك عن الدعوي رقم ٤٥٦/٢٠٢٣ امام محكمة الاستئناف العليا\n| البند | المبلغ |\n| --- | --- |\n| ايجار | ٥٠٠ |")

    def test_safe_edits_applied(self):
        res = corrector.correct(self.RAW, lambda t: reply(("بتاريح", "بتاريخ"), ("الدعوي", "الدعوى")))
        self.assertTrue(res.ok)
        self.assertEqual(res.applied, 2)
        self.assertIn("بتاريخ", res.text)
        self.assertIn("الدعوى رقم", res.text)

    def test_number_edit_rejected(self):
        res = corrector.correct(self.RAW, lambda t: reply(("١٬٢٥٠", "١٬٢٠٠"), ("٤٥٦/٢٠٢٣", "٤٥٦/٢٠٢٤")))
        self.assertEqual(res.applied, 0)
        self.assertTrue(all(e.why_rejected == "يمس رقماً" for e in res.edits))
        self.assertIn("١٬٢٥٠", res.text)

    def test_digit_conversion_is_not_an_edit(self):
        res = corrector.correct(self.RAW, lambda t: reply(("١٥/٣/٢٠٢٤", "15/3/2024")))
        self.assertEqual(res.applied, 0)
        self.assertEqual(res.edits[0].why_rejected, "لا تغيير (أرقام فقط)")

    def test_name_edit_rejected(self):
        res = corrector.correct(self.RAW, lambda t: reply(("الكندري", "الكندرى"), ("محكمة الاستئناف", "محكمه الاستئناف")))
        self.assertEqual(res.applied, 0)
        self.assertTrue(all("اسم" in e.why_rejected for e in res.edits), [e.why_rejected for e in res.edits])

    def test_marker_and_table_protected(self):
        res = corrector.correct(self.RAW, lambda t: reply(("<!-- page:3 -->", "<!-- page:4 -->"), ("| ايجار", "ايجار")))
        self.assertEqual(res.applied, 0)

    def test_long_rewrite_rejected(self):
        res = corrector.correct(self.RAW, lambda t: reply(("امام", "أمام هيئة موقرة جداً من القضاة")))
        self.assertEqual(res.applied, 0)

    def test_doubt_brackets_allowed(self):
        res = corrector.correct(self.RAW, lambda t: reply(("الكندري", "[الكندري]")))
        self.assertEqual(res.applied, 1)

    def test_whole_words_only(self):
        # حالة حقيقية: "مر" ← "ص.ب" أفسدت "المرقاب" و"أمريكي"، و"تا" ← "ت" أفسدت "ومائتان"
        raw = "<!-- page:1 -->\nالمرقاب دولار أمريكي ومائتان - مر ١٤٧٧٦ تا ٢٢٤٢\n" + "نص عادي للحشو " * 20
        res = corrector.correct(raw, lambda t: reply(("مر", "ص.ب"), ("تا", "ت")))
        line = res.text.splitlines()[1]
        self.assertEqual(line, "المرقاب دولار أمريكي ومائتان - ص.ب ١٤٧٧٦ ت ٢٢٤٢")

    def test_bracket_guess_rejected(self):
        res = corrector.correct(self.RAW, lambda t: reply(("امام", "[أمام]")))
        self.assertEqual(res.applied, 0)
        self.assertIn("تخمين", res.edits[0].why_rejected)

    def test_invalid_json_retried_then_raw(self):
        calls = []
        res = corrector.correct(self.RAW, lambda t: calls.append(1) or "ليس JSON")
        self.assertEqual(len(calls), 2)
        self.assertFalse(res.ok)
        self.assertEqual(res.text, self.RAW)

    def test_completeness_failure(self):
        self.assertIn("علامات", corrector.completeness_problem("<!-- page:1 -->\nنص", "نص"))
        self.assertIn("الطول", corrector.completeness_problem("<!-- page:1 -->\n" + "ا" * 100,
                                                            "<!-- page:1 -->\n" + "ا" * 40))

    def test_report_lists_applied_and_rejected(self):
        res = corrector.correct(self.RAW, lambda t: reply(("بتاريح", "بتاريخ"), ("١٬٢٥٠", "١٬٢٠٠")))
        md = corrector.render_report("x.pdf", {3: {"edits": [e.to_dict() for e in res.edits], "ok": True}})
        self.assertIn("مطبّق: 1 — مرفوض: 1", md)
        self.assertIn("يمس رقماً", md)


# ---------------------------------------------------------------- (ب-1) OCR.space
class OcrSpaceTests(unittest.TestCase):
    def test_compress_under_1mb(self):
        rnd = random.Random(1)
        img = Image.frombytes("RGB", (2400, 3200), bytes(rnd.getrandbits(8) for _ in range(2400 * 3200 * 3)))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        self.assertGreater(len(buf.getvalue()), MAX_BYTES)
        out = fit_jpeg(buf.getvalue())
        self.assertLessEqual(len(out), MAX_BYTES)
        self.assertEqual(out[:3], b"\xff\xd8\xff")

    def test_small_jpeg_unchanged(self):
        buf = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(buf, "JPEG")
        self.assertEqual(fit_jpeg(buf.getvalue()), buf.getvalue())

    def test_classify(self):
        self.assertIsNone(classify(200, ok_payload("x"), ""))
        q = classify(200, error_payload("You may only perform this action upto maximum 500 number of times within 86400 seconds"), "")
        self.assertIsInstance(q, QuotaExhausted)

    def test_request_shape_and_header_key(self):
        fake = FakeOcrSpace(lambda i, f: (200, ok_payload("نص ٣")))
        try:
            p = OcrSpaceProvider("ocrspace", OCR_KEY, 3, endpoint=fake.url)
            from app.cloud_gate import _TOKEN
            buf = io.BytesIO()
            Image.new("RGB", (200, 200), "white").save(buf, "JPEG")
            out = p.send("", OcrRequest([PageImage(7, buf.getvalue(), 72, "image/jpeg", 0.1)]), _TOKEN)
            self.assertEqual(out, "<!-- page:7 -->\nنص ٣")       # الخام لا يُحوَّل
            c = fake.calls[0]
            self.assertEqual(c["apikey"], OCR_KEY)
            self.assertEqual(c["form"]["language"], "ara")
            self.assertEqual(c["form"]["OCREngine"], "3")
            self.assertTrue(c["form"]["base64Image"].startswith("data:image/jpeg;base64,"))
            self.assertNotIn(OCR_KEY, json.dumps(c["form"]))
        finally:
            fake.close()

    def test_catalog_entry(self):
        spec = next(s for s in load_catalog() if s.id == "ocrspace")
        self.assertTrue(spec.is_ocr)
        self.assertEqual(spec.rpd, 500)
        self.assertEqual(spec.rpmonth, 25000)

    def test_monthly_quota(self):
        u = UsageCounter(Path(tempfile.mkdtemp()) / "u.json", today=lambda: "2026-10-09")
        lim = RateLimiter("ocrspace", 1000, u, rpd=None, rpmonth=2, sleep=lambda s: None)
        lim.acquire()
        lim.acquire()
        with self.assertRaises(QuotaExhausted):
            lim.acquire()
        u2 = UsageCounter(u.path, today=lambda: "2026-11-01")   # شهر جديد
        RateLimiter("ocrspace", 1000, u2, rpmonth=2).acquire()


# ---------------------------------------------------------------- السلسلة كاملة
class ChainEnv(Env):
    def ocr_slot(self, behavior, rpd=None) -> Slot:
        fake = FakeOcrSpace(behavior)
        self.servers.append(fake)
        spec = ProviderSpec(id="ocrspace", name="OCR.space", kind="ocr", base_url=fake.url,
                            free_tier={"rpd": rpd, "engine3_monthly": 1000})
        prov = OcrSpaceProvider("ocrspace", OCR_KEY, 3, endpoint=fake.url)
        lim = RateLimiter("ocrspace", 1000, self.usage, rpd=rpd, sleep=self.sleeps.append)
        s = Slot(spec, ProviderState(enabled=True, model="engine3"), prov, lim)
        s.fake = fake
        return s


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.env = ChainEnv()
        register_secret(OCR_KEY)
        register_secret(SECRET)

    def tearDown(self):
        self.env.close()

    def _llm(self):
        def behave(pages, i):
            if pages:   # طلب رؤية عادي
                from fake_openai_server import good_text
                return 200, {}, good_text(pages)
            return 200, {}, reply(("تاجلت", "تأجلت"), ("١٬٢٥٠٫٥٠٠", "١٬٢٠٠٫٥٠٠"))
        return self.env.slot("llm", behave)

    def test_ocr_then_corrector(self):
        pdf = self.env.pdf("memo.pdf", 2)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
        llm = self._llm()
        logs = io.StringIO()
        h = logging.StreamHandler(logs)
        h.addFilter(RedactingFilter())
        logging.getLogger().addHandler(h)
        try:
            self.env.run([pdf], [ocr, llm])
        finally:
            logging.getLogger().removeHandler(h)
        st = self.env.state(pdf)
        self.assertEqual(st.status(1), cp.DONE)
        self.assertEqual(st.page(1)["edits_applied"], 1)
        self.assertEqual(st.page(1)["edits_rejected"], 1)
        raw = (st.pages_dir / "p0001.raw.md").read_text(encoding="utf-8")
        self.assertIn("١٬٢٥٠٫٥٠٠", raw)                      # الخام لا يُحوَّل
        md = (self.env.out / "docs" / "memo.md").read_text(encoding="utf-8")
        self.assertIn("1,250.500 د.ك", md)                    # المبلغ لم يتغير وتحوّل للإنجليزية
        self.assertIn("تأجلت", md)
        self.assertNotRegex(md, "[٠-٩]")
        rep = (self.env.out / "corrections" / "memo.md").read_text(encoding="utf-8")
        self.assertIn("يمس رقماً", rep)
        # المفتاح لا يظهر في أي سجل أو ملف
        dumped = logs.getvalue() + "".join(str(e) for e in self.env.events)
        for f in self.env.tmp.rglob("*"):
            if f.is_file() and f.suffix in (".md", ".json", ".csv", ".log"):
                dumped += f.read_text(encoding="utf-8", errors="ignore")
        self.assertNotIn(OCR_KEY, dumped)
        self.assertNotIn(SECRET, dumped)

    def test_quota_moves_to_next_provider(self):
        pdf = self.env.pdf("q.pdf", 3)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)), rpd=1)
        llm = self._llm()
        self.env.run([pdf], [ocr, llm])
        st = self.env.state(pdf)
        self.assertEqual(st.page(1)["method"], "api:ocrspace")
        self.assertEqual(st.page(2)["method"], "api:llm")          # أكملها نموذج الرؤية
        self.assertEqual(st.page(3)["method"], "api:llm")

    def test_quota_with_nothing_else_stops_for_resume(self):
        pdf = self.env.pdf("s.pdf", 2)
        ocr = self.env.ocr_slot(lambda i, f: (200, error_payload("maximum 500 number of times")))
        ctx = self.env.run([pdf], [ocr])
        self.assertTrue(ctx.api.report.quota_stopped)
        self.assertNotEqual(self.env.state(pdf).status(1), cp.DONE)

    def test_corrector_failure_keeps_raw_and_flags(self):
        pdf = self.env.pdf("f.pdf", 1)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
        llm = self.env.slot("llm", lambda pages, i: (200, {}, "لا أستطيع"))
        self.env.run([pdf], [ocr, llm])
        st = self.env.state(pdf)
        self.assertEqual(st.status(1), cp.DONE)
        self.assertEqual(st.page(1)["review_reason"], "corrector_failed")
        md = (self.env.out / "docs" / "f.md").read_text(encoding="utf-8")
        self.assertIn("check:corrector_failed", md)

    def test_hard_page_sends_image_with_text(self):
        pdf = self.env.pdf("h.pdf", 1)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("ا")))   # قصير جداً ⇒ صفحة صعبة
        llm = self.env.slot("llm", lambda pages, i: (200, {}, reply()))
        self.env.run([pdf], [ocr, llm])
        self.assertEqual(llm.fake.calls[0]["images"], 1)
        self.assertTrue(self.env.state(pdf).page(1)["with_image"])

    def test_empty_engine3_retried_with_engine2(self):
        pdf = self.env.pdf("e.pdf", 1)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload("" if f["OCREngine"] == "3" else ARABIC_PAGE)))
        self.env.run([pdf], [ocr])
        self.assertEqual([c["form"]["OCREngine"] for c in ocr.fake.calls], ["3", "2"])
        self.assertEqual(self.env.state(pdf).status(1), cp.DONE)
        self.assertEqual(ocr.provider.engine, 3)                 # يعود للافتراضي بعد الصفحة

    def test_engine3_monthly_limit_switches_to_2(self):
        pdf = self.env.pdf("m.pdf", 1)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
        ocr.spec.free_tier["engine3_monthly"] = 1
        self.env.usage.add_month("ocrspace:e3")
        self.env.run([pdf], [ocr])
        self.assertEqual([c["form"]["OCREngine"] for c in ocr.fake.calls], ["2"])

    def test_engine1_never_used(self):
        self.assertEqual(OcrSpaceProvider("x", OCR_KEY, 1, endpoint="https://example.invalid").engine, 3)

    def test_vision_mode_skips_ocr(self):
        pdf = self.env.pdf("v.pdf", 1)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
        llm = self._llm()
        jobs_settings = self.env.settings()
        jobs_settings.ocr_engine = "vision"
        from app.pipeline import Job, run_batch
        ctx = self.env.ctx([ocr, llm], jobs_settings)
        run_batch([Job(pdf, self.env.hash_cache.get(pdf))], ctx)
        self.assertEqual(ocr.fake.calls, [])
        self.assertEqual(self.env.state(pdf).page(1)["method"], "api:llm")


if __name__ == "__main__":
    unittest.main()


class LayoutTests(unittest.TestCase):
    def test_markdown_beside_pdf_and_work_files_elsewhere(self):
        env = ChainEnv()
        try:
            src = Path(tempfile.mkdtemp(prefix="pdf2md-src-"))
            pdf = make_scanned(src / "memo.pdf")
            ocr = env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
            from app.pipeline import Job, run_batch
            ctx = env.ctx([ocr], env.settings())
            ctx.beside_source = True
            ctx.api.md_dir_for = ctx.md_dir_for
            run_batch([Job(pdf, env.hash_cache.get(pdf))], ctx)
            self.assertEqual(sorted(p.name for p in src.iterdir()), ["memo.md", "memo.pdf"])
            self.assertTrue((env.out / ".state").is_dir())
        finally:
            env.close()

    def test_cleanup_old_work_files(self):
        import time
        from app.config import cleanup_work_dir, work_dir
        os.environ["PDF2MD_WORK_DIR"] = tempfile.mkdtemp(prefix="pdf2md-work-")
        try:
            old, new = work_dir() / ".state" / "a" / "p0001.md", work_dir() / ".state" / "b.json"
            for f in (old, new):
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x", encoding="utf-8")
            t = time.time() - 40 * 86400
            os.utime(old, (t, t))
            self.assertEqual(cleanup_work_dir(30), 1)
            self.assertFalse(old.parent.exists())
            self.assertTrue(new.exists())
        finally:
            del os.environ["PDF2MD_WORK_DIR"]


def make_scanned(path: Path) -> Path:
    from test_cloud import make_scanned_pdf
    return make_scanned_pdf(path, 1)


class FurnitureTests(unittest.TestCase):
    FOOT_AR = "المرقاب - شارع أحمد الجابر - برج المجموعة - ص.ب: 14776 الفيحاء الرمز البريدي 72858 ت: 22422020"
    FOOT_EN = "Murqap-Ahmad Al-Jaber St. Al Majmoo'a Tower 8th floor P.O.Box 14776 Faiha Post Code 72858"

    def _pages(self):
        body = "إن العلاقة بين المدعية والمدعى عليها هي علاقة تعاقدية يحكمها عقد الاتفاق المبرم بينهما"
        return {1: f"- 1 -\n{body}\nوكيل المدعية\nخالد حسن الجمعة\nشخصه\n{self.FOOT_AR}\n{self.FOOT_EN}",
                2: f"-2-\n{body} ثانياً\nضد\n{self.FOOT_AR.replace('الجابر', 'الجاير')}\nMurqap-Ahmad Allaber St\n"
                   f"{self.FOOT_EN.replace('Tower', 'Tawer')}",
                3: f"{body} ثالثاً\n| البند | المبلغ |\n| --- | --- |\n| إيجار | 500 |"}

    def test_footer_and_page_numbers_removed(self):
        from app.cleanup import strip_page_furniture
        out = strip_page_furniture(self._pages())
        for n in (1, 2):
            self.assertNotIn("الرمز البريدي", out[n])
            self.assertNotIn("Murqap", out[n])
            self.assertFalse(out[n].startswith("-"))
        self.assertIn("خالد حسن الجمعة", out[1])            # الاسم يبقى
        self.assertIn("ضد", out[2])                          # كلمة قصيرة حقيقية تبقى
        self.assertTrue(out[3].endswith("| إيجار | 500 |"))  # الجدول لا يُمس

    def test_noise_lines_from_corrector(self):
        raw = "<!-- page:5 -->\nوكيل المدعية\nخالد حسن الجمعة\nشخصه\n" + "نص عادي للحشو " * 20
        res = corrector.correct(raw, lambda t: json.dumps(
            {"edits": [], "noise": ["شخصه", "خالد حسن الجمعة"]}, ensure_ascii=False))
        self.assertNotIn("شخصه", res.text)
        self.assertIn("خالد حسن الجمعة", res.text)
        self.assertEqual([e.applied for e in res.edits], [True, False])


class RangeAndBatchTests(unittest.TestCase):
    def setUp(self):
        self.env = ChainEnv()

    def tearDown(self):
        self.env.close()

    def test_parse_page_range(self):
        from app.file_options import parse_page_range
        self.assertIsNone(parse_page_range("", 10))
        self.assertEqual(parse_page_range("1-5", 10), [1, 2, 3, 4, 5])
        self.assertEqual(parse_page_range("١,٣، 7-20", 9), [1, 3, 7, 8, 9])
        with self.assertRaises(ValueError):
            parse_page_range("5-2", 10)

    def test_only_selected_pages_and_one_correction_per_3_pages(self):
        pdf = self.env.pdf("r.pdf", 8)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
        llm = self.env.slot("llm", lambda pages, i: (200, {}, reply(("تاجلت", "تأجلت"))))
        from app.pipeline import Job, run_batch
        ctx = self.env.ctx([ocr, llm], self.env.settings())
        run_batch([Job(pdf, self.env.hash_cache.get(pdf), pages=[1, 2, 3, 4, 5])], ctx)
        st = self.env.state(pdf)
        self.assertEqual(len(ocr.fake.calls), 5)
        self.assertEqual(len(llm.fake.calls), 2)          # ص1-3 في طلب، ص4-5 في طلب
        self.assertTrue(all(st.status(n) == cp.DONE for n in range(1, 6)))
        self.assertIsNone(st.status(6))
        md = (self.env.out / "docs" / "r.md").read_text(encoding="utf-8")
        self.assertIn("<!-- page:5 -->", md)
        self.assertNotIn("<!-- page:6 -->", md)
        self.assertEqual(md.count("تأجلت"), 5)

    def test_pause_then_resume(self):
        import threading
        pdf = self.env.pdf("p.pdf", 3)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
        from app.pipeline import Job, run_batch
        ctx = self.env.ctx([ocr], self.env.settings())
        ctx.pause_event.set()
        ctx.api.pause = ctx.pause_event
        t = threading.Thread(target=run_batch, args=([Job(pdf, self.env.hash_cache.get(pdf))], ctx))
        t.start()
        t.join(1.0)
        self.assertTrue(t.is_alive())
        self.assertEqual(ocr.fake.calls, [])               # متوقف: لا طلبات
        ctx.pause_event.clear()
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(ocr.fake.calls), 3)


def make_text_pdf_with_photo(path: Path) -> Path:
    """صفحة نصية (طبقة نص) فيها صورة مضمّنة بحجم متوسط."""
    import pymupdf as fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for i in range(12):
        page.insert_text((50, 60 + i * 18), f"Contract clause number {i} text line for the layer", fontsize=11)
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (40, 160, 90)).save(buf, "PNG")
    page.insert_image(fitz.Rect(100, 400, 400, 625), stream=buf.getvalue())
    doc.save(path)
    doc.close()
    return path


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.env = ChainEnv()

    def tearDown(self):
        self.env.close()

    def test_find_photos_ignores_full_page_scans(self):
        import pymupdf as fitz
        from app.images import find_photos
        with fitz.open(make_text_pdf_with_photo(self.env.tmp / "ph.pdf")) as d:
            self.assertEqual(len(find_photos(d[0])), 1)
        with fitz.open(self.env.pdf("scan.pdf", 1)) as d:
            self.assertEqual(find_photos(d[0]), [])

    def test_local_placeholder_and_cloud_description(self):
        pdf = make_text_pdf_with_photo(self.env.tmp / "ph.pdf")
        from app.pipeline import Job, run_batch
        # بلا مزوّد: علامة [صورة]
        from app.pipeline import BatchContext
        import threading
        ctx = BatchContext(self.env.out, self.env.settings(), threading.Event(), lambda *a, **k: None,
                           self.env.hash_cache)
        run_batch([Job(pdf, self.env.hash_cache.get(pdf))], ctx)
        md = (self.env.out / "docs" / "ph.md").read_text(encoding="utf-8")
        self.assertIn("> [صورة]", md)
        # عبر المواقع: وصف ومحادثة بمتحدثَين
        chat = "[صورة: محادثة بين صاحب الهاتف وأحمد]\n**أحمد:** متى الدفع؟ (10:15)\n**صاحب الهاتف:** غداً"
        llm = self.env.slot("llm", lambda pages, i: (200, {}, chat))
        ctx = self.env.ctx([llm], self.env.settings())
        run_batch([Job(pdf, self.env.hash_cache.get(pdf))], ctx)
        self.assertEqual(llm.fake.calls[0]["images"], 1)
        md = (self.env.out / "docs" / "ph.md").read_text(encoding="utf-8")
        self.assertIn("> **أحمد:** متى الدفع؟ (10:15)", md)
        self.assertNotIn("> [صورة]\n", md)
        run_batch([Job(pdf, self.env.hash_cache.get(pdf))], ctx)   # لا يُعاد الوصف
        self.assertEqual(len(llm.fake.calls), 1)

    def test_chat_document_skips_ocr(self):
        pdf = self.env.pdf("chat.pdf", 2)
        ocr = self.env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
        llm = self._llm_vision()
        from app.pipeline import Job, run_batch
        ctx = self.env.ctx([ocr, llm], self.env.settings())
        run_batch([Job(pdf, self.env.hash_cache.get(pdf), chat=True)], ctx)
        self.assertEqual(ocr.fake.calls, [])
        self.assertEqual(self.env.state(pdf).page(1)["method"], "api:llm")

    def _llm_vision(self):
        from fake_openai_server import good_text
        return self.env.slot("llm", lambda pages, i: (200, {}, good_text(pages)))


class ScannedPiecesTests(unittest.TestCase):
    def test_scanned_page_made_of_image_pieces_gets_no_photo_blocks(self):
        # حالة حقيقية (محضر ميدان الرماية): صفحة ممسوحة مخزّنة كقطع صور كبيرة، فوُصفت القطع
        # كـ"صور" وتكرر نص الصفحة مرتين في الملف
        import threading
        import pymupdf as fitz
        from app.pipeline import BatchContext, Job, run_batch
        env = ChainEnv()
        try:
            path = env.tmp / "pieces.pdf"
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            buf = io.BytesIO()
            Image.new("RGB", (500, 400), (230, 230, 230)).save(buf, "PNG")
            page.insert_image(fitz.Rect(60, 200, 520, 600), stream=buf.getvalue())   # ~37% من الصفحة، بلا نص
            doc.save(path)
            doc.close()
            ctx = BatchContext(env.out, env.settings(), threading.Event(), lambda *a, **k: None, env.hash_cache)
            run_batch([Job(path, env.hash_cache.get(path))], ctx)
            self.assertFalse((env.state(path).page(1) or {}).get("photos"))
        finally:
            env.close()


class VisionFullTests(unittest.TestCase):
    def setUp(self):
        self.env = ChainEnv()

    def tearDown(self):
        self.env.close()

    def test_table_grid_detected(self):
        from PIL import ImageDraw
        from app.layout import has_table
        img = Image.new("RGB", (1000, 1400), "white")
        d = ImageDraw.Draw(img)
        for y in range(300, 900, 100):
            d.line((100, y, 900, y), fill="black", width=3)
        for x in (100, 400, 650, 900):
            d.line((x, 300, x, 800), fill="black", width=3)
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        self.assertTrue(has_table(buf.getvalue()))
        blank = io.BytesIO()
        Image.new("RGB", (1000, 1400), "white").save(blank, "JPEG")
        self.assertFalse(has_table(blank.getvalue()))

    def test_unverified_numbers(self):
        from app.layout import unverified_numbers
        self.assertEqual(unverified_numbers("المبلغ 43000 والبند 140A و 5", "المبلغ ٤٣٠٠٠ والبند 140A"), [])
        self.assertEqual(unverified_numbers("قيمة 13000 د.ك", "قيمة 43000 د.ك"), ["13000"])

    def test_rotated_page_read_fully_by_vision_and_numbers_checked(self):
        pdf = self.env.pdf("rot.pdf", 1)
        payload = ok_payload(ARABIC_PAGE)
        payload["ParsedResults"][0]["TextOrientation"] = "90"
        ocr = self.env.ocr_slot(lambda i, f: (200, payload))
        vision = "<!-- page:1 -->\n| البند | المبلغ |\n| --- | --- |\n| تجاري 345/2024 | 99999 |\n" + ARABIC_PAGE
        llm = self.env.slot("llm", lambda pages, i: (200, {}, vision))
        self.env.run([pdf], [ocr, llm])
        st = self.env.state(pdf)
        self.assertEqual(st.page(1)["vision_full"], "rotated")
        self.assertEqual(st.page(1)["method"], "api:llm")
        self.assertEqual(st.page(1)["unverified_numbers"], ["99999"])   # 345 و2024 موجودان في OCR
        self.assertEqual(llm.fake.calls[0]["images"], 1)
        md = (self.env.out / "docs" / "rot.md").read_text(encoding="utf-8")
        self.assertIn("check:numbers_unverified 99999", md)
        self.assertNotIn("orientation", md)


class ResetTests(unittest.TestCase):
    def test_reset_file_forgets_progress(self):
        env = ChainEnv()
        try:
            pdf = env.pdf("again.pdf", 1)
            ocr = env.ocr_slot(lambda i, f: (200, ok_payload(ARABIC_PAGE)))
            env.run([pdf], [ocr])
            env.run([pdf], [ocr])
            self.assertEqual(len(ocr.fake.calls), 1)          # المرة الثانية من الحفظ، بلا إرسال
            from app.checkpoint import reset_file
            self.assertTrue(reset_file(env.out / ".state", env.hash_cache.get(pdf)))
            env.run([pdf], [ocr])
            self.assertEqual(len(ocr.fake.calls), 2)          # بعد الإعادة: يُقرأ من جديد
            self.assertTrue(pdf.exists())
        finally:
            env.close()
