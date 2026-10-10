"""قراءة PDF عبر PyMuPDF: تصنيف الصفحة (نصية/مصوّرة/فارغة)، استخراج النص، ورسم الصفحة.

ملاحظة: في كثير من ملفات PDF العربية تكون طبقة النص تالفة (حروف خاصة بلا مقابل
Unicode، أو حروف لاتينية مكان "ل"، أو كلمات معكوسة). نفحص جودة الطبقة، وإذا كانت
تالفة نعامل الصفحة كمصوّرة حتى يقرأها النموذج البصري بدل إخراج نص مشوّه.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum

import pymupdf as fitz

from . import quality


class PageKind(str, Enum):
    TEXT = "text"
    SCANNED = "scanned"
    BLANK = "blank"


@dataclass
class PageInfo:
    kind: PageKind
    visible_chars: int = 0
    invisible_chars: int = 0
    image_coverage: float = 0.0
    reasons: list[str] = field(default_factory=list)
    # رمز سبب تحويل صفحة نصية إلى مصوّرة (للملخص): quality.REASON_CODE أو "forced_ocr"
    converted_reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["image_coverage"] = round(self.image_coverage, 3)
        return d


# ---------------- فحص جودة طبقة النص ----------------

_ARABIC_LETTER = re.compile(r"[ء-يٱ-ۓﭐ-﷿ﹰ-ﻼ]")
_PRESENTATION = re.compile(r"[ﭐ-﷿ﹰ-﻿]")


def _is_bad_char(ch: str) -> bool:
    cp = ord(ch)
    return ch == "�" or 0xE000 <= cp <= 0xF8FF or (cp < 32 and ch not in "\n\t\r")


def text_layer_problems(text: str) -> list[str]:
    """يعيد قائمة أسباب تجعل طبقة النص غير موثوقة (فارغة = سليمة)."""
    problems: list[str] = []
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return problems
    bad = sum(1 for c in chars if _is_bad_char(c))
    if bad / len(chars) > 0.05:
        problems.append(f"حروف بلا مقابل Unicode ({bad})")
    alnum = sum(1 for c in chars if c.isalnum())
    if alnum / len(chars) < 0.5:
        problems.append(f"رموز بلا حروف ({len(chars) - alnum}/{len(chars)})")

    words = [w for w in re.split(r"\s+", text) if _ARABIC_LETTER.search(w)]
    if words:
        # كلمة عربية فيها حروف لاتينية/غريبة في وسطها => ترميز خط تالف
        mixed = sum(
            1 for w in words
            if any(c.isalpha() and not _ARABIC_LETTER.match(c) and not ("ً" <= c <= "ٟ") for c in w)
        )
        if mixed / len(words) > 0.05:
            problems.append(f"كلمات عربية مختلطة بحروف غريبة ({mixed}/{len(words)})")

        # أشكال العرض: حرف "نهائي" في بداية الكلمة أو "ابتدائي" في نهايتها => ترتيب معكوس
        reversed_words = 0
        checked = 0
        for w in words:
            letters = [c for c in w if _PRESENTATION.match(c)]
            if len(letters) < 2:
                continue
            checked += 1
            first = unicodedata.decomposition(letters[0])
            last = unicodedata.decomposition(letters[-1])
            if first.startswith(("<final>", "<medial>")) or last.startswith(("<initial>", "<medial>")):
                reversed_words += 1
        if checked >= 3 and reversed_words / checked > 0.3:
            problems.append(f"ترتيب حروف معكوس ({reversed_words}/{checked})")
    return problems


def normalize_arabic(text: str) -> str:
    """يحوّل أشكال العرض العربية (ﻫﺬﺍ) إلى الحروف الأساسية (هذا) والمسافات الخاصة إلى عادية."""
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF:
            out.append(unicodedata.normalize("NFKC", ch))
        elif ch in "   ":
            out.append(" ")
        elif ch in "‎‏‪‫‬‭‮":
            continue  # علامات اتجاه خفية
        else:
            out.append(ch)
    return "".join(out)


# ---------------- تصنيف الصفحة ----------------

def _image_coverage(page: fitz.Page) -> float:
    area = abs(page.rect)
    if area <= 0:
        return 0.0
    covered = 0.0
    for img in page.get_image_info():
        r = fitz.Rect(img["bbox"]) & page.rect
        if not r.is_empty:
            covered += abs(r)
    return min(covered / area, 1.0)


FORCED_OCR_CODE = "forced_ocr"
FORCED_OCR_TEXT = "فرض OCR (تجاهل طبقة النص)"


def classify_page(page: fitz.Page, min_chars: int, force_ocr: bool = False) -> PageInfo:
    visible = invisible = 0
    for span in page.get_texttrace():
        n = sum(1 for c in span["chars"] if c[0] > 32 and not chr(c[0]).isspace())
        # type 3 = نص غير مرئي (طبقة OCR من الماسح) ؛ opacity 0 = شفاف
        if span.get("type") == 3 or span.get("opacity", 1.0) == 0:
            invisible += n
        else:
            visible += n
    coverage = _image_coverage(page)
    info = PageInfo(PageKind.TEXT, visible, invisible, coverage)

    if visible >= min_chars:
        text = page.get_text("text")
        reversed_report = quality.detect_reversed_letters(text)
        if reversed_report.flagged:
            info.kind = PageKind.SCANNED
            info.converted_reason = quality.REASON_CODE
            info.reasons.append(reversed_report.describe())
            return info
        problems = text_layer_problems(text)
        if not problems:
            if force_ocr:  # صفحة كانت ستُستخرج من طبقة النص، والمستخدم طلب تجاهلها
                info.kind = PageKind.SCANNED
                info.converted_reason = FORCED_OCR_CODE
                info.reasons.append(FORCED_OCR_TEXT)
                return info
            if coverage > 0.5:
                info.reasons.append("صفحة نصية تحتوي صورة كبيرة")
            return info
        info.kind = PageKind.SCANNED
        info.reasons.append("طبقة النص تالفة: " + "؛ ".join(problems))
        return info

    if visible == 0 and invisible == 0 and coverage == 0 and not page.get_drawings():
        info.kind = PageKind.BLANK
        return info

    info.kind = PageKind.SCANNED
    if invisible:
        info.reasons.append("فيها طبقة OCR مخفية من الماسح (لا نعتمد عليها)")
    return info


# ---------------- الاستخراج والرسم ----------------

_MIRROR = str.maketrans("()[]{}", ")(][}{")


def fix_mirrored_brackets(line: str) -> str:
    """في سطر عربي: إذا كانت الأقواس مقلوبة ")المؤجر(" نعكسها. لا نلمس السطر إن كانت سليمة."""
    if not _ARABIC_LETTER.search(line) or not re.search(r"[()\[\]{}]", line):
        return line

    def balanced(s: str) -> bool:
        depth = 0
        for c in s:
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    return False
        return depth == 0

    if not balanced(line):
        swapped = line.translate(_MIRROR)
        if balanced(swapped):
            return swapped
    return line


def _is_mostly_arabic(text: str) -> bool:
    ar = len(_ARABIC_LETTER.findall(text))
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    return ar >= latin


def _clean(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", normalize_arabic(text)).strip()


def page_text_to_markdown(page: fitz.Page, tables: bool = True, rtl_columns: bool = True) -> str:
    """استخراج طبقة النص كفقرات Markdown، والجداول الموثوقة كجداول Markdown (tables.py).

    - الأسطر التي على نفس الارتفاع تُجمع في سطر واحد (من اليمين لليسار في النص العربي).
    - الكتل المتقاربة عمودياً تُدمج في فقرة واحدة، والمتباعدة تفصلها سطر فارغ.
    - الجدول فقرة مستقلة في موضعه العمودي، وأسطره لا تتكرر كنص عادي.
    """
    from .tables import extract_tables
    found = extract_tables(page, rtl_columns) if tables else []
    boxes = [t.bbox for t in found]

    def in_table(r: fitz.Rect) -> bool:
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        return any(b.x0 <= cx <= b.x1 and b.y0 <= cy <= b.y1 for b in boxes)

    rows: list[tuple[float, float, str, bool]] = [(t.bbox.y0, t.bbox.y1, t.markdown, True) for t in found]
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        pending: list[tuple[fitz.Rect, str]] = []
        for line in block["lines"]:
            text = "".join(s["text"] for s in line["spans"])
            if text.strip() and not in_table(fitz.Rect(line["bbox"])):
                pending.append((fitz.Rect(line["bbox"]), text))
        # تجميع الأسطر المتداخلة عمودياً
        groups: list[list[tuple[fitz.Rect, str]]] = []
        for rect, text in pending:
            for g in groups:
                gr = g[0][0]
                overlap = min(gr.y1, rect.y1) - max(gr.y0, rect.y0)
                if overlap > 0.5 * min(gr.height, rect.height):
                    g.append((rect, text))
                    break
            else:
                groups.append([(rect, text)])
        for g in groups:
            joined = " ".join(t for _, t in g)
            rtl = _is_mostly_arabic(joined)
            g.sort(key=lambda it: -it[0].x1 if rtl else it[0].x0)
            text = fix_mirrored_brackets(_clean(" ".join(t for _, t in g)))
            if text:
                rows.append((min(r.y0 for r, _ in g), max(r.y1 for r, _ in g), text, False))
    if found:
        rows.sort(key=lambda r: r[0])

    paras: list[list[str]] = []
    prev_bottom = None
    prev_height = 0.0
    prev_table = False
    for top, bottom, text, is_table in rows:
        gap = None if prev_bottom is None else top - prev_bottom
        if is_table or prev_table:
            paras.append([text])
            prev_bottom, prev_height, prev_table = bottom, bottom - top, is_table
            continue
        if paras and gap is not None and -prev_height < gap < 0.6 * prev_height:
            paras[-1].append(text)
        else:
            paras.append([text])
        prev_bottom, prev_height, prev_table = bottom, bottom - top, False
    return "\n\n".join("\n".join(p) for p in paras).strip()


def render_page_png(page: fitz.Page, dpi: int) -> bytes:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return pix.tobytes("png")
