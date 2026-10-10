"""اختبارات المرحلة 4: الجداول، مؤشرات الصفحة الضعيفة، القاموس، الوقائع، الفهرس.

python -m unittest tests.test_phase4
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))

import pymupdf as fitz  # noqa: E402

from app import checkpoint as cp  # noqa: E402
from app.audit_log import AuditLog  # noqa: E402
from app.cloud_gate import CLOUD, LOCAL, CloudBlockedError, CloudGate  # noqa: E402
from app.config import Settings, app_data_dir  # noqa: E402
from app.dictionary import SEED, ArabicDictionary  # noqa: E402
from app.facts import extract_facts, find_amounts, find_case_numbers, find_dates, normalize_digits  # noqa: E402
from app.hashing import HashCache, sha256_file  # noqa: E402
from app.index_builder import INDEX_NAME, build_index  # noqa: E402
from app.page_score import bracket_marks, score_text  # noqa: E402
from app.pdf_io import page_text_to_markdown  # noqa: E402
from app.pipeline import BatchContext, Job, finalize_file, run_batch  # noqa: E402
from app.providers.base import ExternalProvider, PageImage  # noqa: E402
from app.review_report import REPORT_NAME  # noqa: E402
from app.summarize import SOURCE_API, ApiSummarizer, clean_pages, extractive_summary  # noqa: E402
from app.tables import MISMATCH_TAG, TABLE_CONT, _visual_order, extract_tables, is_plausible, merge_tables, split_row  # noqa: E402
from make_sample_pdf import TABLE_HEADER, build_tables  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pdf2md-p4-"))
TABLES_PDF = build_tables(TMP / "tables.pdf")


def _table_lines(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.startswith("|")]


# ====================================================================== الجداول من طبقة النص
class TextLayerTableTests(unittest.TestCase):
    def setUp(self):
        self.doc = fitz.open(TABLES_PDF)

    def tearDown(self):
        self.doc.close()

    def test_arabic_table_columns_follow_visual_rtl_order(self):
        md = page_text_to_markdown(self.doc[0])
        rows = [split_row(ln) for ln in _table_lines(md)]
        # أول عمود في Markdown = العمود الأيمن في الـ PDF ("م")
        self.assertEqual(rows[0], TABLE_HEADER)
        self.assertEqual(rows[2], ["۱", "قسط الإيجار الأول", "2025/03/16", "1,250.500"])

    def test_rightmost_pdf_column_is_first(self):
        t = extract_tables(self.doc[0])[0]
        raw = self.doc[0].find_tables().tables[0]
        rightmost = max(raw.rows[0].cells, key=lambda c: c[0])
        self.assertTrue(t.rtl)
        self.assertEqual(t.rows[0][0], "م")
        self.assertGreater(rightmost[0], min(c[0] for c in raw.rows[0].cells))

    def test_ltr_order_when_disabled(self):
        t = extract_tables(self.doc[0], rtl_columns=False)[0]
        self.assertEqual(t.rows[0], list(reversed(TABLE_HEADER)))

    def test_numbers_untouched(self):
        md = page_text_to_markdown(self.doc[0])
        for s in ("1,250.500", "75.250", "2025/03/16", "15/04/2025", "۳۰۰"):
            self.assertIn(s, md)

    def test_text_outside_table_kept_and_not_duplicated(self):
        md = page_text_to_markdown(self.doc[0])
        self.assertIn("كشف الدفعات المستحقة على المستأجر", md.splitlines()[0])
        self.assertEqual(md.count("قسط الإيجار الأول"), 1)

    def test_plain_text_when_no_table(self):
        doc = fitz.open()
        p = doc.new_page()
        p.insert_text((60, 100), "Just a paragraph with no table.", fontsize=12)
        p.draw_rect(fitz.Rect(50, 80, 400, 120))     # إطار حول فقرة ليس جدولاً
        md = page_text_to_markdown(p)
        self.assertNotIn("|", md)
        self.assertIn("Just a paragraph", md)

    def test_plausibility(self):
        self.assertTrue(is_plausible([["أ", "ب"], ["1", "2"]]))
        self.assertFalse(is_plausible([["أ", "ب"], ["1"]]))                 # أعمدة غير متسقة
        self.assertFalse(is_plausible([["أ", ""], ["1", ""]]))               # عمود فارغ
        self.assertFalse(is_plausible([["أ", "ب", "ج"], ["", "", "x"], ["", "", ""]]))  # خلايا فارغة كثيرة
        self.assertFalse(is_plausible([["أ", "ب"]]))                          # صف واحد

    def test_visual_order_detector(self):
        def chars(xs):
            return [(fitz.Rect(x, 0, x + 4, 10), "ب") for x in xs]
        self.assertTrue(_visual_order(chars([10, 15, 20, 25])))    # عربي مخزّن من اليسار
        self.assertFalse(_visual_order(chars([25, 20, 15, 10])))   # الترتيب المنطقي السليم


# ====================================================================== الدمج بين الصفحات
TABLE_A = "| م | البيان | المبلغ |\n| --- | --- | --- |\n| 1 | أول | 100 |\n| 2 | ثان | 200 |"


class MergeTests(unittest.TestCase):
    def test_ocr_continuation_same_columns(self):
        p2 = f"{TABLE_CONT}\n| 3 | ثالث | 300 |\n| 4 | رابع | 400 |\n\nنص بعد الجدول"
        out, st = merge_tables([(1, "مقدمة\n\n" + TABLE_A, "api:x"), (2, p2, "api:x")])
        rows = _table_lines(out[1])
        self.assertEqual(len(rows), 6)
        self.assertEqual(split_row(rows[-1]), ["3", "ثالث", "300"][:0] + ["4", "رابع", "400"])
        self.assertNotIn("|", out[2])
        self.assertIn("table-continued-from:1", out[2])
        self.assertIn("نص بعد الجدول", out[2])
        self.assertEqual((st.merged, st.mismatched), (1, 0))

    def test_repeated_header_dropped(self):
        p2 = f"{TABLE_CONT}\n| م | البيان | المبلغ |\n| --- | --- | --- |\n| 3 | ثالث | 300 |"
        out, st = merge_tables([(1, TABLE_A, "ocr:local"), (2, p2, "ocr:local")])
        self.assertEqual(out[1].count("| م | البيان | المبلغ |"), 1)
        self.assertEqual(len(_table_lines(out[1])), 5)

    def test_mismatch_not_merged_and_flagged(self):
        p2 = f"{TABLE_CONT}\n| 3 | ثالث | 300 | زيادة |"
        out, st = merge_tables([(1, TABLE_A, "api:x"), (2, p2, "api:x")])
        self.assertEqual(len(_table_lines(out[1])), 4)                 # لم يتغير
        self.assertIn(MISMATCH_TAG, out[2])
        self.assertIn("| 3 | ثالث | 300 | زيادة |", out[2])              # البيانات باقية مكانها
        self.assertEqual((st.merged, st.mismatched, st.mismatch_pages), (0, 1, [2]))

    def test_no_marker_ocr_not_merged(self):
        out, st = merge_tables([(1, TABLE_A, "api:x"), (2, "| 3 | ثالث | 300 |", "api:x")])
        self.assertEqual(st.merged, 0)
        self.assertEqual(st.mismatched, 0)

    def test_three_page_chain(self):
        p2 = f"{TABLE_CONT}\n| 3 | ثالث | 300 |"
        p3 = f"{TABLE_CONT}\n| 4 | رابع | 400 |\n\nالخاتمة"
        out, st = merge_tables([(1, TABLE_A, "api:x"), (2, p2, "api:x"), (3, p3, "api:x")])
        self.assertEqual(len(_table_lines(out[1])), 6)
        self.assertIn("table-continued-from:1", out[3])
        self.assertEqual(st.merged, 2)

    def test_gap_page_breaks_chain(self):
        p3 = f"{TABLE_CONT}\n| 3 | ثالث | 300 |"
        out, st = merge_tables([(1, TABLE_A, "api:x"), (2, "", ""), (3, p3, "api:x")])
        self.assertEqual(st.merged, 0)

    def test_page_number_after_table_ignored(self):
        p2 = f"{TABLE_CONT}\n| 3 | ثالث | 300 |"
        out, st = merge_tables([(1, TABLE_A + "\n\n- 1 -", "api:x"), (2, p2, "api:x")])
        self.assertEqual(st.merged, 1)


class PipelineTableTests(unittest.TestCase):
    """جدول ممتد من طبقة النص عبر الدفعة كاملة (بلا محرّك)."""

    def test_text_layer_table_spanning_two_pages(self):
        out = TMP / "out_tables"
        events = []
        ctx = BatchContext(output_dir=out, settings=Settings(), stop_event=threading.Event(),
                           emit=lambda k, **kw: events.append((k, kw)), hash_cache=HashCache(TMP / "hc.json"))
        run_batch([Job(TABLES_PDF, ctx.hash_cache.get(TABLES_PDF))], ctx)
        done = [kw for k, kw in events if k == "file_done"][0]
        self.assertEqual(done["tables"]["detected"], 3)
        self.assertEqual(done["tables"]["merged"], 1)
        self.assertEqual(done["tables"]["mismatched"], 0)   # جدول الصفحة 3 مختلف بلا علامة: لا تعليم
        md = (out / "docs" / "tables.md").read_text(encoding="utf-8")
        p1 = md.split("<!-- page:2 -->")[0]
        self.assertEqual(len(_table_lines(p1)), 2 + 3 + 2)  # ترويسة + فاصل + 3 + 2 صفوف
        self.assertEqual(p1.count("| م | البيان |"), 1)
        self.assertIn("| 5 | تأمين مسترد | 2025/07/01 | 500 |", p1)
        self.assertIn("وقد تم سداد جميع الدفعات", md.split("<!-- page:2 -->")[1])
        # أجزاء الصفحات الأصلية لا تُعدَّل (الدمج عند التجميع فقط)
        st = cp.FileState(out / ".state", ctx.hash_cache.get(TABLES_PDF), "tables.pdf", 3)
        self.assertIn("| م | البيان |", st.read_fragment(2))


# ====================================================================== مؤشرات الجودة
CLEAN = ("إنه في يوم الأحد الموافق تم الاتفاق بين الطرف الأول والطرف الثاني على أن يؤجر الطرف الأول "
         "للطرف الثاني الشقة الكائنة في شارع النخيل مقابل مبلغ شهري يدفع مقدماً في بداية كل شهر. ") * 3


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings()

    def test_clean_page_not_weak(self):
        for method in ("text_layer", "ocr:local", "api:gemini"):
            r = score_text(CLEAN, method, self.s, None, ink=0.05)
            self.assertFalse(r.weak, (method, r))
            self.assertGreaterEqual(r.score, 0.9)

    def test_brackets_flagged(self):
        bad = CLEAN + " [غير واضح] الطرف [؟] الثاني [غير مقروء] "
        r = score_text(bad, "api:x", self.s, None)
        self.assertTrue(r.weak)
        self.assertIn("brackets", r.reasons)

    def test_markdown_brackets_ignored(self):
        md = CLEAN + "\n- [x] بند منجز\n- [ ] بند معلق\nانظر [الموقع](https://example.com) والحاشية[^1]\n[1]: مرجع"
        self.assertEqual(bracket_marks(md), 0)
        self.assertFalse(score_text(md, "api:x", self.s).weak)

    def test_repetition_flagged(self):
        r = score_text("الفقرة الأولى " + "م.م. " * 40, "ocr:local", self.s)
        self.assertTrue(r.weak)
        self.assertIn("repetition", r.reasons)
        self.assertEqual(r.score, 0.0)

    def test_short_vs_ink(self):
        r = score_text("سطر واحد", "ocr:local", self.s, ink=0.08)
        self.assertIn("too_short", r.reasons)
        self.assertFalse(score_text("سطر واحد", "text_layer", self.s, ink=0.08).weak)  # لا يُطبّق على طبقة النص

    def test_unknown_words_with_dictionary(self):
        d = ArabicDictionary(TMP / "dict_score.txt")
        d.add_pages([("k:1", CLEAN)])
        s = Settings(dict_min_words=5)
        self.assertFalse(score_text(CLEAN, "api:x", s, d).weak)
        garbage = " ".join(["ضصثق غعفقث طظزوة ذدجحخ هشسيب"] * 10)
        r = score_text(garbage, "api:x", s, d)
        self.assertIn("unknown_words", r.reasons)
        # قاموس صغير (أقل من الحد) ⇒ المؤشر لا يُطبّق
        self.assertNotIn("unknown_words", score_text(garbage, "api:x", Settings(dict_min_words=10**6), d).reasons)

    def test_thresholds_configurable(self):
        bad = CLEAN + " [؟] [؟] "
        self.assertTrue(score_text(bad, "api:x", self.s).weak)
        self.assertFalse(score_text(bad, "api:x", Settings(bracket_min_count=5)).weak)


# ====================================================================== القاموس
class DictionaryTests(unittest.TestCase):
    def test_build_persist_and_once_only(self):
        path = TMP / "dict1.txt"
        d = ArabicDictionary(path)
        self.assertEqual(d.add_pages([("h:1", "الكتاب القلم الكتاب")]), 1)
        self.assertEqual(d.add_pages([("h:1", "الكتاب")]), 0)              # نفس الصفحة لا تُضاف مرتين
        d2 = ArabicDictionary(path)
        self.assertEqual(d2.counts["الكتاب"], 2)
        self.assertIn("h:1", d2.sources)

    def test_normalization_and_affixes(self):
        d = ArabicDictionary(TMP / "dict2.txt")
        d.add_pages([("h:1", "كتاب مدرسة إدارة")])
        for w in ("والكتاب", "بالكتاب", "كتابهم", "المدرسة", "مدرسه", "الادارة", "كِتَاب"):
            self.assertTrue(d.known(w), w)
        self.assertFalse(d.known("ضصثقف"))

    def test_default_location_is_app_data_and_seed_is_empty(self):
        self.assertEqual(ArabicDictionary().path.parent, app_data_dir())
        words = [ln for ln in SEED.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(words, [])

    def test_pipeline_learns_from_text_layer(self):
        d = ArabicDictionary(TMP / "dict3.txt")
        out = TMP / "out_dict"
        ctx = BatchContext(output_dir=out, settings=Settings(), stop_event=threading.Event(),
                           emit=lambda k, **kw: None, hash_cache=HashCache(TMP / "hc3.json"), dictionary=d)
        run_batch([Job(TABLES_PDF, ctx.hash_cache.get(TABLES_PDF))], ctx)
        self.assertTrue(d.known("المستأجر"))
        self.assertEqual(len(d.sources), 3)


# ====================================================================== الوقائع
class FactsTests(unittest.TestCase):
    def test_digits_normalized(self):
        self.assertEqual(normalize_digits("١٢٣٤٫٥ و ۴۵۶"), "1234.5 و 456")

    def test_dates_both_digit_systems(self):
        t = "بتاريخ ١٦/٠٣/٢٠٢٥ ثم 2025/04/01 و 5 مايو 2024 و 01­05­2025 و ١٤٤٦/٠٥/٠١ هـ"
        vals = [v for _, v in find_dates(t)]
        self.assertEqual(vals, ["2025-03-16", "2025-04-01", "2024-05-05", "2025-05-01", "1446-05-01 هـ"])

    def test_invalid_dates_rejected(self):
        self.assertEqual(find_dates("رقم 45/13/2025 و 99/99/9999"), [])

    def test_kwd_amounts(self):
        t = ("مبلغ ١٬٢٥٠٫٥٠٠ د.ك و 750 دينار شهرياً و KWD 3,000 و 5000 دينار كويتي "
             "و 20 دينارا و 500 فلس و 1,000 د. ك")
        vals = [v for _, v in find_amounts(t)]
        self.assertEqual(vals, ["1,250.5 د.ك", "750 د.ك", "3,000 د.ك", "5,000 د.ك", "20.5 د.ك", "1,000 د.ك"])

    def test_amounts_in_kwd_table_column(self):
        md = "| م | البيان | المبلغ (د.ك) |\n| --- | --- | --- |\n| ١ | قسط | 1,250.500 |\n| 2 | رسوم | ٣٠٠ |"
        facts = extract_facts([(4, md)])
        amounts = {f.value: f for f in facts if f.kind == "amount"}
        self.assertEqual(set(amounts), {"1,250.5 د.ك", "300 د.ك"})
        self.assertEqual(amounts["300 د.ك"].raw, "٣٠٠")      # كما ورد في الجدول
        self.assertEqual(amounts["300 د.ك"].pages, [4])
        self.assertFalse(any(f.value == "1 د.ك" for f in facts))  # عمود "م" ليس مبلغاً

    def test_amount_raw_kept(self):
        raw, _ = find_amounts("المبلغ 1,250.500 د.ك فقط")[0]
        self.assertIn("1,250.500", raw)

    def test_case_numbers(self):
        t = "في الدعوى رقم ١٢٣٤/٢٠٢٣ تجاري كلي/5 بتاريخ اليوم، والطعن بالتمييز رقم 456 لسنة 2022"
        vals = [v for _, v in find_case_numbers(t)]
        self.assertIn("1234/2023 تجاري كلي/5", vals)
        self.assertIn("456/2022", vals)

    def test_names(self):
        from app.facts import find_names
        t = ("بين الطرف الأول السيد أحمد علي (المؤجر) والطرف الثاني السيدة فاطمة حسن (المستأجر). يؤجر الطرف "
             "الأول للطرف الثاني الشقة.\nالمدعي: خالد سعد ضد المدعى عليه/ شركة النخيل للتجارة بصفتها، "
             "والحاضر عنه المحامي ناصر فهد وبنك الكويت الوطني")
        got = {(r, n) for r, n, _ in find_names(t)}
        for item in [("party", "أحمد علي"), ("party", "فاطمة حسن"), ("party", "خالد سعد"),
                     ("party", "شركة النخيل للتجارة"), ("lawyer", "ناصر فهد"), ("entity", "بنك الكويت الوطني")]:
            self.assertIn(item, got)
        self.assertFalse(any("الطرف" in n or "المؤجر" in n for _, n in got))

    def test_facts_carry_pages(self):
        facts = extract_facts([(1, "بتاريخ 2025/03/16 مبلغ 100 د.ك"), (3, "تاريخ ٢٠٢٥/٠٣/١٦")])
        date = next(f for f in facts if f.kind == "date")
        self.assertEqual(date.pages, [1, 3])
        self.assertEqual(next(f for f in facts if f.kind == "amount").pages, [1])


# ====================================================================== الملخص
class SummaryTests(unittest.TestCase):
    def test_ignores_headers_page_numbers_short_lines(self):
        header = "وزارة العدل - إدارة كتاب المحكمة الكلية - دولة الكويت"
        pages = [(n, f"{header}\n\nتوقيع\n\n{body}\n\n- {n} -\n\nصفحة {n} من 3") for n, body in (
            (1, "أقام المدعي دعواه بطلب إلزام المدعى عليها بسداد قيمة الأجرة المتأخرة عن مدة ستة أشهر كاملة."),
            (2, "وقدم المدعي حافظة مستندات طويلة تضمنت صورة عقد الإيجار وكشف حساب الأجرة المستحقة عن المدة."),
            (3, "وحيث إن المحكمة ترى أن الدعوى قد أقيمت على أساس صحيح من القانون فإنها تقضي بإلزام المدعى عليها."))]
        kept = [s for _, s in clean_pages(pages)]
        self.assertFalse(any(header in s or s == "توقيع" or "صفحة" in s for s in kept))
        lines = extractive_summary(pages)
        self.assertGreaterEqual(len(lines), 3)
        self.assertTrue(all("(ص " in ln for ln in lines))
        self.assertFalse(any(header in ln for ln in lines))


# ====================================================================== الفهرس والبوابة
class FakeTextProvider(ExternalProvider):
    def __init__(self):
        self.name = "fake"
        self.model = "m"
        self.calls: list[str] = []

    def _complete(self, prompt: str, images: list[PageImage]) -> str:
        self.calls.append(prompt)
        return "- سطر أول من الملخص (ص 1)\n- سطر ثان (ص 1)\n- سطر ثالث (ص 1)"


BODY = ("صحيفة افتتاح دعوى\n\nأقام المدعي أحمد علي الكندري الدعوى رقم ١٢٣٤/٢٠٢٣ تجاري كلي ضد شركة النخيل للتجارة "
        "بطلب إلزامها بأن تؤدي له مبلغ ١٬٢٥٠٫٥٠٠ د.ك قيمة الأجرة المتأخرة عن المدة من ٢٠٢٥/٠١/٠١ حتى 2025/06/30 "
        "مع الفوائد القانونية والمصروفات ومقابل أتعاب المحاماة الفعلية.")


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pdf2md-idx-"))
        self.out = self.dir / "out"
        self.audit = AuditLog(self.dir / "sent.csv")
        self.gate = CloudGate(self.audit, mode=CLOUD)

    def make_file(self, name: str, pages: int = 2, pending: int = 0) -> cp.FileState:
        pdf = self.dir / name
        doc = fitz.open()
        for i in range(pages):
            doc.new_page().insert_text((50, 80), f"{name} {i}")
        doc.save(pdf)
        doc.close()
        h = sha256_file(pdf)
        st = cp.FileState(self.out / ".state", h, name, pages)
        st.data["source_path"] = str(pdf)
        for n in range(1, pages + 1):
            if n > pages - pending:
                st.save_page(n, cp.NEEDS_REVIEW, "none")
            else:
                st.save_page(n, cp.DONE, "text_layer", BODY if n == 1 else "وتنتهي الصحيفة بالطلبات الختامية المذكورة.")
        return st

    def test_index_format_and_attribution(self):
        self.make_file("a.pdf")
        res = build_index(self.out, Settings())
        md = Path(res["path"]).read_text(encoding="utf-8")
        lines = md.splitlines()
        first_file_line = next(ln for ln in lines if ln.startswith("- **"))
        self.assertIn("a.pdf", first_file_line)
        self.assertIn("صحيفة دعوى", first_file_line)
        self.assertIn("✓ مكتمل", first_file_line)
        self.assertIn("1234/2023 تجاري كلي — ص 1", md)
        self.assertIn("1,250.5 د.ك (كما ورد: 1,250.500 د.ك) — ص 1", md)
        self.assertIn("2025-01-01", md)
        self.assertIn("أحمد علي الكندري", md)
        self.assertIn("### الملخص (المصدر: محلي)", md)
        # السطور الموجزة قبل التفاصيل
        self.assertLess(lines.index(first_file_line), lines.index("## a.pdf"))

    def test_completeness_shows_missing_pages(self):
        self.make_file("b.pdf", pages=3, pending=1)
        md = Path(build_index(self.out, Settings())["path"]).read_text(encoding="utf-8")
        self.assertIn("⚠ ناقص: 1 بانتظار المراجعة", md)

    def test_local_mode_sends_nothing(self):
        self.make_file("a.pdf")
        self.make_file("b.pdf")
        self.gate.set_mode(LOCAL)
        prov = FakeTextProvider()
        md = Path(build_index(self.out, Settings(summary_via_api=True),
                              api=ApiSummarizer(self.gate, prov))["path"]).read_text(encoding="utf-8")
        self.assertEqual(prov.calls, [])
        self.assertFalse((self.dir / "sent.csv").exists())
        self.assertNotIn(SOURCE_API, md)

    def test_cloud_mode_summarizes_via_api_and_logs(self):
        self.make_file("open.pdf")
        prov = FakeTextProvider()
        md = Path(build_index(self.out, Settings(summary_via_api=True),
                              api=ApiSummarizer(self.gate, prov))["path"]).read_text(encoding="utf-8")
        self.assertEqual(len(prov.calls), 1)
        with open(self.dir / "sent.csv", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual({r["file"] for r in rows}, {"open.pdf"})
        self.assertIn(f"المصدر: {SOURCE_API}", md)

    def test_api_option_off_by_default(self):
        self.assertFalse(Settings().summary_via_api)
        self.make_file("open.pdf")
        prov = FakeTextProvider()
        build_index(self.out, Settings(), api=ApiSummarizer(self.gate, prov))
        self.assertEqual(prov.calls, [])

    def test_gate_refuses_in_local_mode_even_if_called_directly(self):
        st = self.make_file("secret.pdf")
        self.gate.set_mode(LOCAL)
        prov = FakeTextProvider()
        with self.assertRaises(CloudBlockedError):
            ApiSummarizer(self.gate, prov).summarize(st.data["source_path"], st.file_hash, [(1, BODY)])
        self.assertEqual(prov.calls, [])
        self.assertFalse((self.dir / "sent.csv").exists())

    def test_incremental_update(self):
        a = self.make_file("a.pdf")
        self.make_file("b.pdf")
        r1 = build_index(self.out, Settings())
        self.assertEqual((r1["built"], r1["reused"]), (2, 0))
        r2 = build_index(self.out, Settings())
        self.assertEqual((r2["built"], r2["reused"]), (0, 2))
        a.save_page(2, cp.DONE, "text_layer", "نص جديد للصفحة الثانية بتاريخ 2024/12/31")
        r3 = build_index(self.out, Settings())
        self.assertEqual((r3["built"], r3["reused"]), (1, 1))
        self.make_file("c.pdf")
        r4 = build_index(self.out, Settings())
        self.assertEqual((r4["built"], r4["reused"], r4["files"]), (1, 2, 3))
        r5 = build_index(self.out, Settings(), full=True)
        self.assertEqual((r5["built"], r5["reused"]), (3, 0))
        self.assertIn("2024-12-31", (self.out / INDEX_NAME).read_text(encoding="utf-8"))


# ====================================================================== تقرير المراجعة
class ReviewReportTests(unittest.TestCase):
    def test_mismatch_and_weak_pages_reported_with_images(self):
        d = Path(tempfile.mkdtemp(prefix="pdf2md-rev-"))
        pdf = d / "m.pdf"
        doc = fitz.open()
        for _ in range(3):
            doc.new_page().insert_text((50, 80), "x")
        doc.save(pdf)
        out = d / "out"
        st = cp.FileState(out / ".state", "f" * 64, "m.pdf", 3)
        st.save_page(1, cp.DONE, "api:x", TABLE_A)
        st.save_page(2, cp.DONE, "api:x", f"{TABLE_CONT}\n| 3 | ثالث | 300 | زيادة |")
        st.save_page(3, cp.DONE, "api:x", CLEAN + " [؟] [غير واضح] [؟]")
        ctx = BatchContext(output_dir=out, settings=Settings(), stop_event=threading.Event(),
                           emit=lambda k, **kw: None, hash_cache=HashCache(d / "hc.json"),
                           dictionary=ArabicDictionary(d / "dict.txt"))
        with fitz.open(pdf) as doc:
            res = finalize_file(st, doc, ctx, api_ok=False)
        self.assertEqual(res["tables"]["mismatched"], 1)
        self.assertEqual(res["weak_count"], 1)
        rep = (out / REPORT_NAME).read_text(encoding="utf-8")
        self.assertIn("## m.pdf", rep)
        self.assertIn("| 2 | جدول ممتد بعدد أعمدة غير متطابق", rep)
        self.assertIn("| 3 | علامات [ ] كثيرة", rep)
        self.assertTrue((out / "review" / f"m__{'f' * 8}_p0002.png").exists())
        md = (out / "docs" / "m.md").read_text(encoding="utf-8")
        self.assertIn(MISMATCH_TAG, md)
        self.assertEqual(st.page(3)["score"]["reasons"], ["brackets"])     # الدرجة محفوظة في checkpoint
        # بعد حل المشكلة تختفي الصفحة من التقرير وتُحذف صورتها
        st.save_page(3, cp.DONE, "api:x", CLEAN)
        with fitz.open(pdf) as doc:
            finalize_file(st, doc, ctx, api_ok=False)
        rep = (out / REPORT_NAME).read_text(encoding="utf-8")
        self.assertNotIn("| 3 |", rep)
        self.assertFalse((out / "review" / f"m__{'f' * 8}_p0003.png").exists())

    def test_weak_ocr_page_held_for_review_in_cloud_mode(self):
        d = Path(tempfile.mkdtemp(prefix="pdf2md-rev2-"))
        pdf = d / "w.pdf"
        doc = fitz.open()
        doc.new_page().insert_text((50, 80), "x")
        doc.save(pdf)
        st = cp.FileState(d / "out" / ".state", "e" * 64, "w.pdf", 1)
        st.save_page(1, cp.DONE, "ocr:local", CLEAN + " [؟] [؟] [؟]")
        ctx = BatchContext(output_dir=d / "out", settings=Settings(), stop_event=threading.Event(),
                           emit=lambda k, **kw: None, hash_cache=HashCache(d / "hc.json"),
                           dictionary=ArabicDictionary(d / "dict.txt"))
        with fitz.open(pdf) as doc:
            finalize_file(st, doc, ctx, api_ok=True)
        self.assertEqual(st.status(1), cp.NEEDS_REVIEW)
        self.assertEqual(st.page(1)["review_reason"], "brackets")
        self.assertTrue(st.local_path(1).exists())     # الناتج المحلي محفوظ جانباً


if __name__ == "__main__":
    unittest.main()
