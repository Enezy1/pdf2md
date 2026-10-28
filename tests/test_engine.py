"""اختبارات المرحلة 2: المحرّك المحلي، مدير النماذج، الفحص المستقل، benchmark."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))

import pymupdf as fitz  # noqa: E402

import benchmark  # noqa: E402
from app import model_manager as mm  # noqa: E402
from app import models_catalog as cat  # noqa: E402
from app.config import Settings  # noqa: E402
from app.crosscheck import match_score  # noqa: E402
from app.hashing import HashCache  # noqa: E402
from app.pdf_io import page_text_to_markdown  # noqa: E402
from app.pipeline import BatchContext, Job, run_batch  # noqa: E402
from app.providers import local_llama as ll  # noqa: E402
from app.providers.base import EngineUnavailable, OcrRequest, PageImage, PageResult, Provider  # noqa: E402
from make_sample_pdf import build  # noqa: E402

FAKE_SERVER = Path(__file__).with_name("fake_llama_server.py")


class LoopbackOnlyTests(unittest.TestCase):
    def test_accepts_only_127_0_0_1(self):
        self.assertEqual(ll.check_loopback_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(ll.check_loopback_url("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080")

    def test_rejects_everything_else(self):
        for url in (
            "http://localhost:8080", "http://0.0.0.0:8080", "http://[::1]:8080", "http://127.0.0.2:8080",
            "http://192.168.1.10:8080", "http://10.0.0.1:8080", "https://127.0.0.1:8080",
            "http://127.0.0.1", "http://user@127.0.0.1:8080", "http://127.0.0.1.evil.com:8080",
            "http://evil.com:80/?h=127.0.0.1", "http://2130706433:8080", "ftp://127.0.0.1:21", "",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ll.check_loopback_url(url)
            with self.subTest(provider=url), self.assertRaises(ValueError):
                ll.LocalLlamaProvider(url)

    def test_provider_rechecks_before_each_request(self):
        p = ll.LocalLlamaProvider("http://127.0.0.1:9")
        p.base_url = "http://example.com:80"  # تلاعب بعد الإنشاء
        with self.assertRaises(ValueError):
            p.ocr(OcrRequest([PageImage(1, b"x", 150)]))

    def test_server_binds_loopback_only(self):
        cmd = ll.server_command(Path("llama-server.exe"), Path("m.gguf"), Path("p.gguf"), 1234)
        self.assertEqual(cmd[cmd.index("--host") + 1], "127.0.0.1")
        self.assertNotIn("0.0.0.0", cmd)

    def test_free_port_is_loopback(self):
        self.assertGreater(ll.free_port(), 0)


def _fake_cmd(mode):
    return lambda exe, model, mmproj, port, threads=None, ctx_size=8192, gpu=False: [sys.executable, str(FAKE_SERVER), str(port), mode]


class ServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for n in ("llama-server.exe", "m.gguf", "p.gguf"):
            (self.tmp / n).write_bytes(b"x")

    def _server(self):
        return ll.LlamaServer(self.tmp / "llama-server.exe", self.tmp / "m.gguf", self.tmp / "p.gguf", self.tmp,
                              startup_timeout=20)

    def test_start_ocr_close(self):
        with mock.patch.object(ll, "server_command", _fake_cmd("مرحبا")), \
                mock.patch.dict(os.environ, {"HTTP_PROXY": "http://10.255.255.1:1", "NO_PROXY": ""}):
            server = self._server()
            url = server.start()
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            provider = ll.LocalLlamaProvider(url, server=server)
            res = provider.ocr(OcrRequest([PageImage(3, b"png", 150)]))
            self.assertEqual((res[0].page_number, res[0].markdown), (3, "مرحبا"))  # بدون ``` وتجاهل الـ proxy
            proc = server.proc
            provider.close()
            self.assertIsNotNone(proc.poll())
            self.assertFalse(server.alive())

    def test_crash_on_start_reported(self):
        with mock.patch.object(ll, "server_command", _fake_cmd("crash")):
            server = self._server()
            with self.assertRaises(EngineUnavailable):
                server.start()
            self.assertFalse(server.alive())

    def test_missing_files(self):
        (self.tmp / "m.gguf").unlink()
        with self.assertRaises(EngineUnavailable):
            self._server().start()


# ------------------------------------------------------------ التنزيل والنسخ

class _RangeHandler(BaseHTTPRequestHandler):
    payload = b""
    ranges: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        type(self).ranges.append(rng)
        data, code = self.payload, 200
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            data, code = data[start:], 206
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _RangeHandler.payload = os.urandom(3 * mm.CHUNK + 123)
        cls.httpd = HTTPServer(("127.0.0.1", 0), _RangeHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}/f.gguf"
        cls.spec = cat.FileSpec("f.gguf", len(_RangeHandler.payload), hashlib.sha256(_RangeHandler.payload).hexdigest())
        cls.opener = ll._NO_PROXY_OPENER

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _RangeHandler.ranges = []

    def test_resume_with_range(self):
        dest = self.tmp / "f.gguf"
        part = dest.with_name("f.gguf.part")
        part.write_bytes(_RangeHandler.payload[:1000])
        mm.download_file(self.url, dest, self.spec, opener=self.opener)
        self.assertEqual(_RangeHandler.ranges, ["bytes=1000-"])
        self.assertEqual(dest.read_bytes(), _RangeHandler.payload)
        self.assertTrue(mm.is_verified(dest, self.spec))
        self.assertFalse(part.exists())

    def test_cancel_then_resume(self):
        dest = self.tmp / "f.gguf"
        stop = threading.Event()
        def progress(name, done, total):
            if done >= mm.CHUNK:
                stop.set()
        with self.assertRaises(mm.Cancelled):
            mm.download_file(self.url, dest, self.spec, progress, stop, opener=self.opener)
        have = dest.with_name("f.gguf.part").stat().st_size
        self.assertGreater(have, 0)
        mm.download_file(self.url, dest, self.spec, opener=self.opener)
        self.assertEqual(_RangeHandler.ranges[-1], f"bytes={have}-")
        self.assertEqual(dest.read_bytes(), _RangeHandler.payload)

    def test_bad_hash_rejected_and_removed(self):
        bad = cat.FileSpec("f.gguf", self.spec.size, "0" * 64)
        dest = self.tmp / "f.gguf"
        with self.assertRaises(mm.IntegrityError):
            mm.download_file(self.url, dest, bad, opener=self.opener)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name("f.gguf.part").exists())

    def test_copy_verified(self):
        src = self.tmp / "src.gguf"
        src.write_bytes(_RangeHandler.payload)
        dest = self.tmp / "out" / "f.gguf"
        mm.copy_verified(src, dest, self.spec)
        self.assertTrue(mm.is_verified(dest, self.spec))
        with self.assertRaises(mm.IntegrityError):  # حجم مختلف
            mm.copy_verified(src, self.tmp / "x.gguf", cat.FileSpec("x", 5, self.spec.sha256))
        with self.assertRaises(mm.IntegrityError):  # بصمة مختلفة
            mm.copy_verified(src, self.tmp / "y.gguf", cat.FileSpec("y", self.spec.size, "1" * 64))
        self.assertFalse((self.tmp / "y.gguf").exists())

    def test_import_from_folder_requires_both_files(self):
        model_b, proj_b = b"model-bytes" * 10, b"proj-bytes" * 7
        spec = cat.ModelSpec("tiny", "Tiny", "x/y",
                             cat.FileSpec("tiny.gguf", len(model_b), hashlib.sha256(model_b).hexdigest()),
                             cat.FileSpec("mmproj-tiny.gguf", len(proj_b), hashlib.sha256(proj_b).hexdigest()))
        half = cat.ModelSpec("half", "Half", "x/z", cat.FileSpec("half.gguf", 3, "0" * 64),
                             cat.FileSpec("mmproj-half.gguf", 3, "0" * 64))
        flash = self.tmp / "flash" / "sub"
        flash.mkdir(parents=True)
        (flash / "tiny.gguf").write_bytes(model_b)
        (flash / "mmproj-tiny.gguf").write_bytes(proj_b)
        (flash / "half.gguf").write_bytes(b"abc")
        with mock.patch.object(cat, "MODELS", (spec, half)):
            rep = mm.import_from_folder(self.tmp / "flash")
            self.assertEqual(rep["models"], ["tiny"])
            self.assertTrue(any("mmproj-half.gguf" in s for s in rep["skipped"]))
            self.assertTrue(mm.is_installed(spec))
            self.assertEqual([m.id for m in mm.installed_models()], ["tiny"])


class CatalogTests(unittest.TestCase):
    def test_every_model_has_both_files_and_hashes(self):
        ids = set()
        for m in cat.MODELS:
            self.assertNotIn(m.id, ids)
            ids.add(m.id)
            for f in (m.model, m.mmproj):
                self.assertTrue(f.name.endswith(".gguf"))
                self.assertRegex(f.sha256, r"^[0-9a-f]{64}$")
                self.assertGreater(f.size, 10_000_000)
            self.assertIn("mmproj", m.mmproj.name.lower())
            self.assertTrue(m.url(m.model).startswith("https://huggingface.co/"))


# ------------------------------------------------------------ الفحص المستقل والمسار

class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def ocr(self, request):
        self.calls += [i.page_number for i in request.images]
        return [PageResult(i.page_number, self.fn(i.page_number)) for i in request.images]


class CrossCheckTests(unittest.TestCase):
    def test_score(self):
        self.assertEqual(match_score("عقد إيجار بين الطرفين", "# عقد ايجار بين الطرفين"), 1.0)
        self.assertLess(match_score("املهمات االستخدام اإلدارة ملن", "المهمات الاستخدام الإدارة لمن"), 0.3)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = build(self.tmp / "in" / "عقد.pdf")
        self.out = self.tmp / "out"
        self.cache = HashCache(self.tmp / "cache.json")
        self.job = Job(self.pdf, self.cache.get(self.pdf))
        with fitz.open(self.pdf) as d:
            self.page1 = page_text_to_markdown(d[0])

    def _run(self, provider, verify=True, events=None):
        s = Settings()
        s.verify_text_with_ocr = verify
        emit = (lambda k, **kw: events.append((k, kw))) if events is not None else (lambda *a, **k: None)
        run_batch([self.job], BatchContext(self.out, s, threading.Event(), emit, self.cache, provider))

    def _state(self):
        return json.loads((self.out / ".state" / f"{self.job.file_hash}.json").read_text(encoding="utf-8"))

    def test_matching_ocr_passes(self):
        p = ScriptedProvider(lambda n: self.page1 if n == 1 else "صفحة مصوّرة")
        self._run(p)
        self.assertIn(1, p.calls)
        chk = self._state()["pages"]["1"]["ocr_check"]
        self.assertTrue(chk["ok"])
        self.assertEqual(self._state()["pages"]["1"]["method"], "text_layer")

    def test_weak_match_flagged(self):
        events = []
        p = ScriptedProvider(lambda n: "نص مختلف تماماً لا علاقة له")
        self._run(p, events=events)
        st = self._state()["pages"]["1"]
        self.assertFalse(st["ocr_check"]["ok"])
        md = (self.out / "docs" / "عقد.md").read_text(encoding="utf-8")
        self.assertIn("<!-- check:weak_match", md)
        self.assertIn("عقد إيجار", md)  # يبقى نص الطبقة؛ التنبيه فقط
        self.assertTrue((self.out / ".state" / self.job.file_hash / "p0001.ocr.md").exists())
        done = [kw for k, kw in events if k == "file_done"][0]
        self.assertEqual(done["weak_pages"], [1])
        self.assertTrue(any("تطابق ضعيف" in kw.get("text", "") for k, kw in events if k == "log"))

    def test_existing_text_pages_checked_later_once(self):
        self._run(None, verify=False)
        p = ScriptedProvider(lambda n: self.page1)
        self._run(p)
        self.assertEqual(sorted(p.calls), [1, 2, 4, 5])  # النصية 1 + المصوّرة المعلّقة
        p2 = ScriptedProvider(lambda n: self.page1)
        self._run(p2)
        self.assertEqual(p2.calls, [])

    def test_off_by_default(self):
        p = ScriptedProvider(lambda n: "x")
        self._run(p, verify=False)
        self.assertNotIn(1, p.calls)

    def test_only_pending_pages_go_to_engine(self):
        self._run(None, verify=False)
        before = self._state()["pages"]
        p = ScriptedProvider(lambda n: f"مقروء {n}")
        self._run(p, verify=False)
        pending = sorted(int(k) for k, v in before.items() if v["status"] == "pending_engine")
        self.assertEqual(sorted(p.calls), pending)

    def test_engine_death_stops_batch(self):
        def die(n):
            raise EngineUnavailable("مات")
        events = []
        self._run(ScriptedProvider(die), verify=False, events=events)
        self.assertEqual([k for k, _ in events if k == "file_error"], ["file_error"])
        self.assertEqual(events[-1], ("finished", {"stopped": True}))
        st = self._state()["pages"]
        self.assertFalse(any(v["status"] == "error" for v in st.values()))


# ------------------------------------------------------------ benchmark

class BenchmarkTests(unittest.TestCase):
    def test_parse_pages(self):
        self.assertEqual(benchmark.parse_pages("2, 4-5", 9), [2, 4, 5])
        with self.assertRaises(ValueError):
            benchmark.parse_pages("1-6", 9)
        with self.assertRaises(ValueError):
            benchmark.parse_pages("10", 9)

    def test_uninstalled_models_excluded_with_reason(self):
        runnable, excluded = benchmark.select_models(["qwen3vl-4b", "no-such-model"])
        self.assertEqual(runnable, [])
        self.assertIn("غير مثبّت", excluded["Qwen3-VL 4B (Q4_K_M)"])
        self.assertIn("ليس في الكتالوج", excluded["no-such-model"])

    def test_report_html(self):
        tmp = Path(tempfile.mkdtemp())
        pdf = build(tmp / "s.pdf")
        spec = cat.MODELS[0]
        rep = benchmark.Report(pdf.name, [1, 2], excluded={"X": "سبب"})
        prov = ScriptedProvider(lambda n: f"<b>صفحة {n}</b>")
        benchmark.run(pdf, [1, 2], [spec], rep, provider_factory=lambda m: prov, log=lambda *a: None)
        self.assertEqual(len(prov.calls), 4)  # صفحتان × دقتان
        html_out = benchmark.render_html(rep)
        self.assertIn("&lt;b&gt;صفحة 2&lt;/b&gt;", html_out)  # النص مُهرّب
        self.assertIn("150 DPI", html_out)
        self.assertIn("200 DPI", html_out)
        self.assertIn("سبب", html_out)
        self.assertEqual(html_out.count("<img "), 2)

    def test_cli_refuses_missing_and_too_many_pages(self):
        tmp = Path(tempfile.mkdtemp())
        pdf = build(tmp / "s.pdf")
        self.assertEqual(benchmark.main([str(pdf), "--pages", "1,2,3,4,5,1-2,6"]), 2)
        out = tmp / "r.html"
        self.assertEqual(benchmark.main([str(pdf), "--pages", "1", "--out", str(out)]), 0)
        self.assertIn("المستبعدة", out.read_text(encoding="utf-8"))


class ModelLabTests(unittest.TestCase):
    """مختبر النماذج (تبويب "داخلي"): الدقة مقابل المرجع، الترتيب، السجل، الإيقاف."""

    def setUp(self):
        from app import model_lab as lab
        self.lab = lab
        self.tmp = Path(tempfile.mkdtemp())
        self.pdf = build(self.tmp / "s.pdf")

    def _two_models(self, good_text):
        a, b = cat.MODELS[0], cat.MODELS[1]
        provs = {a.id: ScriptedProvider(good_text), b.id: ScriptedProvider(lambda n: "كلام لا علاقة له بالصفحة")}
        return [a, b], provs

    def test_text_layer_reference_and_accuracy_ranking(self):
        lab = self.lab
        ref = lab.text_layer_reference(self.pdf, [1, 2, 3])
        self.assertTrue(ref)                                # في العينة صفحات نصية
        pages = sorted(ref)[:2]
        models, provs = self._two_models(lambda n: ref[n])
        rep = lab.Report(self.pdf.name, pages, reference=ref)
        seen = []
        lab.run(self.pdf, pages, models, rep, provider_factory=lambda m: provs[m.id], log=lambda *a: None,
                dpis=(150,), on_cell=lambda *a: seen.append(a[:3]))
        self.assertEqual(len(seen), 2 * len(pages))
        rows = lab.ranking(rep)
        self.assertEqual([r.model_id for r in rows], [models[0].id, models[1].id])
        self.assertEqual(rows[0].accuracy, 1.0)
        self.assertLess(rows[1].accuracy, 0.3)
        html_out = lab.render_html(rep)
        self.assertIn("الترتيب", html_out)
        self.assertIn("النص المرجعي", html_out)

    def test_reference_file_pages(self):
        p = self.tmp / "ref.txt"
        p.write_text("=== صفحة 2 ===\nنص الثانية\n=== صفحة 5 ===\nنص الخامسة\n", encoding="utf-8")
        self.assertEqual(self.lab.parse_reference_file(p), {2: "نص الثانية", 5: "نص الخامسة"})
        p.write_text("نص بلا فواصل", encoding="utf-8")
        self.assertEqual(self.lab.parse_reference_file(p), {0: "نص بلا فواصل"})

    def test_history_saved_without_document_text(self):
        lab = self.lab
        models, provs = self._two_models(lambda n: "نص سري جداً من المستند")
        rep = lab.Report(self.pdf.name, [1])
        lab.run(self.pdf, [1], models, rep, provider_factory=lambda m: provs[m.id], log=lambda *a: None, dpis=(150, 200))
        hist = self.tmp / "h.jsonl"
        self.assertEqual(lab.save_history(rep, hist), 4)
        self.assertEqual(lab.save_history(rep, hist), 4)
        self.assertNotIn("سري", hist.read_text(encoding="utf-8"))
        summary = lab.history_summary(lab.load_history(hist))
        self.assertEqual(len(summary), 4)
        self.assertTrue(all(r["runs"] == 2 for r in summary))

    def test_stop_raises_and_closes_engine(self):
        lab = self.lab
        stop = threading.Event()
        models, provs = self._two_models(lambda n: (stop.set(), "x")[1])
        closed = []
        for p in provs.values():
            p.close = lambda p=p: closed.append(p)
        with self.assertRaises(lab.Stopped):
            lab.run(self.pdf, [1, 2], models, lab.Report(self.pdf.name, [1, 2]),
                    provider_factory=lambda m: provs[m.id], log=lambda *a: None, stop=stop, dpis=(150,))
        self.assertEqual(len(closed), 1)                    # المحرّك الذي بدأ أُغلق


if __name__ == "__main__":
    unittest.main()
