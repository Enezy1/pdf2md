"""مزوّد عام لكل خدمة متوافقة مع صيغة OpenAI (chat/completions) ويرسل الصور.

يُغذّى من الكتالوج (الرابط الأساسي، النموذج، حد الصفحات لكل طلب). يحترم HTTPS_PROXY من
البيئة (httpx trust_env). لا يُستدعى إلا عبر cloud_gate.
"""

from __future__ import annotations

import base64
import email.utils
import re
import time

import httpx

from .base import AuthError, ExternalProvider, PageImage, ProviderError, QuotaExhausted, RateLimited, ServerBusy

TIMEOUT = httpx.Timeout(180.0, connect=20.0)
_DAILY_RE = re.compile(r"per day|daily|RPD|quota|insufficient|credits", re.I)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", value)
    if m:
        n = float(m.group(1))
        return n / 1000 if m.group(2) == "ms" else n * 60 if m.group(2) == "m" else n
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def classify_http_error(status: int, retry_after: str | None, body: str) -> ProviderError:
    snippet = (body or "")[:300]
    if status in (401, 403):
        return AuthError(f"HTTP {status}: المفتاح مرفوض أو بلا صلاحية")
    if status == 402:
        return QuotaExhausted(f"HTTP 402: الرصيد أو الحصة منتهية")
    if status == 429:
        if _DAILY_RE.search(snippet) and not re.search(r"per minute|RPM|TPM", snippet, re.I):
            return QuotaExhausted(f"HTTP 429: انتهت الحصة اليومية")
        return RateLimited(f"HTTP 429", parse_retry_after(retry_after))
    if status in (500, 502, 503, 504, 529):
        return ServerBusy(f"HTTP {status}: الخادم مشغول مؤقتاً", parse_retry_after(retry_after))
    return ProviderError(f"HTTP {status}: {snippet}")


def image_part(img: PageImage) -> dict:
    b64 = base64.b64encode(img.png).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{img.mime};base64,{b64}"}}


class OpenAICompatProvider(ExternalProvider):
    def __init__(self, provider_id: str, base_url: str, api_key: str, model: str, max_pages_per_call: int = 1,
                 transport: httpx.BaseTransport | None = None):
        if not base_url.startswith("https://") and transport is None and not base_url.startswith("http://127.0.0.1"):
            raise ValueError("الرابط الأساسي يجب أن يكون https")
        self.name = provider_id
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_pages_per_call = max_pages_per_call
        self._key = api_key
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, trust_env=True)
        self._no_temperature = False

    def _post(self, body: dict) -> httpx.Response:
        try:
            return self._client.post(f"{self.base_url}/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as e:
            raise ProviderError(f"خطأ اتصال: {type(e).__name__}") from None

    def _complete(self, prompt: str, images: list[PageImage]) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "text", "text": f"[image for page {img.page_number}]"})
            content.append(image_part(img))
        body = {"model": self.model, "messages": [{"role": "user", "content": content}]}
        if not self._no_temperature:
            body["temperature"] = 0
        r = self._post(body)
        if r.status_code == 400 and "temperature" in body:   # GPT-5 وMiniMax وغيرها ترفض 0؛ نعيد بالقيمة الافتراضية
            # نماذج OpenAI الاستدلالية (GPT-5 وما بعده) ترفض temperature=0: يُعاد الطلب بالقيمة الافتراضية
            self._no_temperature = True
            del body["temperature"]
            r = self._post(body)
        if r.status_code != 200:
            raise classify_http_error(r.status_code, r.headers.get("retry-after"), r.text)
        try:
            data = r.json()
            msg = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("رد غير مفهوم من المزوّد") from None
        if isinstance(msg, list):  # بعض الخدمات تعيد أجزاء
            msg = "".join(p.get("text", "") for p in msg if isinstance(p, dict))
        return msg or ""

    def close(self) -> None:
        self._client.close()
