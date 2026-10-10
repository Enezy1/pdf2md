"""توحيد الأرقام إلى الإنجليزية قبل كتابة أي Markdown.

نقطة التطبيق الوحيدة: fsutil.write_markdown_file (الملف النهائي، INDEX.md).
ملفات النص الخام (pNNNN.raw.md) لا تمر من هنا لتبقى مرجعاً كما خرجت من المحرّك.

ما يتغير فقط:
- الأرقام العربية-الهندية ٠-٩ والفارسية ۰-۹ ← 0-9
- الفاصلة العشرية العربية ٫ ← "."  وفاصلة الآلاف ٬ ← ","
- حذف علامات الاتجاه الخفية (LRM/RLM/ALM وعلامات التضمين والعزل)
الفاصلة العربية "،" وكل حرف آخر وترتيب الأرقام لا تُلمس.
"""

from __future__ import annotations

import re

_TABLE = str.maketrans(
    {**{chr(0x0660 + i): str(i) for i in range(10)},
     **{chr(0x06F0 + i): str(i) for i in range(10)},
     "٫": ".", "٬": ","}
)
BIDI_MARKS = "‎‏؜‪‫‬‭‮⁦⁧⁨⁩"
_BIDI_RE = re.compile(f"[{BIDI_MARKS}]")
_NON_LATIN_DIGIT = re.compile("[٠-٩۰-۹٫٬]")


def normalize_digits(text: str) -> str:
    if not text:
        return text
    return _BIDI_RE.sub("", text.translate(_TABLE))


def count_changes(text: str) -> int:
    """عدد الأرقام/الفواصل التي سيغيّرها normalize_digits (بدون علامات الاتجاه)."""
    return len(_NON_LATIN_DIGIT.findall(text or ""))


DIGITS_RULE = "Always write every number using Western digits 0-9 (never Arabic-Indic or Persian digits)."
