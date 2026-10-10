"""تنظيف ما ليس من متن المستند قبل كتابة الـ Markdown: ذيل الصفحة المتكرر (عنوان المكتب)،
وأرقام الصفحات، وبقايا التوقيعات والأختام (أسطر قصيرة بلا معنى).

يُطبَّق عند تجميع الملف فقط؛ أجزاء الصفحات والنص الخام على القرص لا تتغير.
"""

from __future__ import annotations

import difflib
import re

TAIL_LINES = 8                    # يُبحث عن الذيل في آخر 8 أسطر من كل صفحة (ذيل مكتب: عنوان، هاتف، بريد، موقع، رقم الصفحة، الشركة، رأس المال)
SIMILAR = 0.55                    # تشابه سطرين من صفحتين مختلفتين (OCR يشوّه الذيل بشكل مختلف كل مرة)
MIN_SIMILAR_LEN = 25              # أسطر أقصر (اسم المحامي تحت التوقيع مثلاً) لا تُحذف بالتشابه وحده
_ADDRESS = re.compile(
    r"Post\s*Code|P\.?\s*[O0D]\.?\s*B[o0]?|Tel\.|Fax|الرمز\s*البريدي|ص\s*\.?\s*ب\s*[:.]|هاتف\s*:|فاكس|"
    r"www\.|@\w+\.\w+|دور\s*\d{1,3}.*\d{4,}", re.I)
_PAGE_NO = re.compile(r"^[\s\-–—_.()]*\d{0,3}[\s\-–—_.()]*$")
# رقم هاتف وحده في ذيل الصفحة ("+965 90000001")
_PHONE = re.compile(r"^\s*\+?[\d\s\-()]{7,}\s*$")
# "الصفحة 3 من 11" / "صفحة 3 من 11" / "Page 3 of 11"
_PAGE_OF = re.compile(r"^\s*(?:ال)?صفحة\s*\d{1,3}\s*من\s*\d{1,3}\s*$|^\s*page\s*\d{1,3}\s*(?:of|/)\s*\d{1,3}\s*$", re.I)
_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")


def _visible(line: str) -> str:
    return re.sub(r"[\s\-–—_.|*#()]+", "", line)


_SHORT_WORDS = {"ضد", "و", "أو", "او", "ثم", "لا", "نعم", "بين", "مع", "عن", "من", "في", "إلى", "الى"}


def is_noise(line: str) -> bool:
    """سطر من حرف أو حرفين بلا معنى (بقايا توقيع أو ختم)، أو رموز فقط مثل "- -"."""
    if _COMMENT.match(line) or line.lstrip().startswith("|"):
        return False
    v = _visible(line)
    if v == "":
        return line.strip() not in ("", "---")
    return len(v) <= 2 and not v.isdigit() and v not in _SHORT_WORDS


def _tail(lines: list[str]) -> list[int]:
    idx = [i for i, ln in enumerate(lines) if ln.strip() and not _COMMENT.match(ln)]
    return idx[-TAIL_LINES:]


def strip_page_furniture(pages: dict[int, str]) -> dict[int, str]:
    """{رقم الصفحة: النص} ← نفس الصفحات بلا ذيل متكرر ولا أرقام صفحات ولا بقايا توقيع."""
    split = {n: (t or "").splitlines() for n, t in pages.items()}
    tails = {n: [split[n][i] for i in _tail(split[n])] for n in split}
    out: dict[int, str] = {}
    allt = "".join(pages.values())
    arabic_doc = len(re.findall(r"[ء-ي]", allt)) > len(re.findall(r"[A-Za-z]", allt))
    # ذيل حقيقي يتكرر في أغلب الصفحات؛ تشابه مع صفحة واحدة فقط قد يكون غلافاً مكرراً
    # (مذكرة فيها صفحتا غلاف متطابقتان: أطراف الدعوى ورقمها ليست ذيلاً)
    need = 1 if len(split) <= 2 else 2
    for n, lines in split.items():
        others = [ln for m, t in tails.items() if m != n for ln in t]
        other_tails = [t for m, t in tails.items() if m != n]
        drop: set[int] = set()
        seen_hard = False
        # الذيل: من الأسفل للأعلى ما دام السطر عنواناً أو يشبه ذيل صفحة أخرى
        for i in reversed(_tail(lines)):
            ln = lines[i]
            if lines[i].lstrip().startswith("|"):
                break
            st = ln.strip()
            similar = len(st) >= MIN_SIMILAR_LEN and sum(
                any(difflib.SequenceMatcher(None, ln, o).ratio() >= SIMILAR for o in t) for t in other_tails) >= need
            # بداية عنوان مقطوعة ("المرقاب")
            similar = similar or (len(st) >= 4 and sum(o.strip().startswith(st) and len(o.strip()) >= MIN_SIMILAR_LEN
                                                       for o in others) >= 2)
            # سطر لاتيني في ذيل مستند عربي = غالباً العنوان بالإنجليزية
            latin = len(re.findall(r"[A-Za-z]", st)) > 2 * len(re.findall(r"[ء-ي]", st)) and arabic_doc
            hard = bool(_ADDRESS.search(ln) or _PAGE_NO.match(ln) or _PAGE_OF.match(ln) or _PHONE.match(ln))
            if hard or latin or is_noise(ln):
                seen_hard = seen_hard or hard
                drop.add(i)
            elif similar and not seen_hard:
                # سطر يشبه ذيول الصفحات الأخرى يُحذف فقط تحت العنوان/الهاتف/رقم الصفحة أو في ذيل بلا عنوان؛
                # فوق العنوان قد يكون سطر متن يتكرر في الصفحات ("مرفق رقم (4) من حافظة مستنداتنا…")
                drop.add(i)
            else:
                break
        # رقم الصفحة في أول سطر ("- 3 -")
        head = [i for i, ln in enumerate(lines) if ln.strip() and not _COMMENT.match(ln)][:1]
        for i in head:
            if _PAGE_NO.match(lines[i]) or re.fullmatch(r"\s*-\s*\d{1,3}\s*-?\s*", lines[i]):
                drop.add(i)
        kept = [ln for i, ln in enumerate(lines) if i not in drop and not is_noise(ln)]
        out[n] = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return out
