"""الواجهة الموحدة لمحرّكات قراءة الصفحات المصوّرة.

كل مزوّد (محلي أو خارجي) يطبّق Provider. المزوّد الخارجي (is_external=True)
لا يُستدعى أبداً إلا عبر cloud_gate (المرحلة 3)، الذي يرفض كل إرسال في الوضع الداخلي.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class PageImage:
    page_number: int
    png: bytes                       # بيانات الصورة (PNG للمحلي، JPEG للمزوّدات الخارجية)
    dpi: int
    mime: str = "image/png"
    ink: float | None = None         # نسبة الحبر في الصفحة (لفحص "قصيرة بشكل مريب")


@dataclass
class OcrRequest:
    images: list[PageImage]          # صفحة أو صفحتان
    previous_tail: str = ""          # آخر أسطر الصفحة السابقة (لإكمال الجداول)


@dataclass
class PageResult:
    page_number: int
    markdown: str
    meta: dict = field(default_factory=dict)


class EngineUnavailable(Exception):
    """المحرّك توقف أو تعذّر تشغيله: تتوقف الدفعة بدل تعليم كل الصفحات كخطأ."""


class ProviderError(Exception):
    """خطأ عابر من مزوّد خارجي (شبكة، 5xx، رد غير مفهوم)."""


class RateLimited(ProviderError):
    """429: انتظر ثم أعد المحاولة."""

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ServerBusy(ProviderError):
    """5xx / "high demand": ضغط مؤقت على خادم المزوّد — انتظر طويلاً ثم أعد، أو جرّب نموذجاً بديلاً."""

    def __init__(self, message: str = "", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExhausted(ProviderError):
    """انتهت الحصة (اليومية أو الرصيد): المزوّد خارج الخدمة حتى الغد."""


class AuthError(ProviderError):
    """المفتاح مرفوض (401/403): المزوّد خارج الخدمة حتى يُصلح المفتاح."""


class Provider(ABC):
    name: str = "base"
    is_external: bool = False

    @abstractmethod
    def ocr(self, request: OcrRequest) -> list[PageResult]:
        """يقرأ الصور ويعيد نتيجة لكل صفحة بنفس الترتيب."""

    def close(self) -> None:
        pass


class ExternalProvider(Provider):
    """مزوّد خارجي: ocr() مغلقة، والإرسال الوحيد عبر send() بتصريح من cloud_gate."""

    is_external = True
    max_pages_per_call: int = 1

    def ocr(self, request: OcrRequest) -> list[PageResult]:
        raise PermissionError("المزوّد الخارجي لا يُستدعى إلا عبر cloud_gate")

    def send(self, prompt: str, request: OcrRequest, _gate_token: object) -> str:
        from ..cloud_gate import check_token
        check_token(_gate_token)
        return self._complete(prompt, request.images)

    def send_text(self, prompt: str, text: str, _gate_token: object) -> str:
        """طلب نصي بلا صور (تلخيص). نفس قفل التصريح."""
        from ..cloud_gate import check_token
        check_token(_gate_token)
        return self._complete(f"{prompt}\n\n<<<\n{text}\n>>>", [])

    @abstractmethod
    def _complete(self, prompt: str, images: list[PageImage]) -> str:
        """يرسل التعليمات والصور ويعيد النص الخام."""
