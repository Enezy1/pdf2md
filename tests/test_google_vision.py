"""Google Cloud Vision: مزوّد OCR (بنقل وهمي بلا شبكة) وإدخاله في الكتالوج."""
import io
import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers.base import AuthError, PageImage, QuotaExhausted, RateLimited  # noqa: E402
from app.providers.catalog import load_catalog  # noqa: E402
from app.providers.google_vision import GoogleVisionProvider  # noqa: E402
from PIL import Image  # noqa: E402


def _png():
    b = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(b, "PNG")
    return b.getvalue()


def _run(handler):
    p = GoogleVisionProvider("google_vision", "AIza_test", transport=httpx.MockTransport(handler))
    return p._complete("", [PageImage(2, _png(), 300)])


def _err(status, st, msg):
    return lambda r: httpx.Response(status, json={"error": {"code": status, "status": st, "message": msg}})


class GoogleVision(unittest.TestCase):
    def test_ok_key_in_header_not_url(self):
        seen = {}

        def h(req):
            seen["key"] = req.headers.get("x-goog-api-key")
            seen["url"] = str(req.url)
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"responses": [{"fullTextAnnotation": {"text": "محكمة الاستئناف\r\n153"}}]})
        out = _run(h)
        self.assertEqual(seen["key"], "AIza_test")
        self.assertNotIn("AIza_test", seen["url"])
        req = seen["body"]["requests"][0]
        self.assertEqual(req["features"][0]["type"], "DOCUMENT_TEXT_DETECTION")
        self.assertIn("ar", req["imageContext"]["languageHints"])
        self.assertEqual(out, "<!-- page:2 -->\nمحكمة الاستئناف\n153")

    def test_blank(self):
        self.assertIn("<!-- blank -->", _run(lambda r: httpx.Response(200, json={"responses": [{}]})))

    def test_errors(self):
        with self.assertRaises(AuthError):
            _run(_err(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."))
        with self.assertRaises(AuthError):
            _run(_err(403, "PERMISSION_DENIED", "This API method requires billing to be enabled."))
        with self.assertRaises(QuotaExhausted):
            _run(_err(429, "RESOURCE_EXHAUSTED", "Quota exceeded for quota metric"))
        with self.assertRaises(RateLimited):
            _run(_err(429, "RESOURCE_EXHAUSTED", "Too many requests"))

    def test_error_inside_response(self):
        with self.assertRaises(Exception):
            _run(lambda r: httpx.Response(200, json={"responses": [{"error": {"code": 3, "message": "Bad image data."}}]}))

    def test_in_catalog_with_monthly_cap(self):
        spec = next(s for s in load_catalog() if s.id == "google_vision")
        self.assertTrue(spec.is_ocr)
        self.assertEqual(spec.rpmonth, 1000)


if __name__ == "__main__":
    unittest.main()
