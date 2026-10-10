"""اختبارات المرحلة 3: البوابة، فحص الاكتمال، الانتقال بين المزوّدات، التوزيع، الاستئناف، المفاتيح.

كل الطلبات تذهب لخادم وهمي على 127.0.0.1 — لا يُرسل أي طلب حقيقي.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("PDF2MD_DATA_DIR", tempfile.mkdtemp(prefix="pdf2md-test-data-"))
os.environ["PDF2MD_SECRETS_BACKEND"] = "memory"

import pymupdf as fitz  # noqa: E402

from app import checkpoint as cp  # noqa: E402
from app.audit_log import AuditLog  # noqa: E402
from app.cloud_gate import CLOUD, LOCAL, CloudBlockedError, CloudGate, FileChangedError  # noqa: E402
from app.config import Settings  # noqa: E402
from app.dispatcher import ApiRunner, Slot  # noqa: E402
from app.hashing import HashCache, sha256_file  # noqa: E402
from app.pipeline import BatchContext, Job, run_batch  # noqa: E402
from app.providers.base import OcrRequest, PageImage, QuotaExhausted, RateLimited  # noqa: E402
from app.providers.batching import build_prompt, chunk_pages, run_with_split, split_and_check  # noqa: E402
from app.providers.catalog import CATALOG_PATH, ProviderConfig, ProviderSpec, ProviderState, load_catalog  # noqa: E402
from app.providers.openai_compat import OpenAICompatProvider, classify_http_error, parse_retry_after  # noqa: E402
from app.rate_limit import RateLimiter, UsageCounter  # noqa: E402
from app.secrets_store import REDACTED, RedactingFilter, SecretsStore, _MemoryBackend  # noqa: E402
from fake_openai_server import FakeOpenAI, good_text  # noqa: E402

SECRET = "sk-TEST-SECRET-0123456789abcdef"


def make_scanned_pdf(path: Path, pages: int) -> Path:
    """PDF مصوّر بالكامل (صور فقط، بلا طبقة نص)."""
    src = fitz.open()
    p = src.new_page(width=420, height=595)
    for i in range(18):
        p.insert_text((40, 60 + i * 28), "Lorem ipsum dolor sit amet consectetur " * 2, fontsize=11)
    png = p.get_pixmap(dpi=100).tobytes("png")
    out = fitz.open()
    for _ in range(pages):
        out.new_page(width=420, height=595).insert_image(fitz.Rect(0, 0, 420, 595), stream=png)
    out.save(path)
    out.close()
    src.close()
    return path


class Env:
    """بيئة اختبار كاملة: بوابة (وضع "عبر المواقع")، سجل إرسال، مجلد إخراج."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pdf2md-cloud-"))
        self.audit = AuditLog(self.tmp / "sent_log.csv")
        self.gate = CloudGate(self.audit, mode=CLOUD)
        self.usage = UsageCounter(self.tmp / "usage.json")
        self.out = self.tmp / "out"
        self.hash_cache = HashCache(self.tmp / "hc.json")
        self.events: list[tuple] = []
        self.servers: list[FakeOpenAI] = []
        self.sleeps: list[float] = []

    def pdf(self, name: str, pages: int) -> Path:
        return make_scanned_pdf(self.tmp / name, pages)

    def slot(self, pid: str, behavior=None, max_pages: int = 5, rpm: int = 1000) -> Slot:
        fake = FakeOpenAI(behavior)
        self.servers.append(fake)
        spec = ProviderSpec(id=pid, name=pid, kind="openai", base_url=fake.url, max_pages_per_call=max_pages)
        prov = OpenAICompatProvider(pid, fake.url, SECRET, "test-model", max_pages)
        lim = RateLimiter(pid, rpm, self.usage, sleep=self.sleeps.append)
        s = Slot(spec, ProviderState(enabled=True, model="test-model"), prov, lim)
        s.fake = fake
        return s

    def settings(self, batch: int = 1) -> Settings:
        s = Settings()
        s.api_pages_per_call = batch
        s.api_for_scanned = True
        s.api_dpi = 72
        return s

    def ctx(self, slots, settings, stop=None) -> BatchContext:
        stop = stop or threading.Event()
        emit = lambda kind, **kw: self.events.append((kind, kw))  # noqa: E731
        runner = ApiRunner(self.gate, slots, settings, self.out / ".state", self.out / "docs", emit, stop)
        return BatchContext(self.out, settings, stop, emit, self.hash_cache, provider=None, api=runner)

    def run(self, paths, slots, batch=1, stop=None):
        jobs = [Job(p, self.hash_cache.get(p)) for p in paths]
        ctx = self.ctx(slots, self.settings(batch), stop)
        run_batch(jobs, ctx)
        return ctx

    def state(self, path: Path) -> cp.FileState:
        with fitz.open(path) as d:
            return cp.FileState(self.out / ".state", sha256_file(path), path.name, d.page_count)

    def close(self):
        for s in self.servers:
            s.close()


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()


# ---------------------------------------------------------------- 1. القفل
class GateLockTests(EnvCase):
    def _req(self):
        return OcrRequest([PageImage(1, b"\xff\xd8x", 72, "image/jpeg", 0.0)])

    def test_default_mode_is_local(self):
        gate = CloudGate(self.env.audit)
        self.assertEqual(gate.mode, LOCAL)
        self.assertFalse(gate.allowed())

    def test_local_mode_blocks_open_send_and_send_text(self):
        p = self.env.pdf("loc.pdf", 1)
        s = self.env.slot("p1")
        t = self.env.gate.open_file(p)          # فُتح في وضع "عبر المواقع"
        self.env.gate.set_mode(LOCAL)
        with self.assertRaises(CloudBlockedError):
            self.env.gate.open_file(p)
        with self.assertRaises(CloudBlockedError):
            self.env.gate.send(t, s.provider, "x", self._req())
        with self.assertRaises(CloudBlockedError):
            self.env.gate.send_text(t, s.provider, "x", "نص")
        self.assertEqual(s.fake.calls, [])
        self.assertEqual(self.env.audit.rows(), [])

    def test_unknown_mode_falls_back_to_local(self):
        self.env.gate.set_mode("anything")
        self.assertEqual(self.env.gate.mode, LOCAL)

    def test_pipeline_in_local_mode_never_sends(self):
        a = self.env.pdf("a.pdf", 2)
        b = self.env.pdf("b.pdf", 2)
        s = self.env.slot("p1")
        self.env.gate.set_mode(LOCAL)
        self.env.run([a, b], [s])
        self.assertEqual(s.fake.calls, [])
        self.assertEqual(self.env.audit.rows(), [])
        self.assertEqual(self.env.state(a).status(1), cp.PENDING_ENGINE)

    def test_switch_to_local_mid_batch_blocks_next_send(self):
        p = self.env.pdf("flip.pdf", 1)
        s = self.env.slot("p1")
        t = self.env.gate.open_file(p)
        self.env.gate.send(t, s.provider, "x", self._req())
        self.env.gate.set_mode(LOCAL)
        with self.assertRaises(CloudBlockedError):
            self.env.gate.send(t, s.provider, "x", self._req())
        self.assertEqual(len(s.fake.calls), 1)

    def test_changed_file_is_rehashed_and_refused(self):
        p = self.env.pdf("chg.pdf", 1)
        s = self.env.slot("p1")
        t = self.env.gate.open_file(p)
        with open(p, "ab") as f:
            f.write(b"\n% changed")
        with self.assertRaises(FileChangedError):
            self.env.gate.send(t, s.provider, "x", self._req())
        self.assertEqual(s.fake.calls, [])

    def test_touch_without_content_change_is_allowed(self):
        p = self.env.pdf("touch.pdf", 1)
        s = self.env.slot("p1")
        t = self.env.gate.open_file(p)
        os.utime(p, ns=(t.mtime_ns + 10_000_000_000, t.mtime_ns + 10_000_000_000))
        self.env.gate.send(t, s.provider, "x", self._req())
        self.assertEqual(len(s.fake.calls), 1)

    def test_provider_cannot_be_called_outside_gate(self):
        s = self.env.slot("p1")
        with self.assertRaises(PermissionError):
            s.provider.ocr(self._req())
        with self.assertRaises(PermissionError):
            s.provider.send("x", self._req(), object())
        self.assertEqual(s.fake.calls, [])

    def test_probe_sends_only_synthetic_image(self):
        s = self.env.slot("p1", lambda pages, i: (200, {}, "PDF2MD CONNECTION TEST"))
        self.env.gate.probe(s.provider, "m")
        self.assertEqual(s.fake.calls[0]["images"], 1)
        row = self.env.audit.rows()[0]
        self.assertEqual(row["sha256"], "-")
        self.assertEqual(row["result"], "probe")


# ---------------------------------------------------------------- 2. فحص الاكتمال
def _img(n, ink=0.05):
    return PageImage(n, b"x", 72, "image/jpeg", ink)


class CompletenessTests(unittest.TestCase):
    def test_all_markers_in_order(self):
        r = split_and_check(good_text([3, 4]), [_img(3), _img(4)])
        self.assertTrue(r.ok)
        self.assertIn("صفحة 4", r.pages[4])

    def test_missing_marker_fails(self):
        r = split_and_check(good_text([3]), [_img(3), _img(4)])
        self.assertFalse(r.ok)

    def test_out_of_order_fails(self):
        r = split_and_check(good_text([4, 3]), [_img(3), _img(4)])
        self.assertFalse(r.ok)

    def test_short_page_fails(self):
        text = good_text([1]) + "\n<!-- page:2 -->\nقصير"
        r = split_and_check(text, [_img(1), _img(2)])
        self.assertFalse(r.ok)
        self.assertTrue(any("2" in p for p in r.problems))

    def test_blank_page_with_ink_fails_but_real_blank_ok(self):
        self.assertFalse(split_and_check("<!-- page:1 -->\n<!-- blank -->", [_img(1, 0.05)]).ok)
        self.assertTrue(split_and_check("<!-- page:1 -->\n<!-- blank -->", [_img(1, 0.0)]).ok)

    def test_prompt_lists_exact_markers(self):
        p = build_prompt([7, 8], "سطر سابق")
        self.assertIn("<!-- page:7 --> <!-- page:8 -->", p)
        self.assertIn("سطر سابق", p)
        self.assertIn("table-continues", p)

    def test_chunks_only_consecutive(self):
        self.assertEqual(chunk_pages([1, 2, 3, 5, 6, 9], 2), [[1, 2], [3], [5, 6], [9]])

    def test_dropped_page_triggers_split(self):
        calls = []

        def send(prompt, req):
            pages = [i.page_number for i in req.images]
            calls.append(pages)
            return good_text([p for p in pages if not (len(pages) > 1 and p == 2)])

        out = run_with_split(send, [_img(1), _img(2), _img(3)])
        self.assertEqual(calls, [[1, 2, 3], [1], [2], [3]])
        self.assertTrue(all(o.markdown for o in out))
        self.assertEqual({o.batch_size for o in out}, {1})

    def test_single_page_failure_held_for_review(self):
        out = run_with_split(lambda p, r: "no markers", [_img(1), _img(2)])
        self.assertTrue(all(o.markdown is None for o in out))

    def test_pipeline_split_then_review(self):
        env = Env()
        try:
            p = env.pdf("doc.pdf", 4)
            # يُسقط الصفحة 2 دائماً: تُعاد الدفعة صفحةً صفحة، و2 تُعلَّق ولا يُكتب نصها.
            s = env.slot("p1", lambda pages, i: (200, {}, good_text([x for x in pages if x != 2])))
            env.run([p], [s], batch=2)
            st = env.state(p)
            self.assertEqual([st.status(n) for n in (1, 2, 3, 4)], [cp.DONE, cp.NEEDS_REVIEW, cp.DONE, cp.DONE])
            self.assertEqual(st.read_fragment(2), "")
            self.assertEqual(s.fake.calls[0]["pages"], [1, 2])
            md = (env.out / "docs" / "doc.md").read_text(encoding="utf-8")
            self.assertIn("<!-- review:api_incomplete -->", md)
            self.assertIn("<!-- providers: p1: 1,3-4 -->", md)
            self.assertTrue(any(k == "review_needed" for k, _ in env.events))
        finally:
            env.close()


# ---------------------------------------------------------------- 3. الانتقال عند 429
class FailoverTests(EnvCase):
    def test_429_quota_mid_file_moves_rest_to_other_provider_same_run(self):
        f1, f2 = self.env.pdf("f1.pdf", 4), self.env.pdf("f2.pdf", 4)
        # a: أول طلبين ينجحان ثم تنتهي الحصة اليومية في منتصف ملفه
        sa = self.env.slot("a", lambda pages, i: (200, {}, good_text(pages)) if i < 2
                           else (429, {}, "Rate limit reached on requests per day (RPD)"))
        sb = self.env.slot("b")
        self.env.run([f1, f2], [sa, sb])
        self.assertTrue(self.env.usage.is_exhausted("a"))
        mixed = None
        for f in (f1, f2):
            st = self.env.state(f)
            self.assertTrue(all(st.status(n) == cp.DONE for n in range(1, 5)), f)
            methods = [st.page(n)["method"] for n in range(1, 5)]
            if "api:a" in methods:
                mixed = (f, methods)
        self.assertIsNotNone(mixed)
        self.assertEqual(mixed[1], ["api:a", "api:a", "api:b", "api:b"])
        md = (self.env.out / "docs" / (mixed[0].stem + ".md")).read_text(encoding="utf-8")
        self.assertIn("<!-- providers: a: 1-2 | b: 3-4 -->", md)
        log_rows = [r for r in self.env.audit.rows() if r["file"] == mixed[0].name and r["result"] == "ok"]
        self.assertEqual([(r["page"], r["provider"]) for r in log_rows],
                         [("1", "a"), ("2", "a"), ("3", "b"), ("4", "b")])

    def test_all_providers_exhausted_stops_and_resumes(self):
        p = self.env.pdf("doc.pdf", 5)
        sa = self.env.slot("a", lambda pages, i: (200, {}, good_text(pages)) if i < 2
                           else (429, {}, "quota exceeded per day"))
        self.env.run([p], [sa])
        st = self.env.state(p)
        self.assertEqual([st.status(n) for n in (1, 2, 3)], [cp.DONE, cp.DONE, cp.PENDING_ENGINE])
        self.assertTrue(any("انتهت الحصة اليومية" in kw.get("text", "") for k, kw in self.env.events if k == "log"))
        self.assertTrue([kw for k, kw in self.env.events if k == "finished"][-1]["stopped"])
        sb = self.env.slot("b")
        self.env.run([p], [sb])
        st = self.env.state(p)
        self.assertEqual([st.page(n)["method"] for n in range(1, 6)], ["api:a"] * 2 + ["api:b"] * 3)
        self.assertEqual([c["pages"] for c in sb.fake.calls], [[3], [4], [5]])

    def test_retry_after_is_respected(self):
        p = self.env.pdf("doc.pdf", 1)
        s = self.env.slot("a", lambda pages, i: (429, {"Retry-After": "17"}, "slow down per minute") if i == 0
                          else (200, {}, good_text(pages)))
        self.env.run([p], [s])
        self.assertEqual(self.env.state(p).status(1), cp.DONE)
        self.assertIn(17.0, self.env.sleeps)

    def test_backoff_is_exponential(self):
        p = self.env.pdf("doc.pdf", 1)
        s = self.env.slot("a", lambda pages, i: (429, {}, "per minute") if i < 3 else (200, {}, good_text(pages)))
        self.env.run([p], [s])
        self.assertEqual(self.env.sleeps[:3], [2.0, 4.0, 8.0])

    def test_error_classification(self):
        self.assertIsInstance(classify_http_error(429, "5", "Too many per minute"), RateLimited)
        self.assertIsInstance(classify_http_error(429, None, "requests per day"), QuotaExhausted)
        self.assertIsInstance(classify_http_error(402, None, ""), QuotaExhausted)
        self.assertEqual(parse_retry_after("2.5"), 2.5)
        self.assertEqual(parse_retry_after("1m"), 60)


# ---------------------------------------------------------------- 4. التوزيع بالتوازي
class ParallelTests(EnvCase):
    def test_files_distributed_whole_and_in_parallel(self):
        barrier = threading.Barrier(2, timeout=10)
        seen_parallel = []

        def beh(pages, i):
            if i == 0:  # أول طلب لكل مزوّد ينتظر الآخر: يثبت أن العاملين يعملان معاً
                try:
                    barrier.wait()
                    seen_parallel.append(True)
                except threading.BrokenBarrierError:
                    pass
            return 200, {}, good_text(pages)

        files = [self.env.pdf(f"f{i}.pdf", 3) for i in range(4)]
        sa, sb = self.env.slot("a", beh), self.env.slot("b", beh)
        self.env.run(files, [sa, sb])
        self.assertEqual(len(seen_parallel), 2)
        used = set()
        for f in files:
            st = self.env.state(f)
            methods = {st.page(n)["method"] for n in (1, 2, 3)}
            self.assertEqual(len(methods), 1, "كل ملف يعالجه مزوّد واحد")
            used |= methods
        self.assertEqual(used, {"api:a", "api:b"})

    def test_batch_size_capped_by_provider(self):
        p = self.env.pdf("doc.pdf", 5)
        s = self.env.slot("small", max_pages=1)
        self.env.run([p], [s], batch=5)
        self.assertTrue(all(c["images"] == 1 for c in s.fake.calls))
        self.assertEqual(self.env.state(p).page(1)["batch_size"], 1)

    def test_batch_of_two(self):
        p = self.env.pdf("doc.pdf", 5)
        s = self.env.slot("big")
        self.env.run([p], [s], batch=2)
        self.assertEqual([c["pages"] for c in s.fake.calls], [[1, 2], [3, 4], [5]])


# ---------------------------------------------------------------- 5. الاستئناف
class ResumeTests(EnvCase):
    def test_resume_after_stop_continues_from_same_page(self):
        p = self.env.pdf("doc.pdf", 6)
        stop = threading.Event()

        def beh(pages, i):
            if i == 2:
                stop.set()  # المستخدم ضغط إيقاف أثناء الطلب الثالث
            return 200, {}, good_text(pages)

        s = self.env.slot("a", beh)
        self.env.run([p], [s], stop=stop)
        st = self.env.state(p)
        done = [n for n in range(1, 7) if st.status(n) == cp.DONE]
        self.assertEqual(done, [1, 2, 3])
        s2 = self.env.slot("a2")
        self.env.run([p], [s2])
        self.assertEqual([c["pages"] for c in s2.fake.calls], [[4], [5], [6]])
        st = self.env.state(p)
        self.assertTrue(all(st.status(n) == cp.DONE for n in range(1, 7)))

    def test_daily_counter_and_rpd(self):
        u = UsageCounter(self.env.tmp / "u.json", today=lambda: "2026-10-05")
        lim = RateLimiter("x", 100, u, rpd=2, sleep=lambda s: None)
        lim.acquire()
        lim.acquire()
        with self.assertRaises(QuotaExhausted):
            lim.acquire()
        self.assertEqual(UsageCounter(self.env.tmp / "u.json", today=lambda: "2026-10-05").get("x"), 2)
        self.assertEqual(UsageCounter(self.env.tmp / "u.json", today=lambda: "2026-10-06").get("x"), 0)

    def test_rpm_window(self):
        t = [0.0]
        waits = []

        def sleep(s):
            waits.append(s)
            t[0] += s

        lim = RateLimiter("x", 2, UsageCounter(self.env.tmp / "u2.json"), clock=lambda: t[0], sleep=sleep)
        for _ in range(3):
            lim.acquire()
        self.assertEqual(len(waits), 1)
        self.assertGreaterEqual(waits[0], 59.9)


# ---------------------------------------------------------------- 6. المفاتيح لا تظهر في أي سجل
class SecretsTests(EnvCase):
    def test_key_never_in_logs_or_files(self):
        store = SecretsStore(_MemoryBackend())
        store.set_key("a", SECRET)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.addFilter(RedactingFilter())
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            p = self.env.pdf("doc.pdf", 2)
            # الخادم يردّد المفتاح في رسالة الخطأ (أسوأ حالة)
            s = self.env.slot("a", lambda pages, i: (500, {}, f"bad key {SECRET}") if i == 0
                              else (200, {}, good_text(pages)))
            self.env.run([p], [s])
            logging.getLogger("x").error("تسرّب محتمل %s", SECRET)
            try:
                raise RuntimeError(f"boom {SECRET}")
            except RuntimeError:
                logging.getLogger("x").exception("استثناء")
        finally:
            root.removeHandler(handler)
            root.setLevel(old)
        self.assertEqual(s.fake.calls[0]["auth"], f"Bearer {SECRET}")  # أُرسل للمزوّد فقط
        text = buf.getvalue()
        self.assertNotIn(SECRET, text)
        self.assertIn(REDACTED, text)
        for f in self.env.tmp.rglob("*"):
            if f.is_file() and f.suffix != ".pdf":
                self.assertNotIn(SECRET, f.read_text(encoding="utf-8", errors="ignore"), f)
        self.assertNotIn(SECRET, json.dumps(self.env.events, ensure_ascii=False, default=str))

    def test_settings_file_has_no_keys(self):
        self.assertFalse(any("key" in f for f in Settings.__dataclass_fields__))


# ---------------------------------------------------------------- 7. الكتالوج
class CatalogTests(unittest.TestCase):
    def test_catalog_loads_with_dates(self):
        specs = load_catalog(CATALOG_PATH)
        ids = {s.id for s in specs}
        self.assertTrue({"gemini", "groq", "mistral", "cloudflare", "nvidia", "openrouter"} <= ids)
        for s in specs:
            self.assertRegex(s.verified_on, r"^\d{4}-\d{2}-\d{2}$", s.id)

    def test_unverified_not_auto_enabled_and_retired_blocked(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = ProviderConfig(tmp / "ps.json")
        cfg.specs["verified"] = ProviderSpec(id="verified", name="v", kind="openai", base_url="https://x")
        self.assertTrue(cfg.on_key_saved("verified"))
        self.assertFalse(cfg.on_key_saved("mistral"))
        self.assertFalse(cfg.state("mistral").enabled)
        self.assertNotIn("github", {s.id for s in load_catalog()})

    def test_cloudflare_needs_account_id_and_counter_is_approx(self):
        cf = {s.id: s for s in load_catalog()}["cloudflare"]
        self.assertTrue(cf.approx_counter)
        with self.assertRaises(ValueError):
            cf.resolved_base_url({})
        self.assertEqual(cf.resolved_base_url({"account_id": "abc123"}),
                         "https://api.cloudflare.com/client/v4/accounts/abc123/ai/v1")

    def test_active_requires_key_and_orders_by_priority(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = ProviderConfig(tmp / "ps.json")
        store = SecretsStore(_MemoryBackend())
        cfg.state("groq").enabled = True
        cfg.state("groq").priority = 2
        cfg.state("openrouter").enabled = True
        cfg.state("openrouter").priority = 1
        self.assertEqual(cfg.active(store), [])
        store.set_key("groq", "k1-aaaaaa")
        store.set_key("openrouter", "k2-bbbbbb")
        self.assertEqual([s.id for s, _, _ in cfg.active(store)], ["openrouter", "groq"])

    def test_custom_provider_requires_https(self):
        cfg = ProviderConfig(Path(tempfile.mkdtemp()) / "ps.json")
        with self.assertRaises(ValueError):
            cfg.add_custom("x", "http://example.com/v1", "m")
        spec = cfg.add_custom("My Svc", "https://api.example.com/v1/", "vision-1")
        self.assertEqual(spec.base_url, "https://api.example.com/v1")
        self.assertIn(spec.id, ProviderConfig(cfg.state_path).specs)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 8. benchmark
class BenchmarkApiTests(EnvCase):
    def test_api_benchmark_reports_batch_and_missing_marker(self):
        import benchmark
        p = self.env.pdf("bench.pdf", 3)
        s = self.env.slot("p1", lambda pages, i: (200, {}, good_text([x for x in pages if x != 2])))
        rep = benchmark.Report(p.name, [1, 2, 3])
        benchmark.run_api(p, [1, 2, 3], rep, [s], self.env.gate, self.env.settings(batch=3), log=lambda *_: None)
        self.assertEqual(rep.api_batch["p1"], 3)
        self.assertTrue(any("علامة الصفحة 2" in w for w in rep.api_warnings))
        self.assertIsNotNone(rep.api_results[("p1", 1)].seconds)
        rep.images = {n: b"x" for n in (1, 2, 3)}
        html = benchmark.render_html(rep)
        self.assertIn("دفعة 3", html)

    def test_api_benchmark_refused_in_local_mode(self):
        import benchmark
        p = self.env.pdf("bench_s.pdf", 1)
        s = self.env.slot("p1")
        self.env.gate.set_mode(LOCAL)
        with self.assertRaises(CloudBlockedError):
            benchmark.run_api(p, [1], benchmark.Report(p.name, [1]), [s], self.env.gate, self.env.settings())
        self.assertEqual(s.fake.calls, [])


class GeminiAndStoreTests(unittest.TestCase):
    def test_gemini_error_mapping(self):
        from app.providers.gemini import map_gemini_error
        from app.providers.base import AuthError
        self.assertIsInstance(map_gemini_error(429, "Quota exceeded for GenerateRequestsPerDayPerProject"), QuotaExhausted)
        e = map_gemini_error(429, 'rate limit, "retryDelay": "31s"')
        self.assertIsInstance(e, RateLimited)
        self.assertEqual(e.retry_after, 31.0)
        self.assertIsInstance(map_gemini_error(400, "API key not valid"), AuthError)

    def test_gemini_uses_proxy_and_never_calls_outside_gate(self):
        from unittest import mock
        from app.providers.gemini import GeminiProvider
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9"}):
            with mock.patch("google.genai.Client") as client:
                prov = GeminiProvider(SECRET, "gemini-3.5-flash")
                opts = client.call_args.kwargs["http_options"]
                self.assertEqual(opts.client_args, {"proxy": "http://127.0.0.1:9"})
        with self.assertRaises(PermissionError):
            prov.ocr(OcrRequest([_img(1)]))
        self.assertFalse(client.return_value.models.generate_content.called)

    @unittest.skipUnless(os.name == "nt", "DPAPI على ويندوز فقط")
    def test_dpapi_roundtrip_not_plaintext(self):
        from app.secrets_store import _DpapiBackend
        path = Path(tempfile.mkdtemp()) / "secrets.dat"
        store = SecretsStore(_DpapiBackend(path))
        store.set_key("groq", SECRET)
        self.assertEqual(store.get_key("groq"), SECRET)
        self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))
        store.delete_key("groq")
        self.assertIsNone(store.get_key("groq"))


class ReviewFlowTests(EnvCase):
    def test_review_leave_and_send_with_clip(self):
        from app.pipeline import apply_review_decisions, run_api_phase
        p = self.env.pdf("rev.pdf", 3)
        s = self.env.slot("p1", lambda pages, i: (200, {}, good_text([x for x in pages if x not in (2, 3)])))
        ctx = self.env.run([p], [s])
        st = self.env.state(p)
        self.assertEqual([st.status(n) for n in (2, 3)], [cp.NEEDS_REVIEW, cp.NEEDS_REVIEW])
        # لا إعادة إرسال تلقائية لصفحة فشلت في المزوّد
        calls = len(s.fake.calls)
        self.env.run([p], [s])
        self.assertEqual(len(s.fake.calls), calls)
        h = sha256_file(p)
        decisions = [
            {"path": str(p), "hash": h, "page": 2, "index": 0, "action": "leave", "clip": None},
            {"path": str(p), "hash": h, "page": 3, "index": 0, "action": "send", "clip": (0, 0, 200, 150)},
        ]
        good = self.env.slot("p2")
        ctx = self.env.ctx([good], self.env.settings())
        self.assertEqual(apply_review_decisions(decisions, ctx), {0})
        jobs = [Job(p, h, api_ok=True)]
        run_api_phase(jobs, ctx, only_index={0})
        st = self.env.state(p)
        self.assertEqual(st.status(2), cp.ILLEGIBLE)
        self.assertEqual(st.status(3), cp.DONE)
        self.assertEqual([c["pages"] for c in good.fake.calls], [[3]])
        md = (self.env.out / "docs" / "rev.md").read_text(encoding="utf-8")
        self.assertIn("[غير مقروء]", md)
        self.assertNotIn("incomplete", md)


class LocalWeakPageTests(EnvCase):
    def test_weak_local_ocr_held_only_in_cloud_mode(self):
        from app.providers.base import PageResult, Provider

        class Local(Provider):
            name = "local"

            def ocr(self, req):
                return [PageResult(i.page_number, "x") for i in req.images]  # ناتج قصير بشكل مريب

        open_f = self.env.pdf("open.pdf", 1)
        sens_f = self.env.pdf("sens.pdf", 1)
        s = self.env.slot("p1")
        settings = self.env.settings()
        settings.api_for_scanned = False
        ctx = self.env.ctx([s], settings)
        ctx.provider = Local()
        run_batch([Job(open_f, sha256_file(open_f))], ctx)
        self.env.gate.set_mode(LOCAL)
        run_batch([Job(sens_f, sha256_file(sens_f))], ctx)
        self.assertEqual(self.env.state(open_f).status(1), cp.NEEDS_REVIEW)
        self.assertEqual(self.env.state(open_f).page(1)["review_reason"], "too_short")
        self.assertTrue(self.env.state(open_f).local_path(1).exists())
        self.assertEqual(self.env.state(sens_f).status(1), cp.DONE)  # الوضع الداخلي: لا يُعلَّق ولا يُرسل
        self.assertEqual(s.fake.calls, [])
        items = [kw["items"] for k, kw in self.env.events if k == "review_needed"][0]
        self.assertEqual([(Path(i["path"]).name, i["page"]) for i in items], [("open.pdf", 1)])


# ---------------------------------------------------------------- 9. حلقات التكرار (ناتج النموذج الصغير)
LOOP = "# عنوان\nإنه في يوم ٢٠٢٠ الساعة " + "م.م. " * 300


class RepetitionTests(EnvCase):
    def test_detector(self):
        from app.quality import repetition_problem
        for bad in (LOOP, "ملا " * 200, "خلال ٢٠٠" + "٠" * 300, "مَتْمَمَتْ " * 50,
                    "نص " + "حَسْبَة مَنْ " * 20, "| 1 | 0 |\n" * 40):
            self.assertIsNotNone(repetition_problem(bad), bad[:20])
        for good in ("| أ | ب |\n|---|---|\n" + "| 1 | 0 |\n" * 20,
                     "البند الأول: يلتزم الطرف الأول. البند الثاني: يلتزم الطرف الثاني. " * 3,
                     "---\n" * 10, ""):
            self.assertIsNone(repetition_problem(good), good[:20])

    def _local(self, text):
        from app.providers.base import PageResult, Provider

        class Local(Provider):
            name = "qwen-small"
            calls = 0

            def ocr(self, req):
                Local.calls += 1
                return [PageResult(i.page_number, text) for i in req.images]
        return Local()

    def test_local_loop_never_written_and_not_retried_with_same_model(self):
        f = self.env.pdf("sens.pdf", 2)
        ctx = self.env.ctx([], self.env.settings())
        ctx.api = None
        ctx.provider = self._local(LOOP)
        run_batch([Job(f, sha256_file(f))], ctx)
        st = self.env.state(f)
        self.assertEqual([st.status(1), st.status(2)], [cp.UNRELIABLE, cp.UNRELIABLE])
        md = (self.env.out / "docs" / "sens.md").read_text(encoding="utf-8")
        self.assertNotIn("م.م. م.م.", md)
        self.assertIn("ناتج المحرّك غير موثوق", md)
        calls = type(ctx.provider).calls
        run_batch([Job(f, sha256_file(f))], ctx)
        self.assertEqual(type(ctx.provider).calls, calls)  # نفس النموذج: لا إعادة
        other = self._local("نص سليم " + "كلمات مختلفة كثيرة هنا " * 3)
        other.name = "bigger-model"
        ctx.provider = other
        run_batch([Job(f, sha256_file(f))], ctx)
        self.assertEqual(self.env.state(f).status(1), cp.DONE)  # نموذج آخر: يُعاد

    def test_old_done_pages_rechecked_without_rerunning(self):
        f = self.env.pdf("old.pdf", 1)
        h = sha256_file(f)
        st = cp.FileState(self.env.out / ".state", h, f.name, 1)
        st.save_page(1, cp.DONE, "ocr:qwen-small", LOOP)  # ناتج قديم من قبل الإصلاح
        ctx = self.env.ctx([], self.env.settings())
        ctx.api = None
        run_batch([Job(f, h)], ctx)
        self.assertEqual(self.env.state(f).status(1), cp.UNRELIABLE)
        self.assertNotIn("م.م. م.م.", (self.env.out / "docs" / "old.md").read_text(encoding="utf-8"))

    def test_api_loop_output_fails_completeness(self):
        r = split_and_check("<!-- page:1 -->\n" + LOOP, [_img(1)])
        self.assertFalse(r.ok)

    def test_loop_goes_to_api_when_enabled_in_cloud_mode(self):
        f = self.env.pdf("open.pdf", 1)
        s = self.env.slot("p1")
        settings = self.env.settings()
        settings.auto_send_weak = True
        settings.api_for_scanned = False
        ctx = self.env.ctx([s], settings)
        ctx.provider = self._local(LOOP)
        run_batch([Job(f, sha256_file(f))], ctx)
        st = self.env.state(f)
        self.assertEqual(st.status(1), cp.DONE)
        self.assertEqual(st.page(1)["method"], "api:p1")


# ---------------------------------------------------------------- 10. ضغط الخادم (503)
class ServerBusyTests(EnvCase):
    BUSY = (503, {}, "This model is currently experiencing high demand.")

    def test_503_waits_longer_then_succeeds(self):
        p = self.env.pdf("doc.pdf", 2)
        s = self.env.slot("a", lambda pages, i: self.BUSY if i < 4 else (200, {}, good_text(pages)))
        self.env.run([p], [s])
        st = self.env.state(p)
        self.assertEqual([st.status(1), st.status(2)], [cp.DONE, cp.DONE])
        self.assertEqual(self.env.sleeps[:4], [5, 10, 20, 40])

    def test_busy_model_switches_immediately_and_records_it(self):
        from app.model_picker import ModelPicker
        p = self.env.pdf("doc.pdf", 2)
        # النموذج الأساسي مزدحم دائماً؛ البديل يعمل
        s = self.env.slot("a", lambda pages, i: self.BUSY if self._model(i) == "main" else (200, {}, good_text(pages)))
        s.picker = ModelPicker("a", ["main", "lite-model"])
        self.env.run([p], [s])
        st = self.env.state(p)
        self.assertEqual([st.status(1), st.status(2)], [cp.DONE, cp.DONE])
        self.assertEqual(st.page(1)["model"], "lite-model")
        self.assertEqual(self.env.sleeps, [])  # تحويل فوري بلا انتظار
        self.assertEqual([c["model"] for c in s.fake.calls], ["main", "lite-model", "lite-model"])
        self.assertTrue(any("التحويل تلقائياً إلى lite-model" in kw.get("text", "")
                            for k, kw in self.env.events if k == "log"))
        self.assertEqual([r["model"] for r in self.env.audit.rows() if r["result"] == "ok"], ["lite-model"] * 2)

    def _model(self, i):
        return self._srv.calls[i]["model"]

    def setUp(self):
        super().setUp()
        orig = self.env.slot

        def slot(*a, **k):
            s = orig(*a, **k)
            self._srv = s.fake
            return s
        self.env.slot = slot

    def test_picker_prefers_least_busy_and_skips_exhausted(self):
        from app.model_picker import ModelPicker
        t = [0.0]
        pk = ModelPicker("x", ["a", "b", "c"], clock=lambda: t[0])
        self.assertEqual(pk.current, "a")
        self.assertEqual(pk.switch("busy"), (True, "a", "b"))
        self.assertEqual(pk.switch("quota"), (True, "b", "c"))
        moved, _, cur = pk.switch("busy")        # كلها جُرّبت في هذه الجولة ⇒ انتظار
        self.assertFalse(moved)
        self.assertNotEqual(cur, "b")            # b انتهت حصته اليوم
        t[0] += 700                              # بعد 10 دقائق يُنسى الازدحام
        self.assertEqual(pk.best(), "a")

    def test_busy_everywhere_message_is_not_quota(self):
        p = self.env.pdf("doc.pdf", 1)
        s = self.env.slot("a", lambda pages, i: self.BUSY)
        self.env.run([p], [s])
        texts = [kw.get("text", "") for k, kw in self.env.events if k == "log"]
        self.assertTrue(any("مشغولة حالياً" in t for t in texts))
        self.assertFalse(any("انتهت الحصة" in t for t in texts))
        self.assertFalse(self.env.usage.is_exhausted("a"))
        self.assertEqual(self.env.state(p).status(1), cp.PENDING_ENGINE)

    def test_gemini_503_mapping(self):
        from app.providers.base import ServerBusy
        from app.providers.gemini import map_gemini_error
        self.assertIsInstance(map_gemini_error(503, "This model is currently experiencing high demand."), ServerBusy)
        self.assertIsInstance(classify_http_error(503, "30", ""), ServerBusy)
