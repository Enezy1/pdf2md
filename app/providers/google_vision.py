"""Google Cloud Vision (نوع "ocr" في الكتالوج): يقرأ صورة صفحة واحدة ويعيد نصاً خاماً.

حسب التوثيق الرسمي https://cloud.google.com/vision/docs/ocr (من معرفتي، لم يُجرَّب بمفتاح حقيقي):
- POST https://vision.googleapis.com/v1/images:annotate، المفتاح في ترويسة x-goog-api-key (لا في الرابط).
- الميزة DOCUMENT_TEXT_DETECTION (للمستندات الكثيفة) مع languageHints=["ar"].
- الاستجابة: responses[0].fullTextAnnotation.text، أو responses[0].error {code, message}.
- أول 1000 وحدة شهرياً مجانية ثم مدفوعة؛ يتطلب تفعيل الفوترة (بطاقة) في مشروع Google Cloud.
- الطلب حتى 10MB (base64 يزيد الحجم الثلث) ⇒ صورة JPEG تحت ~7MB.
- RESOURCE_EXHAUSTED (429) قد يعني حدّ معدّل أو انتهاء الحصة: نميّز من نص الرسالة.

نفس عقد OcrSpaceProvider: الإرسال الوحيد عبر cloud_gate، والتعليمات (prompt) تُتجاهل.
"""

from __future__ import annotations

import base64
import re

import httpx

from .base import AuthError, ExternalProvider, PageImage, ProviderError, QuotaExhausted, RateLimited, ServerBusy
from .ocrspace import fit_jpeg
from .openai_compat import classify_http_error

ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
MAX_BYTES = 7_000_000
TIMEOUT = httpx.Timeout(120.0, connect=20.0)

_QUOTA_RE = re.compile(r"quota|billing|exceeded.*(day|month)", re.I)
_AUTH_RE = re.compile(r"api key|API_KEY|permission|not been used|disabled|unauthori", re.I)


def _error(payload: dict | None) -> tuple[str, str]:
    """(الحالة، الرسالة) من خطأ أعلى الرد أو من خطأ الصورة الأولى."""
    if not isinstance(payload, dict):
        return "", ""
    err = payload.get("error")
    if not err:
        resp = (payload.get("responses") or [{}])[0]
        err = resp.get("error") if isinstance(resp, dict) else None
    if not isinstance(err, dict):
        return "", ""
    return str(err.get("status") or ""), str(err.get("message") or "")


def classify(status: int, payload: dict | None, body: str) -> ProviderError | None:
    st, msg = _error(payload)
    if status == 200 and not msg:
        return None
    text = f"{st} {msg} {body[:300] if not msg else ''}"
    if status == 429 or st == "RESOURCE_EXHAUSTED":
        if _QUOTA_RE.search(msg):
            return QuotaExhausted("Cloud Vision: انتهت الحصة")
        return RateLimited("Cloud Vision: HTTP 429")
    if status in (401, 403) or st in ("PERMISSION_DENIED", "UNAUTHENTICATED") or (
            status == 400 and _AUTH_RE.search(text)):
        if "billing" in text.lower():
            return AuthError("Cloud Vision: الفوترة غير مفعّلة في مشروع Google Cloud")
        return AuthError("Cloud Vision: المفتاح مرفوض أو الخدمة غير مفعّلة في المشروع")
    if status in (500, 502, 503, 504) or st in ("UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"):
        return ServerBusy("Cloud Vision: الخادم مشغول")
    if status != 200:
        return classify_http_error(status, None, body)
    return ProviderError(f"Cloud Vision: {msg[:200] or 'رد غير مفهوم'}")


class GoogleVisionProvider(ExternalProvider):
    max_pages_per_call = 1
    is_ocr = True
    model = "document-text-detection"

    def __init__(self, provider_id: str, api_key: str, endpoint: str = ENDPOINT,
                 transport: httpx.BaseTransport | None = None):
        if not endpoint.startswith(("https://", "http://127.0.0.1")) and transport is None:
            raise ValueError("رابط Cloud Vision يجب أن يكون https")
        self.name = provider_id
        self.endpoint = endpoint
        self._key = api_key
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, trust_env=True)

    def _complete(self, prompt: str, images: list[PageImage]) -> str:
        if len(images) != 1:
            raise ProviderError("Cloud Vision: صفحة واحدة لكل طلب")
        img = images[0]
        jpeg = fit_jpeg(img.png, MAX_BYTES)
        body = {"requests": [{
            "image": {"content": base64.b64encode(jpeg).decode("ascii")},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            "imageContext": {"languageHints": ["ar", "en"]},
        }]}
        try:
            r = self._client.post(self.endpoint, json=body, headers={"x-goog-api-key": self._key})
        except httpx.TimeoutException:
            raise ServerBusy("Cloud Vision: انتهت مهلة الاتصال") from None
        except httpx.HTTPError as e:
            raise ProviderError(f"خطأ اتصال: {type(e).__name__}") from None
        try:
            payload = r.json()
        except ValueError:
            payload = None
        err = classify(r.status_code, payload, r.text)
        if err is not None:
            raise err
        resp = (payload.get("responses") or [{}])[0] if isinstance(payload, dict) else {}
        text = str((resp.get("fullTextAnnotation") or {}).get("text") or "").replace("\r\n", "\n").strip()
        return f"<!-- page:{img.page_number} -->\n{text if text else '<!-- blank -->'}"

    def close(self) -> None:
        self._client.close()
