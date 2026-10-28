"""مزوّد Google Gemini عبر مكتبة google-genai.

يدعم HTTPS_PROXY (يُمرَّر صراحة لعميل httpx داخل المكتبة). لا يُستدعى إلا عبر cloud_gate.
"""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

from .base import AuthError, ExternalProvider, PageImage, ProviderError, QuotaExhausted, RateLimited, ServerBusy


def _proxy() -> str | None:
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


class GeminiProvider(ExternalProvider):
    def __init__(self, api_key: str, model: str, max_pages_per_call: int = 5, client=None):
        self.name = "gemini"
        self.model = model
        self.text_model = ""          # يُضبط من ApiRunner (settings.fast_corrector)
        self.on_extra_call = None     # callable(model): يُحصي الطلبات الإضافية (بدائل الحجب والملخّص) في عدّاد اليوم
        self.recitation_models: list[str] = []   # يُضبط من make_provider: بدائل عند حجب RECITATION
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
                if images and _recited(resp):
                    resp = self._retry_recited(resp, parts, model)
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

    def _retry_recited(self, resp, parts, used: str):
        """النماذج الخفيفة تحجب صفحات المواصفات الشائعة بـ RECITATION؛ نجرّب بدائلها (كلها بنفس الصفحة) حتى يعود نص كامل.
        الرد الجزئي الأطول هو الاحتياط إن حُجبت كلها."""
        from google.genai import errors, types
        best = resp
        for alt in self.recitation_models:
            if alt == used:
                continue
            if self.on_extra_call:
                self.on_extra_call(alt)
            try:
                r = self._client.models.generate_content(model=alt, contents=parts,
                                                         config=types.GenerateContentConfig(temperature=0))
            except errors.APIError:
                continue
            if not _recited(r) and (r.text or "").strip():
                return r
            if len(r.text or "") > len(best.text or ""):
                best = r
        # كل البدائل محجوبة أو حصتها منتهية: نسخة مُلخَّصة من الصفحة (سطر لكل نقطة بأسماء وأرقام دقيقة) بدل لا شيء
        if len(parts) == 3 and len((best.text or "").strip()) < 300:   # صفحة واحدة فقط (نص + علامة + صورة)
            m = re.search(r"<!-- page:(\d+) -->", str(parts[0]))
            marker = m.group(0) if m else ""
            ask = ("Summarize every bullet point and paragraph on this page into concise Markdown, one line each, keeping all "
                   "product names, model names, technical terms and numbers exact. Do not add a title or commentary.")
            if self.on_extra_call:
                self.on_extra_call(used)
            try:
                r = self._client.models.generate_content(model=used, contents=[ask, *parts[1:]],
                                                         config=types.GenerateContentConfig(temperature=0))
            except errors.APIError:
                return best
            if (r.text or "").strip() and not _recited(r):
                note = "> [ملخّص آلي: حجب Gemini النسخ الحرفي لهذه الصفحة لأنها تطابق نصاً منشوراً]"
                return SimpleNamespace(text=marker + chr(10) + note + chr(10) + chr(10) + r.text.strip(), candidates=[])
        return best


def _recited(resp) -> bool:
    """Gemini يقطع الرد (أو يحجبه كله) بـ RECITATION حين يطابق نصاً منشوراً (مواصفات، معايير، عقود نموذجية)."""
    c = resp.candidates[0] if getattr(resp, "candidates", None) else None
    return c is not None and "RECITATION" in str(getattr(c, "finish_reason", ""))


def map_gemini_error(code: int, message: str) -> ProviderError:
    if code in (401, 403) or "API key not valid" in message:
        return AuthError(f"HTTP {code}: المفتاح مرفوض")
    if code == 429:
        if re.search(r"PerDay|per day|daily", message, re.I):
            return QuotaExhausted("انتهت الحصة اليومية")
        m = re.search(r"retry(?:Delay| in)[\"': ]+([\d.]+)s", message)
        return RateLimited("HTTP 429", float(m.group(1)) if m else None)
    if code == 404:   # اسم نموذج غير موجود/غير متاح للحساب: يُعامل كنموذج منتهٍ فينتقل الاختيار لغيره
        return QuotaExhausted(f"HTTP 404: النموذج غير متاح ({message[:120]})")
    if code in (500, 502, 503, 504) or re.search(r"UNAVAILABLE|overloaded|high demand", message, re.I):
        return ServerBusy(f"HTTP {code}: الخادم مشغول مؤقتاً")
    return ProviderError(f"HTTP {code}: {message[:300]}")
