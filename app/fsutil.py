"""كتابة ذرّية للملفات: لا يبقى ملف نصف مكتوب إذا انقطع البرنامج."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # على ويندوز قد يفشل الاستبدال لحظياً إذا كان الملف مفتوحاً لدى برنامج حماية أو محرر.
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def write_markdown_file(path: Path, text: str, normalize: bool = True) -> None:
    """نقطة الكتابة الوحيدة لكل Markdown نهائي (الملفات، INDEX.md، تقرير المراجعة).
    ملفات raw لا تمر من هنا."""
    if normalize:
        from .digits import normalize_digits
        text = normalize_digits(text)
    atomic_write_text(path, text)


def atomic_write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
