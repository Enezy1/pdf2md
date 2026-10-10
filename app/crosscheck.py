"""فحص مستقل لطبقة النص: مقارنة النص المستخرج بنتيجة OCR للصفحة نفسها.

كاشف الأنماط (quality.py) لا يلتقط كل أنواع التلف، فهذا الفحص يقيس التطابق على مستوى
الكلمات (F1 على مجموعة الكلمات بعد التطبيع). لا يتأثر بترتيب الأسطر أو الأعمدة، لكن
أي كلمة مشوّهة في طبقة النص تخفّض النسبة.
"""

from __future__ import annotations

import re
from collections import Counter

from .pdf_io import normalize_arabic

CHECK_VERSION = 1

_DIACRITICS = re.compile(r"[ً-ٰٟـ]")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
# توحيد الحروف التي يختلف فيها OCR عادة دون أن يعني ذلك تلفاً
_UNIFY = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "٠": "0", "١": "1", "٢": "2",
                        "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9"})


def tokens(text: str) -> Counter:
    t = _DIACRITICS.sub("", normalize_arabic(text)).translate(_UNIFY).lower()
    return Counter(_TOKEN.findall(t))


def match_score(text_layer: str, ocr_text: str) -> float:
    a, b = tokens(text_layer), tokens(ocr_text)
    total = sum(a.values()) + sum(b.values())
    if total == 0:
        return 1.0
    common = sum((a & b).values())
    return round(2 * common / total, 3)
