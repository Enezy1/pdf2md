"""قاموس الكلمات العربية المحلي: يُبنى من صفحات طبقة النص السليمة في مستندات المستخدم.

- الملف الفعلي في مجلد بيانات البرنامج (%APPDATA%\\pdf2md\\arabic_words.txt)، لا في مجلد
  البرنامج، حتى لا يدخل في ملف التثبيت. resources/arabic_words.txt بذرة فارغة فقط.
- لا يُستخدم أي قاموس خارجي، ولا يغادر الملف الجهاز (لا يمر بأي كود شبكة).
- الصيغة: سطر لكل كلمة "الكلمة<TAB>عدد مرات ورودها". الكلمة مطبّعة (بلا تشكيل، أ/إ/آ→ا،
  ى→ي، ة→ه) حتى لا تُعد الفروق الإملائية البسيطة كلمات مجهولة.
- كل صفحة تُضاف مرة واحدة فقط ("بصمة الملف:رقم الصفحة" تُحفظ في arabic_words.sources).
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from pathlib import Path

from .config import app_data_dir
from .fsutil import atomic_write_text

log = logging.getLogger(__name__)

FILE_NAME = "arabic_words.txt"
SEED = Path(__file__).resolve().parent.parent / "resources" / FILE_NAME

_DIAC = re.compile(r"[ً-ٰٟـ]")
_UNIFY = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_WORD = re.compile(r"[ء-يٱ-ۓ]+")

# سوابق ولواحق شائعة: الكلمة معروفة إذا كان جذرها (بعد نزعها) في القاموس
_PREFIXES = sorted(["و", "ف", "ب", "ك", "ل", "ال", "وال", "بال", "كال", "فال", "لل", "ولل", "س", "وب", "ول"],
                   key=len, reverse=True)
_SUFFIXES = sorted(["ه", "ها", "هم", "هما", "هن", "ك", "كم", "نا", "ي", "ات", "ان", "ين", "ون", "يه", "يها",
                    "تها", "ته", "تهم", "وا", "تم", "تان", "تين"], key=len, reverse=True)
MIN_WORD = 2


def normalize_word(w: str) -> str:
    from .pdf_io import normalize_arabic
    return _DIAC.sub("", normalize_arabic(w)).translate(_UNIFY)


def arabic_words(text: str) -> list[str]:
    from .pdf_io import normalize_arabic
    t = _DIAC.sub("", normalize_arabic(re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)))
    return [w.translate(_UNIFY) for w in _WORD.findall(t) if len(w) >= MIN_WORD]


class ArabicDictionary:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_data_dir() / FILE_NAME
        self.sources_path = self.path.with_suffix(".sources")
        self._lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.sources: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists() and SEED.exists():
            try:
                shutil.copyfile(SEED, self.path)
            except OSError:
                pass
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                w, _, c = line.partition("\t")
                self.counts[w.strip()] = int(c) if c.strip().isdigit() else 1
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("تعذّرت قراءة القاموس: %s", e)
        try:
            self.sources = set(self.sources_path.read_text(encoding="utf-8").split())
        except OSError:
            self.sources = set()

    def __len__(self) -> int:
        return len(self.counts)

    # ------------------------------------------------------------ البحث
    def known(self, word: str) -> bool:
        w = normalize_word(word)
        if w in self.counts:
            return True
        for p in [""] + _PREFIXES:
            if p and not w.startswith(p):
                continue
            stem = w[len(p):]
            if len(stem) < MIN_WORD:
                continue
            if stem in self.counts:
                return True
            for s in _SUFFIXES:
                if stem.endswith(s) and len(stem) - len(s) >= MIN_WORD and stem[: -len(s)] in self.counts:
                    return True
        return False

    def unknown_ratio(self, text: str) -> tuple[float, int, list[str]]:
        """(نسبة الكلمات المجهولة، عدد الكلمات، عينة منها)."""
        words = arabic_words(text)
        if not words:
            return 0.0, 0, []
        unknown = [w for w in words if not self.known(w)]
        return len(unknown) / len(words), len(words), list(dict.fromkeys(unknown))[:5]

    # ------------------------------------------------------------ البناء
    def add_text(self, text: str) -> int:
        n = 0
        for w in arabic_words(text):
            self.counts[w] = self.counts.get(w, 0) + 1
            n += 1
        return n

    def add_pages(self, items: list[tuple[str, str]]) -> int:
        """items: [(مفتاح الصفحة "بصمة:رقم"، نصها)]. كل صفحة تُضاف مرة واحدة فقط.
        يعيد عدد الصفحات المضافة."""
        with self._lock:
            new = [(k, t) for k, t in items if k not in self.sources]
            for k, t in new:
                self.add_text(t)
                self.sources.add(k)
            if new:
                self.save()
            return len(new)

    def save(self) -> None:
        lines = [f"{w}\t{c}" for w, c in sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        atomic_write_text(self.path, "\n".join(lines) + "\n")
        atomic_write_text(self.sources_path, "\n".join(sorted(self.sources)) + "\n")
