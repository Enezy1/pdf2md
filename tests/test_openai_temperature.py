import json
import unittest

import httpx

from app.providers.openai_compat import OpenAICompatProvider


class TemperatureFallbackTest(unittest.TestCase):
    def test_retries_without_temperature(self):
        bodies = []

        def handler(req):
            b = json.loads(req.content)
            bodies.append(b)
            if "temperature" in b:
                return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "نص"}}]})

        p = OpenAICompatProvider("openai", "https://api.openai.com/v1", "k", "m", transport=httpx.MockTransport(handler))
        self.assertEqual(p._complete("x", []), "نص")
        self.assertEqual(p._complete("x", []), "نص")
        self.assertEqual(len(bodies), 3)   # المرة الثانية بلا temperature مباشرة


if __name__ == "__main__":
    unittest.main()
