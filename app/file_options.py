"""إعدادات كل ملف (تتبع بصمة الملف لا اسمه): "فرض OCR" ونطاق الصفحات المطلوب قراءتها."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .fsutil import atomic_write_json, read_json

log = logging.getLogger(__name__)


class FileOptions:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        try:
            raw = read_json(self.path, default={}) or {}
            if isinstance(raw, dict):
                self._data = {h: v for h, v in raw.items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("تعذّرت قراءة إعدادات الملفات، سيتم استخدام الافتراضي: %s", e)

    def force_ocr(self, file_hash: str) -> bool:
        with self._lock:
            return self._data.get(file_hash, {}).get("force_ocr") is True

    def set_force_ocr(self, items, value: bool) -> None:
        """items: أزواج (البصمة، اسم الملف)."""
        with self._lock:
            for file_hash, name in items:
                rec = self._data.setdefault(file_hash, {})
                rec["name"] = name
                rec["force_ocr"] = bool(value)
            atomic_write_json(self.path, self._data)

    def chat(self, file_hash: str) -> bool:
        with self._lock:
            return self._data.get(file_hash, {}).get("chat") is True

    def set_chat(self, file_hash: str, name: str, value: bool) -> None:
        with self._lock:
            rec = self._data.setdefault(file_hash, {})
            rec["name"] = name
            rec["chat"] = bool(value)
            atomic_write_json(self.path, self._data)

    def page_range(self, file_hash: str) -> str:
        with self._lock:
            return str(self._data.get(file_hash, {}).get("pages") or "")

    def set_page_range(self, file_hash: str, name: str, spec: str) -> None:
        with self._lock:
            rec = self._data.setdefault(file_hash, {})
            rec["name"] = name
            rec["pages"] = spec.strip()
            atomic_write_json(self.path, self._data)


def parse_page_range(spec: str, page_count: int) -> list[int] | None:
    """"1-5" أو "1,3,7-9" ← قائمة صفحات مرتبة. "" ← None (كل الصفحات). يرفع ValueError عند الخطأ."""
    spec = (spec or "").strip().replace("،", ",").replace("–", "-")
    if not spec:
        return None
    from .digits import normalize_digits
    out: set[int] = set()
    for part in normalize_digits(spec).split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if lo < 1 or hi < lo:
            raise ValueError(f"نطاق غير صالح: {part}")
        out.update(range(lo, min(hi, page_count) + 1))
    if not out:
        raise ValueError("لا توجد صفحات ضمن الملف")
    return sorted(out)
