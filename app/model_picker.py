"""اختيار النموذج تلقائياً لكل مزوّد: دائماً الأقل ازدحاماً.

لكل مزوّد قائمة نماذج من الكتالوج (default_model + fallback_models). البرنامج يبدأ بالنموذج
الأقل مشاكل في آخر 10 دقائق (503 أو 429)، وعند أي ازدحام ينتقل فوراً لنموذج آخر لم يُجرَّب
في هذه الجولة. فقط إذا كانت كل النماذج مزدحمة ينتظر ثم يعيد. النموذج الذي انتهت حصته اليومية
يُستبعد حتى نهاية اليوم. الحالة محفوظة في الذاكرة طوال جلسة البرنامج.
"""

from __future__ import annotations

import threading
import time
from datetime import date

WINDOW = 600  # ثانية: مدة تذكّر ازدحام النموذج

_lock = threading.Lock()
_pickers: dict[str, "ModelPicker"] = {}


class ModelPicker:
    def __init__(self, pid: str, models: list[str], clock=time.monotonic, sticky: bool = False):
        self.pid = pid
        self.sticky = sticky    # لا ينتقل إلا عند انتهاء الحصة اليومية (الازدحام وحد الدقيقة: انتظار)
        self.models = list(dict.fromkeys(m for m in models if m))
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {m: [] for m in self.models}
        self._exhausted: dict[str, str] = {}  # model → التاريخ
        self._tried: set[str] = set()
        self.current = self.models[0] if self.models else ""
        self.current = self.best()

    def _penalty(self, m: str) -> int:
        now = self.clock()
        self._hits[m] = [t for t in self._hits[m] if now - t < WINDOW]
        return len(self._hits[m])

    def _available(self) -> list[str]:
        today = date.today().isoformat()
        return [m for m in self.models if self._exhausted.get(m) != today]

    def best(self, exclude: set[str] = frozenset()) -> str:
        with self._lock:
            pool = [m for m in self._available() if m not in exclude]
            if not pool:
                return ""
            return min(pool, key=lambda m: (self._penalty(m), self.models.index(m)))

    def ok(self, model: str) -> None:
        with self._lock:
            self._tried.clear()

    def switch(self, kind: str) -> tuple[bool, str, str]:
        """يسجّل مشكلة النموذج الحالي ويحاول الانتقال لغيره.
        kind: "busy" أو "limited" أو "quota". يعيد (انتقل؟، القديم، الجديد)."""
        old = self.current
        if self.sticky and kind != "quota":
            return False, old, old
        with self._lock:
            if kind == "quota":
                self._exhausted[old] = date.today().isoformat()
            elif old in self._hits:
                self._hits[old].append(self.clock())
            self._tried.add(old)
        nxt = self.best(exclude=self._tried)
        if nxt:
            self.current = nxt
            return True, old, nxt
        with self._lock:
            self._tried.clear()  # جولة جديدة بعد الانتظار
        fallback = self.best()
        if fallback:
            self.current = fallback
        return False, old, self.current

    @property
    def all_exhausted(self) -> bool:
        return not self._available()


def picker_for(pid: str, models: list[str], sticky: bool = False) -> ModelPicker:
    """نفس الكائن طوال الجلسة حتى يتذكر البرنامج أي النماذج كانت مزدحمة."""
    with _lock:
        p = _pickers.get(pid)
        if p is None or p.models != list(dict.fromkeys(m for m in models if m)) or p.sticky != sticky:
            p = _pickers[pid] = ModelPicker(pid, models, sticky=sticky)
        return p


def models_for(spec, state=None) -> list[str]:
    """النماذج المرشحة: الافتراضي ثم البدائل (والمخصص: نموذجه الوحيد)."""
    first = spec.default_model or (state.model if state else "")
    if spec.custom and state and state.model:
        first = state.model
    return list(dict.fromkeys([first, *spec.fallback_models]))
