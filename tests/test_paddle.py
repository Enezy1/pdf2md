"""اختبارات PaddleOCR (تجريبي) بعملية worker وهمية: التنصيب والحذف، الرجوع للمحرّك المحلي،
علامات الثقة، ترتيب الأسطر العربي، توحيد الأرقام، عدم الاتصال بالشبكة، والعمل بدون Paddle."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))

from make_sample_pdf import build  # noqa: E402

from app import checkpoint as cp  # noqa: E402
from app import paddle_engine as pe  # noqa: E402
from app.config import Settings, app_data_dir  # noqa: E402
from app.hashing import HashCache  # noqa: E402
from app.models_catalog import FileSpec  # noqa: E402
from app.pipeline import BatchContext, Job, run_batch  # noqa: E402
from app.providers.base import PageResult, Provider  # noqa: E402

FAKE = ROOT / "tests" / "fake_paddle_worker.py"


def box(x0, y0, x1, y1):
    return [x0, y0, x1, y1]


GOOD = [
    {"text": "عقد إيجار", "conf": 0.97, "box": box(600, 100, 900, 140)},
    {"text": "رقم ١٥٣ لسنة ٢٠٢٤", "conf": 0.95, "box": box(500, 200, 900, 240)},
]
LOW = GOOD + [{"text": "كلمة غامضة", "conf": 0.42, "box": box(500, 300, 900, 340)}]


def fake_worker(lines=GOOD, mode="ok", log: Path | None = None) -> pe.PaddleWorker:
    env = {"FAKE_PADDLE_MODE": mode, "FAKE_PADDLE_LINES": json.dumps(lines, ensure_ascii=False)}
    if log:
        env["FAKE_PADDLE_LOG"] = str(log)
    with mock.patch.dict(os.environ, env):
        return pe.PaddleWorker(engine_root=Path(tempfile.mkdtemp()), script=FAKE, start_timeout=30, page_timeout=30).start()


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self):
        self.calls = []

    def ocr(self, request):
        self.calls += [i.page_number for i in request.images]
        return [PageResult(i.page_number, f"نص محلي {i.page_number}") for i in request.images]


# ------------------------------------------------------------ التنسيق
class LayoutTests(unittest.TestCase):
    def test_reading_order_rtl_top_down(self):
        items = [
            {"text": "يسار-أعلى", "conf": 0.9, "box": box(100, 100, 300, 140)},
            {"text": "سطر-ثاني", "conf": 0.9, "box": box(400, 200, 900, 240)},
            {"text": "يمين-أعلى", "conf": 0.8, "box": box(500, 105, 900, 138)},
        ]
        lines = pe.order_lines(items)
        self.assertEqual([ln.text for ln in lines], ["يمين-أعلى يسار-أعلى", "سطر-ثاني"])
        self.assertAlmostEqual(lines[0].conf, 0.8)  # ثقة السطر المدموج = الأدنى

    def test_digits_and_brackets(self):
        md, low = pe.to_markdown(pe.order_lines([{"text": "المبلغ ٤٥٠٠٠ )دينار(", "conf": 0.99, "box": box(0, 0, 9, 9)}]),
                                 0.8, True)
        self.assertEqual(md, "المبلغ 45000 (دينار)")
        self.assertEqual(low, [])

    def test_low_confidence_marked(self):
        md, low = pe.to_markdown(pe.order_lines(LOW), 0.8, True)
        self.assertEqual(low, [3])
        self.assertIn(f"{pe.LOW_MARK} كلمة غامضة", md)
        self.assertNotIn(pe.LOW_MARK + " عقد", md)
        from app.page_score import bracket_marks
        self.assertEqual(bracket_marks(md), 1)  # تُعدّ علامة [ ] في درجة الصفحة

    def test_raw_keeps_confidence_and_original_digits(self):
        raw = pe.raw_text(pe.order_lines(GOOD), 12)
        self.assertIn("0.950\tرقم ١٥٣ لسنة ٢٠٢٤", raw)

    def test_indic_zero_dots_restored(self):
        from app.paddle_worker import fix_indic_zeros, pred_reverse
        self.assertEqual(pred_reverse(fix_indic_zeros("(· . ٣٤ ك.د)")), ")د.ك ٠٠٣٤(")
        self.assertEqual(fix_indic_zeros("12.5 د.ك"), "12.5 د.ك")      # الأرقام الإنجليزية لا تُمس
        self.assertEqual(fix_indic_zeros("ك.د"), "ك.د")

    def test_comma_number_keeps_order(self):
        from app.paddle_worker import pred_reverse
        self.assertEqual(pred_reverse("٨٠٣٥،٢٩١"), "٨٠٣٥،٢٩١")

    def test_page_image_rendered_for_vector_page(self):
        import pymupdf as fitz
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "hello")
        png, scale = pe.page_image(page)
        self.assertEqual(scale, 1)
        self.assertTrue(png[1:4] == b"PNG")

    def test_page_image_native_low_res_scan(self):
        import pymupdf as fitz
        doc = fitz.open()
        page = doc.new_page()
        pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 826, 1169), False)
        pix.clear_with(255)
        page.insert_image(page.rect, pixmap=pix)
        png, scale = pe.page_image(page)
        self.assertEqual(scale, 3)  # ~100 نقطة ← تكبير ×3 داخل worker

    def test_worker_reverses_arabic_keeps_numbers(self):
        from app.paddle_worker import pred_reverse
        self.assertEqual(pred_reverse("مقر 153"), "153 رقم")
        self.assertEqual(pred_reverse("ةنس ٤٢٠٢"), "٤٢٠٢ سنة")


# ------------------------------------------------------------ العملية المنفصلة والشبكة
class WorkerTests(unittest.TestCase):
    def test_protocol_and_isolation_flags(self):
        w = fake_worker()
        try:
            self.assertTrue(w.info["net_blocked"], "الشبكة يجب أن تكون ممنوعة داخل worker")
            self.assertEqual(w.info["nosite"], 1)
            self.assertEqual(w.info["isolated"], 1)
            lines, ms = w.ocr_lines(b"png")
            self.assertEqual(len(lines), 2)
        finally:
            w.close()
        self.assertFalse(w.alive)

    def test_real_worker_blocks_network(self):
        code = ("import sys,socket;sys.path.insert(0,r'%s');import app.paddle_worker as w;w.block_network()\n"
                "for f in (lambda: socket.create_connection(('example.com',80)), lambda: socket.getaddrinfo('x',1),"
                " lambda: socket.socket().connect(('127.0.0.1',9))):\n"
                "  try: f(); print('OPEN')\n  except w.NetworkBlocked: print('BLOCKED')\n") % ROOT
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30).stdout
        self.assertEqual(out.split(), ["BLOCKED"] * 3)

    def test_crash_raises_paddle_error(self):
        w = fake_worker(mode="crash")
        with self.assertRaises(pe.PaddleError):
            w.ocr_lines(b"png")
        self.assertFalse(w.alive)

    def test_start_failure(self):
        with self.assertRaises(pe.PaddleError):
            fake_worker(mode="noready")

    def test_quick_test_with_fake(self):
        lines = [{"text": t, "conf": 0.99, "box": box(0, i * 50, 500, i * 50 + 40)} for i, t in enumerate(pe.QUICK_LINES)]
        r = pe.quick_test(lambda: fake_worker(lines))
        self.assertTrue(r["ok"], r)
        r = pe.quick_test(lambda: fake_worker(lines[:1]))
        self.assertFalse(r["ok"])
        self.assertIn("45000", r["missing"])


# ------------------------------------------------------------ الربط بالمعالجة
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = build(self.tmp / "in" / "عقد.pdf")
        self.out = self.tmp / "out"
        self.cache = HashCache(self.tmp / "cache.json")
        self.job = Job(self.pdf, self.cache.get(self.pdf))
        self.events = []

    def _run(self, paddle, provider=None, mode="paddle", api_ok=False):
        s = Settings()
        s.scan_engine = mode
        ctx = BatchContext(self.out, s, threading.Event(), lambda k, **kw: self.events.append((k, kw)), self.cache,
                           provider, paddle=paddle)
        try:
            run_batch([self.job], ctx)
        finally:
            if ctx.paddle is not None:
                ctx.paddle.close()
        return ctx

    def _state(self):
        return cp.FileState(self.out / ".state", self.job.file_hash, self.pdf.name, self._pages())

    def _pages(self):
        import pymupdf as fitz
        with fitz.open(self.pdf) as d:
            return d.page_count

    def _md(self):
        return (self.out / "docs" / "عقد.md").read_text(encoding="utf-8")

    def _logs(self):
        return " ".join(kw.get("text", "") for k, kw in self.events if k == "log")

    def test_paddle_only_reads_scanned_pages(self):
        self._run(fake_worker(LOW), mode="paddle")
        st = self._state()
        scanned = [n for n in range(1, st.page_count + 1) if st.page(n)["method"] == pe.METHOD]
        self.assertEqual(scanned, [2, 4, 5])
        p = st.page(2)
        self.assertEqual(p["status"], cp.DONE)
        self.assertEqual(p["paddle"]["low_lines"], [3])
        frag = st.read_fragment(2)
        self.assertIn("رقم 153 لسنة 2024", frag)            # أرقام موحّدة
        self.assertIn(f"{pe.LOW_MARK} كلمة غامضة", frag)    # السطر الضعيف معلّم
        raw = st.paddle_path(2).read_text(encoding="utf-8")
        self.assertIn("0.420\tكلمة غامضة", raw)             # النص الخام مع الثقة
        md = self._md()
        self.assertIn("<!-- engines: paddle: 2,4-5 -->", md)
        report = (self.out / "needs_human_review.md").read_text(encoding="utf-8")
        self.assertIn("PaddleOCR", report)

    def test_paddle_then_local_for_low_confidence(self):
        local = ScriptedProvider()
        self._run(fake_worker(LOW), local, mode="paddle_local")
        self.assertEqual(sorted(local.calls), [2, 4, 5])
        st = self._state()
        self.assertEqual(st.page(2)["method"], "ocr:scripted")
        self.assertEqual(st.page(2)["paddle"]["low_lines"], [3])  # يبقى أثر محاولة Paddle
        self.assertTrue(st.paddle_path(2).exists())

    def test_paddle_then_local_keeps_confident_pages(self):
        local = ScriptedProvider()
        self._run(fake_worker(GOOD), local, mode="paddle_local")
        self.assertEqual(local.calls, [])
        self.assertEqual(self._state().page(4)["method"], pe.METHOD)

    def test_failure_falls_back_to_local(self):
        local = ScriptedProvider()
        self._run(fake_worker(mode="fail"), local, mode="paddle")
        self.assertEqual(sorted(local.calls), [2, 4, 5])
        self.assertIn("تعذّر PaddleOCR", self._logs())

    def test_crash_without_local_stays_pending(self):
        log = self.tmp / "calls.log"
        w = fake_worker(mode="crash", log=log)
        with mock.patch.object(pe.PaddleWorker, "start", side_effect=pe.PaddleError("لا إعادة")):
            ctx = self._run(w, None, mode="paddle")
        self.assertIsNone(ctx.paddle)                         # تُرك Paddle بعد فشل إعادة التشغيل
        self.assertEqual(log.read_text().count("ocr"), 1)     # لم تُرسل صفحات أخرى لعملية ميتة
        st = self._state()
        for n in (2, 4, 5):
            self.assertEqual(st.status(n), cp.PENDING_ENGINE)
        self.assertIn("[غير مقروء: صفحة مصوّرة بانتظار المحرّك المحلي]", self._md())
        self.assertEqual(st.page(1)["method"], "text_layer")  # البرنامج أكمل كالمعتاد

    def test_pending_pages_picked_up_later(self):
        self._run(None)
        self.assertEqual(self._state().status(2), cp.PENDING_ENGINE)
        self._run(fake_worker(GOOD))
        self.assertEqual(self._state().page(2)["method"], pe.METHOD)

    def test_no_network_during_run(self):
        opened = []
        real = socket.socket.connect

        def spy(sock, addr):
            opened.append(addr)
            return real(sock, addr)
        with mock.patch.object(socket.socket, "connect", spy), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")):
            self._run(fake_worker(GOOD))
        self.assertEqual(opened, [])

    def test_runs_as_usual_without_paddle(self):
        local = ScriptedProvider()
        self._run(None, local, mode="local")
        self.assertEqual(sorted(local.calls), [2, 4, 5])
        md = self._md()
        self.assertIn("<!-- engines: scripted: 2,4-5 -->", md)
        self.assertNotIn("paddle", md)
        self.assertFalse(any(self._state().paddle_path(n).exists() for n in (2, 4, 5)))


# ------------------------------------------------------------ التنصيب والحذف
def _wheel(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}/__init__.py", "X = 1\n")
        z.writestr(f"{name}-1.0.dist-info/METADATA", "Name: x\n")
    return buf.getvalue()


class FakeOpener:
    def __init__(self, blobs: dict[str, bytes]):
        self.blobs, self.urls = blobs, []

    def open(self, req, timeout=60):
        url = req.full_url
        self.urls.append(url)
        data = self.blobs[url]
        rng = req.headers.get("Range")
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        return _Resp(data[start:], 206 if rng else 200)


class _Resp(io.BytesIO):
    def __init__(self, data, status):
        super().__init__(data)
        self.status = status


def _hash_tree(p: Path) -> dict:
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob("*"))
            if f.is_file() and "__pycache__" not in f.parts}


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="pdf2md-paddle-"))
        self.env = mock.patch.dict(os.environ, {"PDF2MD_DATA_DIR": str(self.data)})
        self.env.start()
        blobs, items = {}, []
        for kind, name, content in (("model", "ch_PP-OCRv5_det_mobile.onnx", b"det" * 100),
                                    ("model", "arabic_PP-OCRv5_rec_mobile.onnx", b"rec" * 100),
                                    ("wheel", "fakelib-1.0-py3-none-any.whl", _wheel("fakelib"))):
            url = f"https://example.invalid/{name}"
            blobs[url] = content
            items.append(pe.Item(kind, url, FileSpec(name, len(content), hashlib.sha256(content).hexdigest())))
        self.blobs = blobs
        self.patches = [mock.patch.object(pe, "ITEMS", tuple(items)),
                        mock.patch.object(pe, "unsupported_reason", return_value="")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()

    def test_install_then_uninstall_touches_only_paddle(self):
        # ملفات أخرى في مجلد البيانات وفي المشروع يجب ألا تُمس
        (self.data / "settings.json").write_text("{}", encoding="utf-8")
        (self.data / "models" / "qwen").mkdir(parents=True)
        (self.data / "models" / "qwen" / "m.gguf").write_bytes(b"gguf")
        (self.data / "engines" / "other").mkdir(parents=True)
        (self.data / "engines" / "other" / "keep.txt").write_text("k")
        before_data = _hash_tree(self.data)
        before_proj = _hash_tree(ROOT / "app")
        req_before = (ROOT / "requirements.txt").read_bytes()

        self.assertFalse(pe.is_installed())
        msg = pe.install(opener=FakeOpener(self.blobs))
        self.assertIn("تم تنصيب", msg)
        self.assertTrue(pe.is_installed())
        r = pe.root()
        self.assertEqual(r, self.data / "engines" / "paddle")
        self.assertTrue((r / "lib" / "fakelib" / "__init__.py").is_file())
        self.assertTrue((r / "models" / "arabic_PP-OCRv5_rec_mobile.onnx").is_file())
        self.assertEqual(json.loads((r / "version.json").read_text(encoding="utf-8"))["version"], pe.ENGINE_VERSION)
        self.assertFalse((r / "downloads").exists())

        s = Settings()
        s.scan_engine, s.paddle_min_conf = "paddle", 0.6
        s.save()
        freed = pe.uninstall(s)
        self.assertGreater(freed, 0)
        self.assertFalse(r.exists())
        self.assertFalse(pe.is_installed())
        self.assertEqual(Settings.load().scan_engine, "local")
        self.assertEqual(Settings.load().paddle_min_conf, 0.8)
        after = _hash_tree(self.data)
        after.pop("settings.json")
        before_data.pop("settings.json")
        self.assertEqual(after, before_data)
        self.assertEqual(_hash_tree(ROOT / "app"), before_proj)
        self.assertEqual((ROOT / "requirements.txt").read_bytes(), req_before)

    def test_uninstall_removes_empty_engines_dir(self):
        pe.install(opener=FakeOpener(self.blobs))
        pe.uninstall()
        self.assertFalse((self.data / "engines").exists())
        self.assertTrue(self.data.exists())

    def test_bad_hash_rejected_nothing_installed(self):
        bad = dict(self.blobs)
        url = next(u for u in bad if u.endswith("rec_mobile.onnx"))
        bad[url] = b"x" * len(bad[url])
        from app.model_manager import IntegrityError
        with self.assertRaises(IntegrityError):
            pe.install(opener=FakeOpener(bad))
        self.assertFalse(pe.is_installed())

    def test_resume_after_interruption(self):
        url = next(u for u in self.blobs if u.endswith("det_mobile.onnx"))
        part = pe.root() / "downloads" / "ch_PP-OCRv5_det_mobile.onnx.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(self.blobs[url][:50])
        op = FakeOpener(self.blobs)
        pe.install(opener=op)
        self.assertTrue(pe.is_installed())

    def test_install_from_folder(self):
        src = Path(tempfile.mkdtemp()) / "usb" / "sub"
        src.mkdir(parents=True)
        for url, data in self.blobs.items():
            (src / url.rsplit("/", 1)[1]).write_bytes(data)
        pe.install_from_folder(src.parent)
        self.assertTrue(pe.is_installed())

    def test_install_from_incomplete_folder(self):
        src = Path(tempfile.mkdtemp())
        with self.assertRaises(pe.PaddleError):
            pe.install_from_folder(src)
        self.assertFalse(pe.is_installed())

    def test_frozen_exe_needs_embedded_python(self):
        """داخل exe لا مفسّر: المحرّك متاح فقط بعد فك البايثون المضمَّن؛ والتنصيب القديم يُحدَّث بتنزيله وحده."""
        import io as _io
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("python.exe", b"MZ")
            z.writestr("python313._pth", "python313.zip\n.\n")
        blob = buf.getvalue()
        py = pe.Item("python", "https://example.invalid/py.zip", FileSpec("py.zip", len(blob), hashlib.sha256(blob).hexdigest()))
        pe.install(opener=FakeOpener(self.blobs))               # تنصيب بلا بايثون (مثل تنصيب قديم)
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(pe, "PY_EMBED", py):
            self.assertEqual(pe.unsupported_reason(), "")
            self.assertTrue(pe.needs_python())
            self.assertFalse(pe.is_available())
            pe.install_python(opener=FakeOpener({py.url: blob}))
            self.assertFalse(pe.needs_python())
            self.assertTrue(pe.is_available())
            self.assertEqual(pe.worker_python(), str(pe.root() / "python" / "python.exe"))
        self.assertFalse((pe.root() / "downloads").exists())

    def test_pinned_catalog(self):
        real = [i for i in self.patches[0].temp_original]
        for i in real:
            self.assertEqual(len(i.file.sha256), 64)
            self.assertGreater(i.file.size, 0)
            self.assertTrue(i.url.startswith("https://"))
            if i.kind == "wheel":
                self.assertIn(i.file.name, i.url)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        with mock.patch.dict(os.environ, {"PDF2MD_DATA_DIR": tempfile.mkdtemp()}):
            self._check()

    def _check(self):
        s = Settings()
        self.assertEqual((s.scan_engine, s.paddle_min_conf), ("local", 0.8))
        s.scan_engine, s.paddle_min_conf = "bogus", 7.0
        s.save()
        s2 = Settings.load()
        self.assertEqual(s2.scan_engine, "local")
        self.assertEqual(s2.paddle_min_conf, 1.0)


if __name__ == "__main__":
    unittest.main()


class EngineChoiceTests(unittest.TestCase):
    """المثبّت الوحيد هو الافتراضي؛ والتفعيل الصريح يُحترم."""

    def _eff(self, scan, llama_ready, paddle_ok):
        from app import engine_choice as ec
        from app import models_catalog as cat
        s = Settings()
        s.scan_engine, s.local_model = scan, cat.MODELS[0].id
        with mock.patch.object(ec, "ready_llama_spec", return_value=cat.MODELS[0] if llama_ready else None):
            return ec.effective(s, paddle_ok)[0]

    def test_only_paddle_installed_is_default(self):
        self.assertEqual(self._eff("local", False, True), "paddle")

    def test_local_default_when_both_installed(self):
        self.assertEqual(self._eff("local", True, True), "local")

    def test_explicit_paddle(self):
        self.assertEqual(self._eff("paddle", True, True), "paddle")
        self.assertEqual(self._eff("paddle_local", True, True), "paddle_local")
        self.assertEqual(self._eff("paddle_local", False, True), "paddle")

    def test_paddle_choice_ignored_after_uninstall(self):
        self.assertEqual(self._eff("paddle", True, False), "local")
        self.assertEqual(self._eff("paddle", False, False), "none")


class FixesTests(unittest.TestCase):
    def test_duplicate_cover_page_not_treated_as_footer(self):
        from app.cleanup import strip_page_furniture
        cover = ("مذكرة بدفاع\n\nفاطمة موسى ابراهيم الشطي مدعية\n\nضد\n\n"
                 "شركة اول اباوت اس لإدارة المشاريع مدعى عليها\n\nفي الدعوى رقم 2025/7983 تجاري كلي/47\n\n"
                 "والمحدد لنظرها جلسة 2025/12/8\n\nأمام الخبيرة أ/ أسماء عصام العصيمي")
        body = "\n\n".join(f"سطر من متن الصفحة رقم {i} فيه كلام كثير مختلف" for i in range(8))
        pages = {1: cover, 2: body + " أ", 3: body + " ب", 4: body + " ج", 5: cover}
        out = strip_page_furniture(pages)
        self.assertIn("شركة اول اباوت", out[1])
        self.assertIn("أمام الخبيرة", out[1])

    def test_margin_marks_dropped_but_short_first_word_kept(self):
        size = (2000, 3000)
        lines = pe.order_lines([
            {"text": "2", "conf": 0.9, "box": box(950, 50, 1000, 110)},            # رقم الصفحة أعلى
            {"text": "F", "conf": 0.9, "box": box(1950, 400, 1990, 450)},          # علامة على الحافة
            {"text": "إن", "conf": 0.9, "box": box(1850, 1000, 1950, 1060)},       # أول كلمة في سطر
            {"text": "العلاقة بين الطرفين", "conf": 0.9, "box": box(1000, 1000, 1840, 1060)},
            {"text": "ضد", "conf": 0.9, "box": box(950, 1500, 1050, 1560)},        # كلمة قصيرة في المتن
        ])
        kept = [ln.text for ln in pe.drop_margin_marks(lines, size)]
        self.assertEqual(kept, ["إن العلاقة بين الطرفين", "ضد"])

    def test_office_footer_removed_but_repeated_attachment_line_kept(self):
        from app.cleanup import strip_page_furniture
        foot = ["برج النموذج دور 10 - حي الاختبار ص.ب 00000 الاختبار 00000 النموذج", "+965 90000001",
                "info@example.com", "www.example.com", "الصفحة {n} من 11",
                "شركة مساهمة كويتية مهنية (مقفلة)", "رأس المال المدفوع 500,000 د.ك"]
        pages = {}
        for n in range(1, 6):
            body = [f"نص المتن الخاص بالصفحة {n} وفيه كلام مختلف عن غيره رقم {n * 7}",
                    "(مرفق رقم (4) من حافظة مستنداتنا المقدمة بجلسة اليوم)"]
            pages[n] = "\n".join(body + [f.format(n=n) for f in foot])
        out = strip_page_furniture(pages)
        for n in range(1, 6):
            self.assertTrue(out[n].endswith("(مرفق رقم (4) من حافظة مستنداتنا المقدمة بجلسة اليوم)"), out[n])
            self.assertNotIn("90000001", out[n])
