import unittest

from app.model_picker import ModelPicker
from app.providers.base import PageImage
from app.providers.batching import plan_whole, run_with_split


def img(n):
    return PageImage(page_number=n, png=b"", dpi=100, mime="image/jpeg", ink=0.0)


class PlanWholeTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(len(plan_whole(list(range(1, 31)), 40)), 1)
        self.assertEqual(len(plan_whole(list(range(1, 41)), 40)), 1)
        parts = plan_whole(list(range(1, 58)), 40)
        self.assertEqual([len(p) for p in parts], [29, 28])
        self.assertEqual(len(plan_whole(list(range(1, 81)), 40)), 2)
        self.assertEqual(len(plan_whole(list(range(1, 82)), 40)), 3)


class RetryBadOnlyTest(unittest.TestCase):
    def test_resends_missing_pages_only(self):
        calls = []

        def send(prompt, req):
            nums = [i.page_number for i in req.images]
            calls.append(nums)
            keep = nums if len(calls) > 1 else [n for n in nums if n not in (7, 8)]
            return "\n".join(f"<!-- page:{n} -->\nنص الصفحة {n}" for n in keep)

        out = run_with_split(send, [img(n) for n in range(1, 21)])
        self.assertEqual(calls[1], [7, 8])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(o.markdown for o in out))


class StickyPickerTest(unittest.TestCase):
    def test_waits_unless_quota(self):
        p = ModelPicker("g:a", ["a", "x", "y"], sticky=True)
        self.assertEqual(p.switch("limited"), (False, "a", "a"))
        self.assertEqual(p.switch("busy"), (False, "a", "a"))
        moved, _, new = p.switch("quota")
        self.assertTrue(moved)
        self.assertEqual(new, "x")


class SingleUnmarkedTest(unittest.TestCase):
    def test_single_page_without_marker_is_accepted(self):
        out = run_with_split(lambda prompt, req: "تعاقدت الشركة مع المقاول على تنفيذ الأعمال الموضحة بالمواصفات وفق الجدول الزمني المرفق بالعقد والأسعار المتفق عليها بين الطرفين", [img(7)])
        self.assertEqual(out[0].page, 7)
        self.assertTrue(out[0].markdown)


class PartialKeptTest(unittest.TestCase):
    def test_failed_page_keeps_best_text(self):
        img7 = PageImage(page_number=7, png=b"", dpi=100, mime="image/jpeg", ink=0.2)
        out = run_with_split(lambda prompt, req: "<!-- page:7 -->" + chr(10) + "نص قصير", [img7])
        self.assertIsNone(out[0].markdown)
        self.assertIn("نص قصير", out[0].partial)


if __name__ == "__main__":
    unittest.main()
