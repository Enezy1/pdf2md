"""استخراج الوقائع بالأنماط من نص الصفحات: التواريخ، المبالغ، أرقام القضايا، الأسماء والجهات،
ونوع المستند. كل واقعة تُسجَّل مع أرقام الصفحات التي وردت فيها (للإسناد لاحقاً).

- قبل المطابقة تُوحَّد الأرقام: العربية-الهندية (٠-٩) والفارسية (۰-۹) → 0-9، والفاصلة العشرية
  العربية ٫ → . ، وفاصل الآلاف ٬ → , ، والشرطة اللينة وأشباه الشرطات → -.
  التوحيد للمطابقة فقط: الواقعة تُعرض بنصها كما ورد (raw) وبجانبها القيمة الموحدة (value).
- لا شبكة ولا نموذج: يعمل على كل الملفات.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PUNCT = str.maketrans({"­": "-", "‐": "-", "‑": "-", "‒": "-",
                        "–": "-", "−": "-", " ": " ", " ": " "})


def normalize_for_compare(text: str) -> str:
    """للمقارنة والاستخراج فقط (لا يُكتب في Markdown): الأرقام أولاً بنفس دالة الكتابة،
    ثم أشكال العرض العربية والشرطات والمسافات الخاصة — حتى لا يُحسب تحويل الأرقام تعديلاً."""
    from .digits import normalize_digits as _digits
    from .pdf_io import normalize_arabic
    return normalize_arabic(_digits(text)).translate(_PUNCT)


normalize_digits = normalize_for_compare  # اسم قديم


# ---------------------------------------------------------------- التواريخ
MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "إبريل": 4, "مايو": 5, "يونيو": 6,
    "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
    "كانون الثاني": 1, "شباط": 2, "آذار": 3, "نيسان": 4, "أيار": 5, "حزيران": 6, "تموز": 7, "آب": 8,
    "أيلول": 9, "تشرين الأول": 10, "تشرين الثاني": 11, "كانون الأول": 12,
}
_MONTH_ALT = "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True))
_SEP = r"\s*[/\-.]\s*"
_RE_DMY = re.compile(rf"(?<![\d/.\-])(\d{{1,2}}){_SEP}(\d{{1,2}}){_SEP}(\d{{4}})(?![\d/\-])")
_RE_YMD = re.compile(rf"(?<![\d/.\-])(\d{{4}}){_SEP}(\d{{1,2}}){_SEP}(\d{{1,2}})(?![\d/\-])")
_RE_TEXT = re.compile(rf"(?<!\d)(\d{{1,2}})\s*(?:من\s+)?(?:شهر\s+)?({_MONTH_ALT})\s*(?:سنة|عام|لسنة)?\s*(\d{{4}})")
_HIJRI = re.compile(r"^\s*(?:هـ|ه\b|هجري)")


def _valid(y: int, m: int, d: int) -> bool:
    return 1 <= m <= 12 and 1 <= d <= 31 and 1300 <= y <= 2200


def find_dates(text: str) -> list[tuple[str, str]]:
    """[(النص كما ورد بعد توحيد الأرقام، القيمة YYYY-MM-DD أو مع (هـ))]."""
    t = normalize_for_compare(text)
    out: list[tuple[int, str, str]] = []
    taken: list[tuple[int, int]] = []

    def add(m: re.Match, y: int, mo: int, d: int) -> None:
        if not _valid(y, mo, d) or any(a < m.end() and m.start() < b for a, b in taken):
            return
        hijri = bool(_HIJRI.match(t[m.end():m.end() + 6])) or y < 1600
        value = f"{y:04d}-{mo:02d}-{d:02d}" + (" هـ" if hijri else "")
        taken.append((m.start(), m.end()))
        out.append((m.start(), m.group(0).strip(), value))

    for m in _RE_TEXT.finditer(t):
        add(m, int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    for m in _RE_YMD.finditer(t):
        add(m, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in _RE_DMY.finditer(t):
        add(m, int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return [(raw, val) for _, raw, val in sorted(out)]


# ---------------------------------------------------------------- المبالغ (دينار كويتي)
_NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_KWD_AFTER = r"(?:د\s*\.\s*ك|دينارا?\s*كويتيا?|دنانير\s*كويتية|دينارا?|دنانير|KWD|K\.?D\.?)"
_KWD_BEFORE = r"(?:KWD|K\.?D\.?|د\s*\.\s*ك)"
_FILS = r"(?:\s*و\s*(\d{1,3})\s*فلسا?|\s*و\s*(\d{1,3})\s*فلوس)?"
_RE_AMOUNT_AFTER = re.compile(rf"(?<![\d.,])({_NUM})\s*(?:\(\s*[^()\d]{{0,40}}\s*\)\s*)?{_KWD_AFTER}(?![A-Za-z]){_FILS}")
_RE_AMOUNT_BEFORE = re.compile(rf"{_KWD_BEFORE}\s*({_NUM})(?![\d])")
# "مبلغ وقدره 5,000" بلا عملة صريحة: لا يُعتبر مبلغاً بالدينار (قد يكون عملة أخرى) — لا نخمّن.


def _to_number(s: str) -> float | None:
    s = re.sub(r"[,\s]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _fmt(v: float) -> str:
    return f"{v:,.3f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


def find_amounts(text: str) -> list[tuple[str, str]]:
    """[(النص كما ورد، القيمة الموحدة "1,250.5 د.ك")]."""
    t = normalize_for_compare(text)
    out: list[tuple[int, str, str]] = []
    taken: list[tuple[int, int]] = []
    for rx in (_RE_AMOUNT_AFTER, _RE_AMOUNT_BEFORE):
        for m in rx.finditer(t):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            v = _to_number(m.group(1))
            if v is None:
                continue
            fils = next((g for g in m.groups()[1:] if g), None) if rx is _RE_AMOUNT_AFTER else None
            if fils:
                v += int(fils) / 1000
            taken.append((m.start(), m.end()))
            out.append((m.start(), m.group(0).strip(), f"{_fmt(v)} د.ك"))
    return [(raw, val) for _, raw, val in sorted(out)]


# ---------------------------------------------------------------- أرقام القضايا
_CASE_KW = r"(?:القضية|قضية|الدعوى|دعوى|الطعن|طعن|الاستئناف|استئناف|التمييز|الطلب|الأمر|أمر\s+الأداء|الجنحة|جنحة|الجناية|جناية)"
_RE_CASE_SLASH = re.compile(
    rf"{_CASE_KW}(?:\s+(?:بالتمييز|بالاستئناف|المقيدة|المقيد|المرفوعة))?\s*(?:رقم|رقمي|برقم|بالرقم)?\s*[:\-]?\s*"
    r"\(?\s*(\d{1,7}\s*/\s*\d{2,4}(?:\s*(?:[ء-ي]+(?:\s+[ء-ي]+){0,2})\s*(?:/\s*\d{1,3})?)?)")
_RE_CASE_YEAR = re.compile(rf"{_CASE_KW}[^\n.،]{{0,25}}?رقم\s*(\d{{1,7}})\s*(?:لسنة|سنة|لعام)\s*(\d{{4}})")
_CASE_TAIL_STOP = {"بتاريخ", "المؤرخ", "المؤرخة", "الصادر", "الصادرة", "في", "من", "ضد", "و", "والمقامة", "لسنة"}


def find_case_numbers(text: str) -> list[tuple[str, str]]:
    t = normalize_for_compare(text)
    out: list[tuple[int, str, str]] = []
    for m in _RE_CASE_YEAR.finditer(t):
        out.append((m.start(), m.group(0).strip(), f"{m.group(1)}/{m.group(2)}"))
    for m in _RE_CASE_SLASH.finditer(t):
        val = re.sub(r"\s+", " ", m.group(1)).strip()
        words = val.split(" ")
        # إزالة كلمات ليست من وصف القضية (بتاريخ...)
        keep = []
        for w in words:
            if w in _CASE_TAIL_STOP:
                break
            keep.append(w)
        val = re.sub(r"\s*/\s*", "/", " ".join(keep))
        out.append((m.start(), m.group(0).strip(), val))
    seen, res = set(), []
    for _, raw, val in sorted(out):
        if val not in seen:
            seen.add(val)
            res.append((raw, val))
    return res


# ---------------------------------------------------------------- الأسماء والجهات
_STOP = {"ضد", "بصفته", "بصفتها", "بصفتهم", "وذلك", "الذي", "التي", "في", "على", "عن", "من", "إلى", "الى", "حيث",
         "بتاريخ", "المقيم", "المقيمة", "ويعلن", "يعلن", "تعلن", "وموطنه", "ومحله", "الكائن", "رقم", "بموجب",
         "والمدعى", "والمدعي", "الطرف", "للطرف", "والطرف", "بالطرف", "و", "قد", "أن", "ان", "هو", "هي", "وهو", "وهي", "مع", "لدى", "عنه", "عنها", "بشخصه"}
_ROLES = {
    "party": r"(?:المدعى\s+عليهم?|المدعى\s+عليها|المدعية|المدعي|المستأنف\s+ضده|المستأنف\s+ضدها|المستأنفة|المستأنف|"
             r"الطاعنة|الطاعن|المطعون\s+ضده|المطعون\s+ضدها|المؤجر|المستأجرة|المستأجر|الطرف\s+الأول|الطرف\s+الثاني|"
             r"المتهمة|المتهم|المجني\s+عليه|الشاكي|المشكو\s+في\s+حقه|السيدة|السيد)",
    "lawyer": r"(?:المحامية|المحامي|الأستاذة|الأستاذ|الاستاذ|وكيلها|وكيله|الوكيل\s+عن)",
}
_ENTITY = re.compile(
    r"((?:وزارة|شركة|بنك|بيت\s+التمويل|محكمة|المحكمة|هيئة|الهيئة|مؤسسة|المؤسسة|بلدية|إدارة|الإدارة|النيابة|"
    r"جامعة|مجلس|ديوان|مكتب)\s+[ء-ي][ء-ي ]{1,60})")
_NAME_WORD = re.compile(r"^[ء-ي]{2,}$")
_HEADS = {"وزارة", "شركة", "بنك", "محكمة", "المحكمة", "هيئة", "الهيئة", "مؤسسة", "المؤسسة", "بلدية", "إدارة",
          "الإدارة", "النيابة", "جامعة", "مجلس", "ديوان", "مكتب", "المدعي", "المدعى", "المحامي", "السيد", "السيدة"}


def _take_words(s: str, max_words: int) -> str:
    out = []
    for w in re.split(r"\s+", s.strip()):
        if out and w[:1] in "(«[\"":   # صفة بين قوسين بعد الاسم: "أحمد علي (المؤجر)"
            break
        ends = w[-1:] in "،,.:؛;)»]"
        w = w.strip("،,.:؛;()[]\"'«»/")
        if not w or not _NAME_WORD.match(w) or w in _STOP or (out and w[0] == "و" and w[1:] in _HEADS):
            break
        out.append(w)
        if len(out) >= max_words or ends:
            break
    return " ".join(out)


def find_names(text: str) -> list[tuple[str, str, str]]:
    """[(الدور: party|lawyer|entity، الاسم، النص كما ورد)]. مرشحات بالأنماط، تحتاج نظرة إنسان."""
    from .pdf_io import normalize_arabic
    t = re.sub(r"[ً-ٰٟـ]", "", normalize_arabic(text))
    out: list[tuple[int, str, str, str]] = []
    for role, rx in _ROLES.items():
        # الاسم في lookahead حتى لا يبتلع التطابقُ دوراً يليه ("المدعي: … ضد المدعى عليه/ …")
        for m in re.finditer(r"(?<![ء-ي])" + rx + r"\s*[:/\-]?\s*(?:/\s*)?(?:السيد(?:ة)?\s*[/:]?\s*)?(?=([ء-ي][^\n]{0,80}))", t):
            name = _take_words(m.group(1), 4)
            if len(name.split()) >= 2 or (role == "lawyer" and name):
                out.append((m.start(), role, name, (m.group(0) + m.group(1))[:60].strip()))
    for m in _ENTITY.finditer(t):
        words = m.group(1).split()
        name = words[0] + (" " + _take_words(" ".join(words[1:]), 3) if len(words) > 1 else "")
        if len(name.split()) >= 2:
            out.append((m.start(), "entity", name.strip(), m.group(0)[:60].strip()))
    return [(role, name, raw) for _, role, name, raw in sorted(out)]


# ---------------------------------------------------------------- نوع المستند
DOC_TYPES = [
    ("حكم", [(r"حكمت\s+المحكمة", 4), (r"أصدرت\s+(?:المحكمة\s+)?الحكم", 3), (r"باسم\s+صاحب\s+السمو", 3),
             (r"منطوق\s+الحكم", 3), (r"الأسباب", 1), (r"بعد\s+الاطلاع\s+على\s+الأوراق", 2), (r"فلهذه\s+الأسباب", 2)]),
    ("صحيفة دعوى", [(r"صحيفة\s+(?:افتتاح\s+)?(?:الدعوى|دعوى)", 5), (r"يلتمس\s+الطالب", 2), (r"بناء\s+عليه", 1),
                    (r"وكلفته\s+بالحضور", 3), (r"أنا\s+مندوب\s+الإعلان", 3)]),
    ("صحيفة استئناف", [(r"صحيفة\s+(?:ال)?استئناف", 6), (r"المستأنف\s+ضده", 2)]),
    ("طعن بالتمييز", [(r"(?:صحيفة\s+)?الطعن\s+بالتمييز", 6), (r"المطعون\s+ضده", 2)]),
    ("مذكرة", [(r"مذكرة\s+(?:بدفاع|بالدفاع|دفاع|ختامية|شارحة|بأقوال)", 6), (r"^\s*#*\s*مذكرة", 4), (r"مذكرة", 1)]),
    ("تقرير", [(r"تقرير\s+(?:الخبير|الخبرة|طبي|فني)", 6), (r"^\s*#*\s*تقرير", 4), (r"إدارة\s+الخبراء", 3),
               (r"المأمورية", 2), (r"تقرير", 1)]),
    ("عقد", [(r"^\s*#*\s*عقد", 5), (r"عقد\s+(?:إيجار|بيع|عمل|مقاولة|اتفاق|شراكة)", 5), (r"الطرف\s+الأول", 2),
             (r"الطرف\s+الثاني", 2), (r"البند\s+(?:الأول|الثاني)", 2)]),
    ("إنذار", [(r"إنذار\s+(?:رسمي|بالدفع|على\s+يد)", 6), (r"^\s*#*\s*إنذار", 4)]),
    ("توكيل", [(r"توكيل\s+(?:رسمي|خاص|عام)", 6), (r"وكالة\s+(?:عامة|خاصة)", 5)]),
    ("محضر", [(r"^\s*#*\s*محضر", 5), (r"محضر\s+(?:جلسة|ضبط|اجتماع)", 5)]),
    ("كتاب رسمي", [(r"تحية\s+طيبة\s+وبعد", 3), (r"المحترم", 1), (r"الموضوع\s*:", 2)]),
]


def detect_doc_type(pages: list[tuple[int, str]]) -> tuple[str, dict[str, int]]:
    """يعيد (النوع أو "غير محدد"، الدرجات). الصفحتان الأوليان أثقل وزناً."""
    scores: dict[str, int] = {}
    for i, (_n, text) in enumerate(pages):
        weight = 3 if i == 0 else 2 if i == 1 else 1
        t = re.sub(r"[ً-ٰٟـ]", "", text)
        for name, pats in DOC_TYPES:
            for pat, w in pats:
                hits = len(re.findall(pat, t, re.M))
                if hits:
                    scores[name] = scores.get(name, 0) + min(hits, 3) * w * weight
    if not scores:
        return "غير محدد", {}
    best = max(scores.items(), key=lambda kv: kv[1])
    return (best[0] if best[1] >= 6 else "غير محدد"), scores


# ---------------------------------------------------------------- التجميع لكل ملف
@dataclass
class Fact:
    kind: str           # date | amount | case | party | lawyer | entity
    value: str          # القيمة الموحدة (مفتاح التجميع)
    raw: str            # أول صيغة وردت بها
    pages: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "raw": self.raw, "pages": self.pages}


def clean_for_facts(markdown: str) -> str:
    """نص الصفحة بلا تعليقات وبلا رموز Markdown الشكلية (خلايا الجداول تُفصل بمسافات)."""
    t = re.sub(r"<!--.*?-->", " ", markdown or "", flags=re.S)
    t = re.sub(r"^\s*\|?\s*:?-{3,}.*$", " ", t, flags=re.M)
    t = t.replace("|", " ; ")
    t = re.sub(r"[*_#>`]+", " ", t)
    return t


_KWD_HEADER = re.compile(rf"{_KWD_AFTER}|{_KWD_BEFORE}|بالدينار")
_CELL_NUM = re.compile(rf"^\s*({_NUM})\s*$")


def table_amounts(markdown: str) -> list[tuple[str, str]]:
    """مبالغ في جداول عمودُها معنون بالدينار ("المبلغ (د.ك)"): الخلية رقم بلا عملة."""
    from .tables import split_row
    out: list[tuple[str, str]] = []
    header: list[str] | None = None
    cols: set[int] = set()
    for line in (markdown or "").splitlines() + [""]:
        if not line.strip().startswith("|"):
            header, cols = None, set()
            continue
        cells = split_row(line)
        if header is None:
            header = cells
            cols = {i for i, c in enumerate(cells) if _KWD_HEADER.search(normalize_for_compare(c))}
            continue
        if all(re.fullmatch(r":?-{3,}:?", c.strip()) for c in cells if c.strip()):
            continue
        for i in cols:
            if i < len(cells):
                m = _CELL_NUM.match(normalize_for_compare(cells[i]))
                v = _to_number(m.group(1)) if m else None
                if v is not None:
                    out.append((cells[i].strip(), f"{_fmt(v)} د.ك"))
    return out


def extract_facts(pages: list[tuple[int, str]]) -> list[Fact]:
    facts: dict[tuple[str, str], Fact] = {}

    def add(kind: str, value: str, raw: str, n: int) -> None:
        f = facts.setdefault((kind, value), Fact(kind, value, re.sub(r"\s+", " ", raw).strip()))
        if n not in f.pages:
            f.pages.append(n)

    for n, md in pages:
        for raw, val in table_amounts(md):
            add("amount", val, raw, n)
        text = clean_for_facts(md)
        for raw, val in find_dates(text):
            add("date", val, raw, n)
        for raw, val in find_amounts(text):
            add("amount", val, raw, n)
        for raw, val in find_case_numbers(text):
            add("case", val, raw, n)
        for role, name, raw in find_names(text):
            add(role, name, raw, n)
    return list(facts.values())
