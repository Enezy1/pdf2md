"""تسليم Gemini القراءة مبكراً عند 429 من OCR.space، وسطر قرار كل صفحة."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import dispatcher as d  # noqa: E402
from app.providers.base import RateLimited  # noqa: E402
from app.rate_limit import GiveUp, RateLimiter, UsageCounter  # noqa: E402


def _limiter(tmp: str) -> RateLimiter:
    return RateLimiter("ocrspace", 600, UsageCounter(Path(tmp) / "u.json"), sleep=lambda s: None)


class MaxRateLimitTests(unittest.TestCase):
    def test_custom_cap_gives_up_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def fn():
                calls.append(1)
                raise RateLimited("429")
            with self.assertRaises(GiveUp):
                _limiter(tmp).call(fn, max_429=d.OCR_MAX_429)
            self.assertEqual(len(calls), d.OCR_MAX_429)

    def test_ocr_down_flags_slot_at_once(self):
        slot = SimpleNamespace(down="", down_kind="")
        err = d.ApiRunner._ocr_down(slot, "429 متكرر", "errors")
        self.assertIsInstance(err, d._SlotDown)
        self.assertEqual((slot.down, slot.down_kind), ("429 متكرر", "errors"))


class RouteNoteTests(unittest.TestCase):
    llm = SimpleNamespace(spec=SimpleNamespace(name="Gemini"))

    def note(self, n, i, full=(), hard=(), easy=(), skip=(), send_hard=True):
        return d._route_note(n, i, self.llm, dict.fromkeys(full), list(hard), list(easy), set(skip), send_hard)

    def test_each_route(self):
        i = {"full_why": "table", "weak": False}
        self.assertIn("جدول", self.note(1, i, full=[1]))
        self.assertIn("صورتها مع النص", self.note(2, {"full_why": "", "weak": True}, hard=[2]))
        self.assertIn("نصها فقط", self.note(3, i, easy=[3]))
        self.assertIn("بلا تصحيح", self.note(4, i))
        self.assertEqual(self.note(5, i, skip=[5]), "")

    def test_weak_but_send_hard_off(self):
        self.assertIn("معطّل", self.note(1, {"full_why": "", "weak": True}, easy=[1], send_hard=False))


if __name__ == "__main__":
    unittest.main()
