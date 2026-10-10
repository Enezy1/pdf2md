"""مزوّد Google Gemini عبر مكتبة google-genai.

يدعم HTTPS_PROXY (يُمرَّر صراحة لعميل httpx داخل المكتبة). لا يُستدعى إلا عبر cloud_gate.
"""

from __future__ import annotations

import os
import re

from .base import AuthError, ExternalProvider, PageImage, ProviderError, QuotaExhausted, RateLimited, ServerBusy


def _proxy() -> str | None:
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


class GeminiProvider(ExternalProvider):
    def __init__(self, api_key: str, model: str, max_pages_per_call: int = 5, client=None):
        self.name = "gemini"
        self.model = model
        self.text_model = ""          # يُضبط من ApiRunner (settings.fast_corrector)
        self.max_pages_per_call = max_pages_per_call
        if client is None:
            from google import genai
            from google.genai import types
            client_args = {"proxy": _proxy()} if _proxy() else None
            opts = types.HttpOptions(timeout=180_000, client_args=client_args,
                                     retry_options=types.HttpRetryOptions(attempts=1))
            client = genai.Client(api_key=api_key, http_options=opts)
        self._client = client

    def _complete(self, prompt: str, images: list[PageImage]) -> str:
        from google.genai import errors, types
        parts: list = [prompt]
        for img in images:
            parts.append(f"[image for page {img.page_number}]")
            parts.append(types.Part.from_bytes(data=img.png, mime_type=img.mime))
        model, config = self.model, types.GenerateContentConfig(temperature=0)
        if not images and self.text_model:
            # الطلبات النصية (التصحيح): نسخة أخف بأقل "تفكير" — أسرع بكثير، والمهمة لا تحتاج تفكيراً
            model = self.text_model
            config = types.GenerateContentConfig(temperature=0,
                                                 thinking_config=types.ThinkingConfig(thinking_level="minimal"))
        try:
            try:
                resp = self._client.models.generate_content(model=model, contents=parts, config=config)
            except errors.APIError as e:
                if model == self.model or (getattr(e, "code", 0) or 0) not in (400, 404):
                    raise
                # النسخة الخفيفة أو "minimal" غير مدعومين: الطلب العادي
                self.text_model = ""
                resp = self._client.models.generate_content(
                    model=self.model, contents=parts, config=types.GenerateContentConfig(temperature=0))
        except errors.APIError as e:
            raise map_gemini_error(getattr(e, "code", 0) or 0, str(getattr(e, "message", "") or e)) from None
        except Exception as e:
            raise ProviderError(f"خطأ اتصال: {type(e).__name__}") from None
        return resp.text or ""


def map_gemini_error(code: int, message: str) -> ProviderError:
    if code in (401, 403) or "API key not valid" in message:
        return AuthError(f"HTTP {code}: المفتاح مرفوض")
    if code == 429:
        if re.search(r"PerDay|per day|daily", message, re.I):
            return QuotaExhausted("انتهت الحصة اليومية")
        m = re.search(r"retry(?:Delay| in)[\"': ]+([\d.]+)s", message)
        return RateLimited("HTTP 429", float(m.group(1)) if m else None)
    if code in (500, 502, 503, 504) or re.search(r"UNAVAILABLE|overloaded|high demand", message, re.I):
        return ServerBusy(f"HTTP {code}: الخادم مشغول مؤقتاً")
    return ProviderError(f"HTTP {code}: {message[:300]}")
