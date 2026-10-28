"""Qari-OCR عبر واجهة qari.g4t.io (نوع "ocr" في الكتالوج): يقرأ صورة صفحة واحدة ويعيد نصاً خاماً.

حسب صفحة الموقع https://qari.g4t.io (تحقق 2026-10-14):
- POST https://qari.g4t.io/api/v1/ocr، المفتاح في ترويسة Authorization: Bearer <key>.
- multipart: الحقل image (PNG/JPG/WEBP/TIFF حتى 10MB) والحقل language ("auto" يكتشف اللغة).
- الاستجابة: {"success", "text", "confidence" (نسبة مئوية), "processing_time" (ثوانٍ)}. لا PDF بعد.
- الخطة المجانية: 100 طلب يومياً.
- رموز الأخطاء وصيغة رسائلها غير موثّقة: نصنّف من الرمز ومن نص الرد معاً.

نفس عقد OcrSpaceProvider: الإرسال الوحيد عبر cloud_gate، والتعليمات (prompt) تُتجاهل.
"""

from __future__ import annotations

import re

import httpx

from .base import AuthError, ExternalProvider, PageImage, ProviderError, QuotaExhausted, RateLimited, ServerBusy
from .ocrspace import fit_jpeg
from .openai_compat import classify_http_error

ENDPOINT = "https://qari.g4t.io/api/v1/ocr"
MAX_BYTES = 10_000_000 - 100_000        # الحد 10MB، مع هامش
TIMEOUT = httpx.Timeout(180.0, connect=20.0)

_QUOTA_RE = re.compile(r"daily|quota|limit (reached|exceeded)|exceeded", re.I)


def classify(status: int, payload: dict | None, body: str) -> ProviderError | None:
    msg = ""
    if isinstance(payload, dict):
        msg = str(payload.get("error") or payload.get("message") or payload.get("detail") or "")
    text = f"{msg} {body[:300] if status != 200 else ''}"
    if status == 200 and isinstance(payload, dict) and payload.get("success") is True:
        return None
    if status in (401, 403):
        return AuthError("Qari: المفتاح مرفوض")
    if status == 429 or _QUOTA_RE.search(text):
        return QuotaExhausted("Qari: انتهت حصة اليوم (100 طلب في الخطة المجانية)")
    if status != 200:
        return classify_http_error(status, None, body)
    return ProviderError(f"Qari: {msg[:200] or 'رد غير مفهوم'}")


class QariApiProvider(ExternalProvider):
    max_pages_per_call = 1
    is_ocr = True
    model = "qari-api"

    def __init__(self, provider_id: str, api_key: str, endpoint: str = ENDPOINT,
                 transport: httpx.BaseTransport | None = None):
        if not endpoint.startswith(("https://", "http://127.0.0.1")) and transport is None:
            raise ValueError("رابط Qari يجب أن يكون https")
        self.name = provider_id
        self.endpoint = endpoint
        self._key = api_key
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, trust_env=True)

    def _complete(self, prompt: str, images: list[PageImage]) -> str:
        if len(images) != 1:
            raise ProviderError("Qari: صفحة واحدة لكل طلب")
        img = images[0]
        jpeg = fit_jpeg(img.png, MAX_BYTES)
        try:
            r = self._client.post(self.endpoint, data={"language": "auto"},
                                  files={"image": ("page.jpg", jpeg, "image/jpeg")},
                                  headers={"Authorization": f"Bearer {self._key}"})
        except httpx.TimeoutException:
            raise ServerBusy("Qari: انتهت مهلة الاتصال") from None
        except httpx.HTTPError as e:
            raise ProviderError(f"خطأ اتصال: {type(e).__name__}") from None
        try:
            payload = r.json()
        except ValueError:
            payload = None
        err = classify(r.status_code, payload, r.text)
        if err is not None:
            if isinstance(err, QuotaExhausted) and r.status_code == 429 and not _QUOTA_RE.search(r.text):
                raise RateLimited("HTTP 429")
            raise err
        text = str(payload.get("text") or "").replace("\r\n", "\n").strip()
        return f"<!-- page:{img.page_number} -->\n{text if text else '<!-- blank -->'}"

    def close(self) -> None:
        self._client.close()
