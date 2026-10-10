"""سجل الإرسال sent_log.csv: أي صفحة من أي ملف أُرسلت لأي مزوّد ومتى.

لا يحتوي نص المستند ولا المفاتيح. يُكتب في مجلد بيانات البرنامج.
"""

from __future__ import annotations

import csv
import threading
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = ("time_utc", "file", "sha256", "page", "provider", "model", "batch_size", "result")


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, file_name: str, file_hash: str, pages: list[int], provider: str, model: str,
               result: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            # utf-8-sig حتى يفتحه Excel بالعربية بشكل صحيح
            with open(self.path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(COLUMNS)
                for p in pages:
                    w.writerow((now, file_name, file_hash, p, provider, model, len(pages), result))

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
