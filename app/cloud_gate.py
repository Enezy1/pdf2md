"""البوابة الوحيدة للاتصال الخارجي.

القواعد (قفل في الكود لا في الواجهة):
- البوابة تحسب بصمة الملف بنفسها من القرص عند فتحه (open_file) ولا تقبل بصمة من المستدعي.
- قبل كل إرسال: مقارنة الحجم وتاريخ التعديل فقط؛ إذا تغيّرا تُعاد البصمة كاملة، وإن
  اختلفت البصمة يُرفض الإرسال.
- وضع التشغيل: "local" (الافتراضي) يمنع كل اتصال خارجي يحمل محتوى مستند؛ لا يُسمح
  بالإرسال إلا في وضع "cloud" ("عبر المواقع")، ويُقرأ الوضع قبل كل إرسال.
- المزوّد الخارجي لا يرسل إلا بتصريح _TOKEN الخاص بهذه الوحدة (ExternalProvider.send).
- كل محاولة إرسال تُسجَّل في sent_log.csv (بدون نص المستند).
- اختبار الاتصال يرسل صورة وهمية يولّدها البرنامج فقط.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .audit_log import AuditLog
from .hashing import sha256_file
from .providers.base import ExternalProvider, OcrRequest
from .secrets_store import redact

log = logging.getLogger(__name__)

_TOKEN = object()


def check_token(token: object) -> None:
    if token is not _TOKEN:
        raise PermissionError("الإرسال الخارجي مسموح فقط عبر cloud_gate")


LOCAL, CLOUD = "local", "cloud"


class CloudBlockedError(PermissionError):
    """الوضع الحالي "داخلي": لا يغادر الجهاز شيء."""


class FileChangedError(PermissionError):
    pass


@dataclass
class GateTicket:
    path: Path
    file_hash: str
    size: int
    mtime_ns: int

    @property
    def name(self) -> str:
        return self.path.name


class CloudGate:
    def __init__(self, audit: AuditLog, mode: str = LOCAL):
        self.audit = audit
        self._mode = LOCAL
        self.set_mode(mode)

    # ---------------------------------------------------------------- الوضع
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = CLOUD if mode == CLOUD else LOCAL

    def allowed(self, file_hash: str = "") -> bool:
        """للعرض والتخطيط فقط. القرار الفعلي في open_file/send."""
        return self._mode == CLOUD

    def _check_mode(self, name: str) -> None:
        if self._mode != CLOUD:
            raise CloudBlockedError(f"{name}: الوضع الداخلي — الاتصال الخارجي ممنوع")

    def open_file(self, path: Path) -> GateTicket:
        """تُحسب البصمة مرة واحدة عند بدء الملف."""
        path = Path(path)
        self._check_mode(path.name)
        st = os.stat(path)
        digest = sha256_file(path)
        return GateTicket(path, digest, st.st_size, st.st_mtime_ns)

    def _revalidate(self, ticket: GateTicket) -> None:
        # الوضع قد يتغير أثناء الدفعة: يُقرأ قبل كل إرسال.
        self._check_mode(ticket.name)
        st = os.stat(ticket.path)
        if st.st_size != ticket.size or st.st_mtime_ns != ticket.mtime_ns:
            digest = sha256_file(ticket.path)
            if digest != ticket.file_hash:
                raise FileChangedError(f"{ticket.name}: تغيّر محتوى الملف منذ بدء معالجته")
            ticket.size, ticket.mtime_ns = st.st_size, st.st_mtime_ns

    # ---------------------------------------------------------------- الإرسال
    def send(self, ticket: GateTicket, provider: ExternalProvider, prompt: str, request: OcrRequest,
             model: str = "") -> str:
        if not isinstance(ticket, GateTicket):
            raise PermissionError("تصريح غير صالح")
        if not isinstance(provider, ExternalProvider):
            raise PermissionError("ليس مزوّداً خارجياً معروفاً")
        self._revalidate(ticket)
        pages = [img.page_number for img in request.images]
        result = "error"
        try:
            text = provider.send(prompt, request, _TOKEN)
            result = "ok"
            return text
        except Exception as e:
            result = f"error:{type(e).__name__}"
            raise
        finally:
            self.audit.record(ticket.name, ticket.file_hash, pages, provider.name, model, result)
            log.info("إرسال %s ص%s → %s: %s", ticket.name, pages, provider.name, result)

    def send_text(self, ticket: GateTicket, provider: ExternalProvider, prompt: str, text: str,
                  model: str = "", purpose: str = "summary") -> str:
        """إرسال نص ناتج (لا صور) — للتلخيص في الفهرس. نفس فحوص send.
        يُسجَّل في sent_log.csv بالصفحة 0 وبالغرض في عمود النموذج."""
        if not isinstance(ticket, GateTicket):
            raise PermissionError("تصريح غير صالح")
        if not isinstance(provider, ExternalProvider):
            raise PermissionError("ليس مزوّداً خارجياً معروفاً")
        self._revalidate(ticket)
        result = "error"
        try:
            out = provider.send_text(prompt, text, _TOKEN)
            result = "ok"
            return out
        except Exception as e:
            result = f"error:{type(e).__name__}"
            raise
        finally:
            self.audit.record(ticket.name, ticket.file_hash, [0], provider.name, f"{purpose}:{model}", result)
            log.info("إرسال نص (%s) %s → %s: %s", purpose, ticket.name, provider.name, result)

    def probe(self, provider: ExternalProvider, model: str = "") -> str:
        """اختبار الاتصال بصورة وهمية يولّدها البرنامج (لا مستند حقيقي).
        مسموح في كل الأوضاع لأنه لا يحمل أي محتوى من ملفات المستخدم."""
        from .providers.render import probe_image
        img = probe_image()
        prompt = "This is a connection test image. Reply with the text you see in it."
        try:
            text = provider.send(prompt, OcrRequest([img]), _TOKEN)
        finally:
            self.audit.record("[صورة اختبار وهمية]", "-", [0], provider.name, model, "probe")
        return redact(text)
