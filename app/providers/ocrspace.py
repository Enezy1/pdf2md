"""محرّك OCR.space (نوع "ocr" في الكتالوج): يقرأ صورة صفحة واحدة ويعيد نصاً خاماً.

حسب التوثيق الرسمي https://ocr.space/OCRAPI (تحقق 2026-10-09):
- POST https://api.ocr.space/parse/image، المفتاح في ترويسة "apikey".
- language=ara للعربية. OCREngine=3 الأساسي (الأشمل)، و2 احتياط: إذا أعاد 3 صفحة فارغة
  تُعاد بالمحرّك 2، وبعد بلوغ 3 حده الشهري يُستخدم 2. المحرّك 1 لا يقبل العربية فلا يُستخدم.
- الخطة المجانية: 1MB للملف، 500 طلب يومياً لكل IP، 25000 شهرياً، و1000 تحويل بالمحرّك 3.
- الاستجابة: ParsedResults[].ParsedText، وOCRExitCode، وIsErroredOnProcessing، وErrorMessage.
  لا توجد درجة ثقة؛ مواقع الكلمات فقط مع isOverlayRequired (لا نطلبها).
- رموز HTTP للأخطاء غير موثّقة: نصنّف من الرمز ومن نص ErrorMessage معاً.

كل صفحة صورة JPEG منفصلة تحت 1MB (fit_jpeg). لا يُرسل PDF أبداً.
"""

from __future__ import annotations

import base64
import io
import re
import threading

import httpx
from PIL import Image

from .base import AuthError, ExternalProvider, PageImage, ProviderError, QuotaExhausted, RateLimited, ServerBusy
from .openai_compat import classify_http_error

ENDPOINT = "https://api.ocr.space/parse/image"
MAX_BYTES = 1_000_000 - 2_048           # هامش أمان تحت 1MB
TIMEOUT = httpx.Timeout(120.0, connect=20.0)
ENGINE3_MONTHLY = 1000

_QUOTA_RE = re.compile(r"maximum\s+\d+\s+number of times|exceeded|quota|limit reached|upto maximum", re.I)
_AUTH_RE = re.compile(r"api ?key.*(invalid|not valid|missing)|invalid api|unauthori", re.I)
_BUSY_RE = re.compile(r"timed? ?out|E101|busy|try again later|server", re.I)


def fit_jpeg(data: bytes, max_bytes: int = MAX_BYTES) -> bytes:
    """يعيد ترميز الصورة حتى تصبح تحت max_bytes: الجودة أولاً (حتى 40) ثم الأبعاد تدريجياً."""
    if len(data) <= max_bytes and data[:3] == b"\xff\xd8\xff":
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    quality = 85
    while True:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) <= max_bytes:
            return out
        if quality > 40:
            quality -= 10
        elif max(img.size) > 300:
            img = img.resize((max(1, int(img.width * 0.85)), max(1, int(img.height * 0.85))), Image.LANCZOS)
        else:
            raise ProviderError("تعذّر ضغط الصورة تحت 1MB")


def classify(status: int, payload: dict | None, body: str) -> ProviderError | None:
    msg = ""
    if isinstance(payload, dict):
        em = payload.get("ErrorMessage")
        msg = " ".join(em) if isinstance(em, list) else str(em or "")
    text = f"{msg} {body[:300] if status != 200 else ''}"
    if status == 200 and isinstance(payload, dict) and not payload.get("IsErroredOnProcessing") \
            and payload.get("OCRExitCode") in (1, 2):
        return None
    if _QUOTA_RE.search(text):
        return QuotaExhausted("OCR.space: انتهت الحصة")
    if status in (401, 403) or _AUTH_RE.search(text):
        return AuthError("OCR.space: المفتاح مرفوض")
    if status != 200:
        return classify_http_error(status, None, body)
    if _BUSY_RE.search(text):
        return ServerBusy("OCR.space: انتهت مهلة المعالجة")
    return ProviderError(f"OCR.space: {msg[:200] or 'رد غير مفهوم'}")


class OcrSpaceProvider(ExternalProvider):
    """ExternalProvider عادي: الإرسال الوحيد عبر cloud_gate. التعليمات (prompt) تُتجاهل."""

    max_pages_per_call = 1
    is_ocr = True

    def __init__(self, provider_id: str, api_key: str, engine: int = 3, endpoint: str = ENDPOINT,
                 transport: httpx.BaseTransport | None = None):
        if not endpoint.startswith(("https://", "http://127.0.0.1")) and transport is None:
            raise ValueError("رابط OCR.space يجب أن يكون https")
        self.name = provider_id
        self._default = engine if engine in (2, 3) else 3
        self._local = threading.local()     # المحرّك لكل خيط: الصفحات تُقرأ بالتوازي
        self.endpoint = endpoint
        self._key = api_key
        self._client = httpx.Client(timeout=TIMEOUT, transport=transport, trust_env=True)

    @property
    def engine(self) -> int:
        return getattr(self._local, "engine", self._default)

    @property
    def model(self) -> str:
        return f"engine{self.engine}"

    @model.setter
    def model(self, _value) -> None:   # يتجاهل: المحرّك يُختار بـ set_engine
        pass

    def set_engine(self, engine: int) -> None:
        self._local.engine = engine if engine in (2, 3) else 3

    def _complete(self, prompt: str, images: list[PageImage]) -> str:
        if len(images) != 1:
            raise ProviderError("OCR.space: صفحة واحدة لكل طلب")
        img = images[0]
        jpeg = fit_jpeg(img.png)
        form = {
            "base64Image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
            "language": "ara",
            "OCREngine": str(self.engine),
            "filetype": "JPG",
            "detectOrientation": "true",
            "scale": "true",
            "isOverlayRequired": "false",
        }
        try:
            r = self._client.post(self.endpoint, data=form, headers={"apikey": self._key})
        except httpx.TimeoutException:
            raise ServerBusy("OCR.space: انتهت مهلة الاتصال") from None
        except httpx.HTTPError as e:
            raise ProviderError(f"خطأ اتصال: {type(e).__name__}") from None
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code == 429 and not (payload and _QUOTA_RE.search(str(payload.get("ErrorMessage")))):
            raise RateLimited("HTTP 429")
        err = classify(r.status_code, payload, r.text)
        if err is not None:
            raise err
        parts = [p.get("ParsedText") or "" for p in payload.get("ParsedResults") or [] if isinstance(p, dict)]
        text = "\n".join(parts).replace("\r\n", "\n").strip()
        # detectOrientation: "TextOrientation" = 0 أو 90 أو 180 أو 270 (الصفحة المدوّرة تُرسل لنموذج الرؤية)
        orient = next((str(p.get("TextOrientation") or "0") for p in payload.get("ParsedResults") or []
                       if isinstance(p, dict)), "0")
        mark = f"\n<!-- orientation:{orient} -->" if orient.strip() not in ("", "0") else ""
        return f"<!-- page:{img.page_number} -->{mark}\n{text if text else '<!-- blank -->'}"

    def close(self) -> None:
        self._client.close()
