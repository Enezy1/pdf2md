"""بصمة SHA-256 للملفات مع ذاكرة مؤقتة حتى لا نعيد حساب بصمة ملف لم يتغير."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path

from .fsutil import atomic_write_json, read_json

log = logging.getLogger(__name__)


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class HashCache:
    """يربط (المسار، الحجم، وقت التعديل) ببصمة الملف."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._data: dict = read_json(self.path, default={}) or {}
        except (json.JSONDecodeError, OSError):
            log.warning("ذاكرة البصمات تالفة، سيُعاد إنشاؤها")
            self._data = {}

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(path))

    def get(self, path: Path) -> str:
        st = os.stat(path)
        key = self._key(path)
        with self._lock:
            rec = self._data.get(key)
            if rec and rec.get("size") == st.st_size and rec.get("mtime_ns") == st.st_mtime_ns:
                return rec["sha256"]
        digest = sha256_file(path)
        with self._lock:
            self._data[key] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}
        return digest

    def save(self) -> None:
        with self._lock:
            data = dict(self._data)
        atomic_write_json(self.path, data)
