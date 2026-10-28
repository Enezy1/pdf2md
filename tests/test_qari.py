"""Qari: مزوّد API (بنقل وهمي بلا شبكة) وإدخال النموذجين المحليين في الكتالوج."""
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import models_catalog as cat  # noqa: E402
from app.providers.base import AuthError, PageImage, QuotaExhausted  # noqa: E402
from app.providers.catalog import load_catalog  # noqa: E402
from app.providers.qari_api import QariApiProvider  # noqa: E402
from PIL import Image  # noqa: E402
import io  # noqa: E402


def _png():
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(b, "PNG")
    return b.getvalue()


def _provider(handler):
    return QariApiProvider("qari", "sk_test", transport=httpx.MockTransport(handler))


class QariApi(unittest.TestCase):
    def test_ok_sends_bearer_and_image(self):
        seen = {}

        def h(req):
            seen["auth"] = req.headers["authorization"]
            seen["body"] = req.content
            return httpx.Response(200, json={"success": True, "text": "مرحبا Hello", "confidence": 98.7})
        out = _provider(h)._complete("", [PageImage(3, _png(), 300)])
        self.assertEqual(seen["auth"], "Bearer sk_test")
        self.assertIn(b'name="image"', seen["body"])
        self.assertIn(b"auto", seen["body"])
        self.assertEqual(out, "<!-- page:3 -->\nمرحبا Hello")

    def test_blank(self):
        out = _provider(lambda r: httpx.Response(200, json={"success": True, "text": ""}))._complete(
            "", [PageImage(1, _png(), 300)])
        self.assertIn("<!-- blank -->", out)

    def test_auth_and_quota(self):
        with self.assertRaises(AuthError):
            _provider(lambda r: httpx.Response(401, json={"success": False, "error": "Missing API key"}))._complete(
                "", [PageImage(1, _png(), 300)])
        with self.assertRaises(QuotaExhausted):
            _provider(lambda r: httpx.Response(429, json={"success": False, "error": "Daily limit exceeded"}))._complete(
                "", [PageImage(1, _png(), 300)])


class Catalog(unittest.TestCase):
    def test_qari_models_first_with_own_prompt(self):
        self.assertEqual(cat.MODELS[0].id, "olmocr2-7b")   # أفضل ما جرّبه المستخدم
        self.assertLess(cat.rank("qari-v04-4b"), cat.rank("qari-v03-2b"))
        self.assertEqual(cat.get("qari-v04-4b").prompt, "Free OCR.")
        for m in cat.MODELS:
            self.assertEqual(len(m.model.sha256), 64)

    def test_provider_in_catalog(self):
        spec = next(s for s in load_catalog() if s.id == "qari")
        self.assertTrue(spec.is_ocr)
        self.assertEqual(spec.rpd, 100)


if __name__ == "__main__":
    unittest.main()


class GpuSupport(unittest.TestCase):
    def test_server_command_gpu_flag(self):
        from app.providers.local_llama import server_command
        base = server_command(Path("e"), Path("m"), Path("p"), 1234)
        self.assertNotIn("--n-gpu-layers", base)
        self.assertIn("--n-gpu-layers", server_command(Path("e"), Path("m"), Path("p"), 1234, gpu=True))

    def test_gpu_parse_and_advice(self):
        from app import gpu_info
        raw = ('[{"Name":"Intel(R) UHD Graphics 630","AdapterRAM":1073741824},'
               '{"Name":"Microsoft Basic Display Adapter","AdapterRAM":0},'
               '{"Name":"NVIDIA GeForce RTX 4060","AdapterRAM":4294967295}]')
        g = gpu_info.parse(raw)
        self.assertEqual([x.discrete for x in g], [False, True])
        self.assertIn("كرت مستقل", gpu_info.advice(g))
        self.assertIn("كرت مدمج", gpu_info.advice(g[:1]))
        self.assertEqual(gpu_info.parse("not json"), [])

    def test_engine_for_falls_back_to_cpu_when_gpu_build_missing(self):
        import tempfile
        from unittest import mock
        from app import model_manager as mm
        with tempfile.TemporaryDirectory() as d, mock.patch.object(mm, "llama_root", return_value=Path(d)):
            cpu = Path(d) / mm.cat.LLAMA_TAG
            cpu.mkdir()
            (cpu / "llama-server.exe").write_bytes(b"x")
            self.assertEqual(mm.engine_for(True), (cpu / "llama-server.exe", None))
            gpu = Path(d) / (mm.cat.LLAMA_TAG + "-vulkan")
            gpu.mkdir()
            (gpu / "llama-server.exe").write_bytes(b"x")
            self.assertEqual(mm.engine_for(True), (gpu / "llama-server.exe", cpu / "llama-server.exe"))
            self.assertEqual(mm.engine_for(False), (cpu / "llama-server.exe", None))

    def test_start_falls_back_to_cpu_if_gpu_server_fails(self):
        from unittest import mock
        from app.providers import local_llama as ll
        starts = []

        class FakeServer:
            def __init__(self, exe, model, mmproj, log_dir, threads=None, gpu=False, **kw):
                self.exe, self.gpu = exe, gpu

            def start(self, stop=None):
                starts.append(self.exe)
                if self.gpu:
                    raise ll.EngineUnavailable("vulkan fail")
                return "http://127.0.0.1:5555"

            def close(self):
                pass
        with mock.patch.object(ll, "LlamaServer", FakeServer):
            p = ll.start_local_provider(Path("gpu.exe"), Path("m"), Path("p"), Path("."), "x",
                                        gpu=True, fallback_exe=Path("cpu.exe"))
        self.assertEqual(starts, [Path("gpu.exe"), Path("cpu.exe")])
        self.assertEqual(p.base_url, "http://127.0.0.1:5555")
