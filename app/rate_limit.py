"""ضبط معدل الطلبات لكل مزوّد، والانتظار التصاعدي عند 429، والعدّاد اليومي.

- حد طلبات بالدقيقة (نافذة منزلقة 60 ثانية)، الافتراضي 10.
- عند 429: انتظار تصاعدي (2، 4، 8… حتى 120 ثانية) أو Retry-After إن كان أكبر.
  بعد MAX_429 أخطاء متتالية يُعتبر المزوّد غير متاح لهذه الدفعة.
- عدّاد يومي لكل مزوّد يُحفظ في usage.json ويُصفّر مع تغيّر التاريخ المحلي.
- كل انتظار قابل للمقاطعة بزر الإيقاف.
"""

from __future__ import annotations

import collections
import json
import threading
import time
from datetime import date
from pathlib import Path
from typing import Callable

from .fsutil import atomic_write_json, read_json
from .providers.base import AuthError, ProviderError, QuotaExhausted, RateLimited, ServerBusy

MAX_BACKOFF = 120.0
MAX_429 = 6
MAX_ERRORS = 3
# ضغط مؤقت على الخادم (503): انتظار أطول قبل الاستسلام (~3 دقائق إجمالاً)
BUSY_DELAYS = (5, 10, 20, 40, 60, 60)


class GiveUp(ProviderError):
    """أخطاء متكررة: انقل العمل لمزوّد آخر (أو نموذج بديل إن كان busy)."""

    def __init__(self, message: str = "", busy: bool = False):
        super().__init__(message)
        self.busy = busy


class Stopped(Exception):
    """ضغط المستخدم إيقاف أثناء الانتظار."""


class UsageCounter:
    def __init__(self, path: Path, today: Callable[[], str] = lambda: date.today().isoformat()):
        self.path = Path(path)
        self._today = today
        self._lock = threading.Lock()
        try:
            self._data = read_json(self.path, default={}) or {}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _day(self) -> dict:
        d = self._today()
        if self._data.get("date") != d:
            month = self._data.get("month") if isinstance(self._data.get("month"), dict) else {}
            self._data = {"date": d, "counts": {}, "exhausted": [], "month": month}
        m = self._data.setdefault("month", {})
        if m.get("ym") != d[:7]:
            self._data["month"] = {"ym": d[:7], "counts": {}}
        return self._data

    def add_month(self, key: str, n: int = 1) -> int:
        """عدّاد شهري (مثل تحويلات محرّك OCR.space 3) يُصفّر مع تغيّر الشهر."""
        with self._lock:
            c = self._day()["month"]["counts"]
            c[key] = c.get(key, 0) + n
            atomic_write_json(self.path, self._data)
            return c[key]

    def get_month(self, key: str) -> int:
        with self._lock:
            return self._day()["month"]["counts"].get(key, 0)

    def add(self, pid: str, n: int = 1) -> int:
        with self._lock:
            day = self._day()
            day["counts"][pid] = day["counts"].get(pid, 0) + n
            atomic_write_json(self.path, self._data)
            return day["counts"][pid]

    def get(self, pid: str) -> int:
        with self._lock:
            return self._day()["counts"].get(pid, 0)

    def mark_exhausted(self, pid: str) -> None:
        with self._lock:
            day = self._day()
            if pid not in day["exhausted"]:
                day["exhausted"].append(pid)
            atomic_write_json(self.path, self._data)

    def is_exhausted(self, pid: str) -> bool:
        with self._lock:
            return pid in self._day()["exhausted"]


class RateLimiter:
    def __init__(self, pid: str, rpm: int, usage: UsageCounter, rpd: int | None = None,
                 stop: threading.Event | None = None, clock=time.monotonic, sleep=None,
                 rpmonth: int | None = None):
        self.pid = pid
        self.rpmonth = rpmonth
        self.rpm = max(1, int(rpm))
        self.rpd = rpd
        self.usage = usage
        self.stop = stop or threading.Event()
        self.clock = clock
        self._sleep = sleep
        self._times: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleep:
            self._sleep(seconds)
        elif self.stop.wait(seconds):
            raise Stopped()
        if self.stop.is_set():
            raise Stopped()

    def acquire(self) -> None:
        if self.usage.is_exhausted(self.pid):
            raise QuotaExhausted("الحصة اليومية منتهية")
        if self.rpd is not None and self.usage.get(self.pid) >= self.rpd:
            self.usage.mark_exhausted(self.pid)
            raise QuotaExhausted("بلغ العدّاد اليومي حد الخطة المجانية")
        if self.rpmonth is not None and self.usage.get_month(self.pid) >= self.rpmonth:
            raise QuotaExhausted("بلغ العدّاد الشهري حد الخطة المجانية")
        while True:
            with self._lock:
                now = self.clock()
                while self._times and now - self._times[0] >= 60:
                    self._times.popleft()
                if len(self._times) < self.rpm:
                    self._times.append(now)
                    break
                delay = 60 - (now - self._times[0]) + 0.05
            self._wait(delay)
        self.usage.add(self.pid)
        if self.rpmonth is not None:
            self.usage.add_month(self.pid)

    def call(self, fn: Callable[[], str], log: Callable[[str], None] = lambda s: None,
             on_switch: Callable[[str], bool] | None = None, max_429: int = MAX_429) -> str:
        """on_switch(kind): ينقل لنموذج آخر أقل ازدحاماً؛ True = أعد فوراً بلا انتظار.
        max_429: عدد أخطاء 429 المتتالية قبل الاستسلام (أقل ⇒ يتسلّم مزوّد آخر أسرع).
        ينفّذ طلباً واحداً مع ضبط المعدل والانتظار التصاعدي. يرفع QuotaExhausted أو GiveUp."""
        n429 = errors = busy = 0
        while True:
            if self.stop.is_set():
                raise Stopped()
            self.acquire()
            try:
                return fn()
            except QuotaExhausted:
                if on_switch and on_switch("quota"):
                    continue
                self.usage.mark_exhausted(self.pid)
                raise
            except RateLimited as e:
                if on_switch and on_switch("limited"):
                    continue
                n429 += 1
                if n429 >= max_429:
                    raise GiveUp(f"{self.pid}: 429 متكرر") from None
                delay = min(MAX_BACKOFF, 2.0 ** n429)
                if e.retry_after is not None:
                    delay = max(delay, min(e.retry_after, 3600))
                log(f"{self.pid}: 429 — انتظار {delay:.0f} ث")
                self._wait(delay)
            except ServerBusy as e:
                if on_switch and on_switch("busy"):
                    continue
                if busy >= len(BUSY_DELAYS):
                    raise GiveUp(f"{self.pid}: الخادم مشغول ({e})", busy=True) from None
                delay = BUSY_DELAYS[busy]
                if e.retry_after is not None:
                    delay = max(delay, min(e.retry_after, 300))
                busy += 1
                log(f"{self.pid}: الخادم مشغول مؤقتاً — محاولة {busy + 1} بعد {delay:.0f} ث")
                self._wait(delay)
            except (GiveUp, Stopped, AuthError):
                raise
            except ProviderError as e:
                errors += 1
                if errors >= MAX_ERRORS:
                    raise GiveUp(f"{self.pid}: أخطاء متكررة ({e})") from None
                self._wait(min(MAX_BACKOFF, 3.0 * errors))
