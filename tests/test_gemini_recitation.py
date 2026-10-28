import unittest
from types import SimpleNamespace

from app.providers.base import PageImage
from app.providers.gemini import GeminiProvider


def resp(text, finish):
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish)])


class FakeModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        return resp("", "FinishReason.RECITATION") if "lite" in model else resp("نص كامل", "FinishReason.STOP")


class RecitationTest(unittest.TestCase):
    def test_falls_back_to_non_lite_model(self):
        models = FakeModels()
        p = GeminiProvider("k", "gemini-3.5-flash-lite", client=SimpleNamespace(models=models))
        p.recitation_models = ["gemini-3.8-flash"]
        img = PageImage(page_number=1, png=b"x", dpi=100, mime="image/jpeg")
        self.assertEqual(p._complete("p", [img]), "نص كامل")
        self.assertEqual(models.calls, ["gemini-3.5-flash-lite", "gemini-3.8-flash"])


class AllRecitedModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(contents[0][:20])
        if str(contents[0]).startswith("Summarize"):
            return resp("نقطة أولى", "FinishReason.STOP")
        return resp("", "FinishReason.RECITATION")


class SummaryLastResortTest(unittest.TestCase):
    def test_summary_when_every_model_recites(self):
        models = AllRecitedModels()
        p = GeminiProvider("k", "gemini-3.5-flash-lite", client=SimpleNamespace(models=models))
        p.recitation_models = ["gemini-2.5-flash"]
        img = PageImage(page_number=5, png=b"x", dpi=100, mime="image/jpeg")
        out = p._complete("Transcribe <!-- page:5 -->", [img])
        self.assertTrue(out.startswith("<!-- page:5 -->"))
        self.assertIn("نقطة أولى", out)


if __name__ == "__main__":
    unittest.main()
