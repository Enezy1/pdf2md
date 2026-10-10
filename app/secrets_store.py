"""حفظ مفاتيح المزوّدات خارج الكود وخارج الإعدادات النصية.

الترتيب: keyring (مدير بيانات الاعتماد في ويندوز) إن كانت المكتبة مثبّتة، وإلا DPAPI
(تشفير ويندوز المرتبط بحساب المستخدم) في ملف secrets.dat. الملف لا يُفك إلا على نفس
الحساب ونفس الجهاز.

كل مفتاح يُحمَّل أو يُحفظ يُسجَّل في RedactingFilter، فلا يظهر في أي سجل حتى لو تسرّب
داخل رسالة خطأ.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path

from .config import app_data_dir
from .fsutil import atomic_write_json, read_json

log = logging.getLogger(__name__)

SERVICE = "pdf2md"
REDACTED = "«مفتاح محجوب»"

_known: set[str] = set()
_known_lock = threading.Lock()


def register_secret(value: str) -> None:
    if value and len(value) >= 6:
        with _known_lock:
            _known.add(value)


def redact(text: str) -> str:
    with _known_lock:
        secrets = sorted(_known, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, REDACTED)
    return text


class RedactingFilter(logging.Filter):
    """يحجب أي مفتاح معروف من رسالة السجل ومن نص الاستثناء."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and record.exc_info[1] is not None:
            import traceback
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def install_log_redaction() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())


# ---------------------------------------------------------------- الخلفيات
class _MemoryBackend:
    """للاختبارات فقط."""

    def __init__(self):
        self.d: dict[str, str] = {}

    def get(self, name):
        return self.d.get(name)

    def set(self, name, value):
        self.d[name] = value

    def delete(self, name):
        self.d.pop(name, None)


class _KeyringBackend:
    def __init__(self):
        import keyring  # noqa: F401 — يفشل هنا إن لم تكن مثبّتة
        self.kr = keyring

    def get(self, name):
        return self.kr.get_password(SERVICE, name)

    def set(self, name, value):
        self.kr.set_password(SERVICE, name, value)

    def delete(self, name):
        try:
            self.kr.delete_password(SERVICE, name)
        except Exception:
            pass


class _DpapiBackend:
    def __init__(self, path: Path):
        if os.name != "nt":
            raise OSError("DPAPI متاح على ويندوز فقط")
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def _crypt(data: bytes, protect: bool) -> bytes:
        import ctypes
        from ctypes import wintypes

        class BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(data, len(data))
        inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        # CRYPTPROTECT_UI_FORBIDDEN = 0x1
        ok = fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out))
        if not ok:
            raise OSError("فشل DPAPI")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)

    def _load(self) -> dict:
        return read_json(self.path, default={}) or {}

    def get(self, name):
        with self._lock:
            blob = self._load().get(name)
        if not blob:
            return None
        return self._crypt(base64.b64decode(blob), protect=False).decode("utf-8")

    def set(self, name, value):
        enc = base64.b64encode(self._crypt(value.encode("utf-8"), protect=True)).decode()
        with self._lock:
            d = self._load()
            d[name] = enc
            atomic_write_json(self.path, d)

    def delete(self, name):
        with self._lock:
            d = self._load()
            if d.pop(name, None) is not None:
                atomic_write_json(self.path, d)


def _default_backend():
    if os.environ.get("PDF2MD_SECRETS_BACKEND") == "memory":
        return _MemoryBackend()
    try:
        return _KeyringBackend()
    except Exception:
        return _DpapiBackend(app_data_dir() / "secrets.dat")


class SecretsStore:
    def __init__(self, backend=None):
        self.backend = backend or _default_backend()

    @staticmethod
    def _name(provider_id: str) -> str:
        return f"api_key:{provider_id}"

    def get_key(self, provider_id: str) -> str | None:
        try:
            v = self.backend.get(self._name(provider_id))
        except Exception as e:
            log.warning("تعذّرت قراءة مفتاح %s: %s", provider_id, type(e).__name__)
            return None
        if v:
            register_secret(v)
        return v or None

    def set_key(self, provider_id: str, value: str) -> None:
        value = value.strip()
        if not value:
            self.delete_key(provider_id)
            return
        register_secret(value)
        self.backend.set(self._name(provider_id), value)
        log.info("حُفظ مفتاح المزوّد %s", provider_id)

    def delete_key(self, provider_id: str) -> None:
        self.backend.delete(self._name(provider_id))

    def has_key(self, provider_id: str) -> bool:
        return self.get_key(provider_id) is not None
