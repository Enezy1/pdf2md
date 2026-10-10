"""كتالوج المزوّدات (resources/providers_catalog.json) وحالة كل مزوّد عند المستخدم.

- الكتالوج قابل للتعديل يدوياً: إضافة مزوّد أو تحديث حدوده بدون تغيير الكود.
- حالة المستخدم (تفعيل، نموذج، أولوية، حد الدقيقة، الحقول الإضافية، المزوّدات المخصّصة)
  في providers_state.json داخل مجلد بيانات البرنامج. المفاتيح ليست هنا (secrets_store).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import app_data_dir
from ..fsutil import atomic_write_json, read_json

log = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).resolve().parents[2] / "resources" / "providers_catalog.json"
FORMAT_ID = "pdf2md-providers-catalog"
DEFAULT_RPM = 10
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


@dataclass
class ProviderSpec:
    id: str
    name: str
    kind: str                      # "gemini" أو "openai" أو "ocr" (محرّك OCR بلا تعليمات)
    description: str = ""
    base_url: str = ""
    key_url: str = ""
    default_model: str = ""
    accepts_images: bool = True
    max_pages_per_call: int = 1
    free_tier: dict = field(default_factory=dict)
    data_training: str = ""
    quota_unit: str = "requests"   # "neurons" ⇒ العدّاد اليومي تقريبي
    extra_fields: list = field(default_factory=list)
    verified_on: str = ""
    unverified: list = field(default_factory=list)
    retired: bool = False
    sources: list = field(default_factory=list)
    fallback_models: list = field(default_factory=list)
    custom: bool = False

    @property
    def has_unverified(self) -> bool:
        return bool(self.unverified)

    @property
    def approx_counter(self) -> bool:
        return self.quota_unit != "requests"

    @property
    def rpd(self) -> int | None:
        v = self.free_tier.get("rpd")
        return v if isinstance(v, int) and v > 0 else None

    @property
    def rpmonth(self) -> int | None:
        v = self.free_tier.get("rpmonth")
        return v if isinstance(v, int) and v > 0 else None

    @property
    def is_ocr(self) -> bool:
        return self.kind == "ocr"

    def resolved_base_url(self, extra: dict) -> str:
        url = self.base_url
        for f in self.extra_fields:
            fid = f["id"]
            val = (extra.get(fid) or "").strip()
            if "{" + fid + "}" in url:
                if not re.fullmatch(r"[A-Za-z0-9_-]+", val):
                    raise ValueError(f"{f.get('label', fid)}: قيمة مفقودة أو غير صالحة")
                url = url.replace("{" + fid + "}", val)
        return url.rstrip("/")


@dataclass
class ProviderState:
    enabled: bool = False
    model: str = ""
    priority: int = 100
    rpm: int = DEFAULT_RPM
    extra: dict = field(default_factory=dict)


def _spec_from(d: dict, custom: bool = False) -> ProviderSpec:
    known = {f for f in ProviderSpec.__dataclass_fields__}
    spec = ProviderSpec(**{k: v for k, v in d.items() if k in known})
    spec.custom = custom
    if not _ID_RE.match(spec.id):
        raise ValueError(f"معرّف مزوّد غير صالح: {spec.id!r}")
    if spec.kind not in ("gemini", "openai", "ocr"):
        raise ValueError(f"نوع مزوّد غير معروف: {spec.kind!r}")
    spec.max_pages_per_call = max(1, int(spec.max_pages_per_call or 1))
    return spec


def load_catalog(path: Path = CATALOG_PATH) -> list[ProviderSpec]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.error("تعذّرت قراءة كتالوج المزوّدات: %s", e)
        return []
    if data.get("format") != FORMAT_ID:
        log.error("ملف الكتالوج ليس بالصيغة المتوقعة")
        return []
    out = []
    for d in data.get("providers", []):
        try:
            out.append(_spec_from(d))
        except (TypeError, ValueError) as e:
            log.error("مزوّد غير صالح في الكتالوج: %s", e)
    return out


class ProviderConfig:
    """الكتالوج + المزوّدات المخصّصة + حالة كل مزوّد."""

    def __init__(self, state_path: Path | None = None, catalog_path: Path = CATALOG_PATH):
        self.state_path = state_path or (app_data_dir() / "providers_state.json")
        self._lock = threading.RLock()
        raw = {}
        try:
            raw = read_json(self.state_path, default={}) or {}
        except (OSError, json.JSONDecodeError):
            log.warning("حالة المزوّدات تالفة، سيُبدأ من الافتراضي")
        self.specs: dict[str, ProviderSpec] = {s.id: s for s in load_catalog(catalog_path)}
        for d in raw.get("custom", []):
            try:
                s = _spec_from(d, custom=True)
                if s.id not in self.specs:
                    self.specs[s.id] = s
            except (TypeError, ValueError) as e:
                log.error("مزوّد مخصص غير صالح: %s", e)
        self.states: dict[str, ProviderState] = {}
        for pid, st in (raw.get("states") or {}).items():
            if isinstance(st, dict):
                known = {f for f in ProviderState.__dataclass_fields__}
                self.states[pid] = ProviderState(**{k: v for k, v in st.items() if k in known})

    def state(self, pid: str) -> ProviderState:
        with self._lock:
            if pid not in self.states:
                spec = self.specs.get(pid)
                self.states[pid] = ProviderState(model=spec.default_model if spec else "")
            st = self.states[pid]
            if not st.model and pid in self.specs:
                st.model = self.specs[pid].default_model
            return st

    def save(self) -> None:
        with self._lock:
            data = {
                "custom": [
                    {k: v for k, v in asdict(s).items() if k != "custom"}
                    for s in self.specs.values() if s.custom
                ],
                "states": {k: asdict(v) for k, v in self.states.items()},
            }
        atomic_write_json(self.state_path, data)

    def add_custom(self, name: str, base_url: str, model: str, max_pages: int = 1) -> ProviderSpec:
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("الرابط الأساسي يجب أن يبدأ بـ https://")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "custom"
        pid = f"custom-{slug}"
        with self._lock:
            n = 2
            while pid in self.specs:
                pid = f"custom-{slug}-{n}"
                n += 1
            spec = ProviderSpec(
                id=pid, name=name.strip() or pid, kind="openai", description="مزوّد مخصص بصيغة OpenAI",
                base_url=base_url, default_model=model.strip(), max_pages_per_call=max(1, max_pages),
                data_training="غير معروف — راجع شروط الخدمة.", unverified=["limits", "data_training"],
                custom=True,
            )
            self.specs[pid] = spec
            self.state(pid).model = spec.default_model
        self.save()
        return spec

    def remove_custom(self, pid: str) -> None:
        with self._lock:
            if pid in self.specs and self.specs[pid].custom:
                del self.specs[pid]
                self.states.pop(pid, None)
        self.save()

    def can_enable(self, pid: str) -> tuple[bool, str]:
        spec = self.specs.get(pid)
        if spec is None:
            return False, "مزوّد غير معروف"
        if spec.retired:
            return False, "الخدمة متوقفة"
        if not spec.accepts_images:
            return False, "لا يقبل الصور"
        return True, ""

    def on_key_saved(self, pid: str) -> bool:
        """بعد إدخال المفتاح: يُفعَّل تلقائياً فقط إذا كانت كل بنوده متحققاً منها."""
        spec = self.specs[pid]
        ok, _ = self.can_enable(pid)
        if ok and not spec.has_unverified:
            self.state(pid).enabled = True
            self.save()
            return True
        return False

    def active(self, secrets) -> list[tuple[ProviderSpec, ProviderState, str]]:
        """المزوّدات المفعّلة التي لها مفتاح، مرتّبة بالأولوية (الأصغر أولاً)."""
        out = []
        for pid, spec in self.specs.items():
            st = self.state(pid)
            if not st.enabled or not self.can_enable(pid)[0]:
                continue
            key = secrets.get_key(pid)
            if key:
                out.append((spec, st, key))
        out.sort(key=lambda t: (t[1].priority, t[0].name))
        return out
