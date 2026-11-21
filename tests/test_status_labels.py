"""اختبارات نصوص حالات الملفات: app/ui/status_labels.py

الهدف: منع رجوع الجدول إلى الصيغ الملتبسة («جاهز» بدل «بانتظار البدء»، أو نسبة مائلة
«4/171» تنقلب بصرياً، أو رقم بلا وحدة).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ui import status_labels as sl  # noqa: E402


class TestStatusText(unittest.TestCase):
    def test_queued_is_not_called_ready(self):
        """«جاهز» كانت تُقرأ كأنها «تم» — الآن «بانتظار البدء» ورمادية."""
        st = sl.FileStatus(sl.QUEUED)
        self.assertEqual(sl.text(st), "بانتظار البدء")
        self.assertEqual(sl.tone(st), "")

    def test_scanning_and_working(self):
        self.assertEqual(sl.text(sl.FileStatus(sl.SCANNING)), "جارٍ الفحص")
        self.assertEqual(sl.tone(sl.FileStatus(sl.SCANNING)), "busy")
        self.assertEqual(sl.text(sl.FileStatus(sl.WORKING, done=50, total=100)), "جارٍ · أنجز 50 من 100")

    def test_no_slash_ratio(self):
        """النسبة المائلة تنقلب بصرياً في نص RTL («4/171» تصير «171/4»)."""
        for st in (sl.FileStatus(sl.WORKING, done=4, total=171),
                   sl.FileStatus(sl.PARTIAL, done=4, total=171)):
            self.assertNotIn("/", sl.text(st))
            self.assertIn("أنجز 4 من 171", sl.text(st))

    def test_partial_reads_as_partial(self):
        st = sl.FileStatus(sl.PARTIAL, done=4, total=171)
        self.assertEqual(sl.text(st), "مكتمل جزئياً · أنجز 4 من 171")
        self.assertEqual(sl.tone(st), "warn")

    def test_done_and_pending_unit(self):
        self.assertEqual(sl.text(sl.FileStatus(sl.DONE)), "تم")
        self.assertEqual(sl.tone(sl.FileStatus(sl.DONE)), "ok")
        self.assertEqual(sl.text(sl.FileStatus(sl.DONE, pending=28)), "تم · 28 صفحة معلّقة")
        self.assertEqual(sl.tone(sl.FileStatus(sl.DONE, pending=28)), "warn")
        self.assertEqual(sl.text(sl.FileStatus(sl.DONE, pending=1)), "تم · صفحة واحدة معلّقة")
        self.assertEqual(sl.text(sl.FileStatus(sl.DONE, pending=2)), "تم · صفحتان معلّقتان")
        self.assertEqual(sl.text(sl.FileStatus(sl.DONE, pending=5)), "تم · 5 صفحات معلّقات")

    def test_error_shows_reason_below(self):
        st = sl.FileStatus(sl.ERROR, error="الملف تالف")
        self.assertEqual(sl.text(st), "خطأ")
        self.assertEqual(sl.tone(st), "err")
        self.assertIn("الملف تالف", sl.note(st))

    def test_note_is_short(self):
        """السطر تحت الاسم يوسّع عمود الاسم، والجدول لا يُمرَّر أفقياً."""
        st = sl.FileStatus(sl.ERROR, error="خطأ " * 60)
        self.assertLessEqual(len(sl.note(st)), 47)


if __name__ == "__main__":
    unittest.main()
