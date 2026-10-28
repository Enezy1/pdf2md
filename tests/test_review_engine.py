import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz

from app.config import Settings
from app.pipeline import read_page_now


class FakeGate:
    def open_file(self, path):
        return "ticket"

    def send(self, ticket, provider, prompt, request, model=""):
        return provider.reply


class FakeLimiter:
    def call(self, fn, log=None):
        return fn()


def slot(sid, ocr, reply):
    return SimpleNamespace(id=sid, is_ocr=ocr, spec=SimpleNamespace(name=sid), limiter=FakeLimiter(),
                           provider=SimpleNamespace(reply=reply, model="m"))


class ReviewEngineTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.pdf = str(Path(d) / "a.pdf")
        doc = fitz.open()
        doc.new_page().insert_text((72, 72), "hello")
        doc.save(self.pdf)
        doc.close()

    def api(self):
        slots = [slot("gemini", False, ""), slot("google_vision", True, "  نص من جوجل فيجن  ")]
        return SimpleNamespace(slots=slots, gate=FakeGate(), llm_slots=lambda: [s for s in slots if not s.is_ocr])

    def test_chosen_ocr_slot_is_used(self):
        self.assertEqual(read_page_now(self.api(), self.pdf, 1, Settings(), "google_vision"), "نص من جوجل فيجن")

    def test_unknown_slot_raises(self):
        with self.assertRaises(RuntimeError):
            read_page_now(self.api(), self.pdf, 1, Settings(), "nope")


if __name__ == "__main__":
    unittest.main()
