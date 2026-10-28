"""كاشف "ترتيب الحروف المعكوس" في طبقة النص العربية.

المشكلة: بعض الخطوط تحوي حروفاً مركّبة (ligatures) مثل لام+ألف، فيُستخرج الزوج بترتيبه
البصري المعكوس: "المهمات" → "املهمات"، "الإدارة" → "اإلدارة"، "الاستخدام" → "االستخدام"،
"لمن" → "ملن". كل الحروف عربية فلا يكشفها فحص نسبة الحروف.

الكاشف يبحث عن أنماط شبه مستحيلة في العربية السليمة، وهو محافظ عمداً: الإنذار الكاذب
كلفته معالجة صفحة بـ OCR فقط، أما التفويت فيُخرج نصاً مشوّهاً إلى الـ Markdown.
لا نحاول إصلاح النص بقلب الحروف؛ الصفحة تُحوَّل إلى OCR.

حدود معروفة: "لا" المعكوسة تصير "ال" وهي كلمة سليمة الشكل، فلا يكشفها هذا الكاشف.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

REASON_CODE = "reversed_letters"
REASON_TEXT = "طبقة نص تالفة (ترتيب الحروف)"

# رقم نسخة قواعد الجودة. يُخزَّن مع كل صفحة نصية؛ إذا زاد، تُعاد المعاينة للصفحات القديمة.
QUALITY_VERSION = 2

# العتبة: عدد الكلمات المشبوهة (بالتكرار) التي تُعتبر الصفحة بعدها تالفة.
MIN_SUSPECT_WORDS = 2
# صفحة قصيرة (عدد كلماتها العربية أقل من هذا) يكفي فيها كلمة مشبوهة واحدة.
SHORT_PAGE_WORDS = 30

_DIACRITICS = re.compile(r"[ً-ٰٟۖ-ۭـ]")  # تشكيل + تطويل
_ARABIC_LETTER = re.compile(r"[ء-يٱ-ۓ]")
_WORD_SPLIT = re.compile(r"[^ء-يٱ-ۓ]+")

# سوابق شائعة قبل الكلمة (و، ف، ب، ك، ل).
_PREFIX = "[وفبكل]{0,2}"

# ا + (إ|أ|آ) + ل : "الإ/الأ/الآ" معكوسة. لا تظهر في العربية السليمة (وبأي موضع).
_RE_ALEF_HAMZA_LAM = re.compile(r"ا[إأآ]ل")
# اال في أول الكلمة (بعد سوابق): "الا" معكوسة (االستخدام). لا كلمة عربية تبدأ بـ "اا".
_RE_ALEF_ALEF_LAM = re.compile(rf"^{_PREFIX}اال")
# امل في أول الكلمة: "الم" معكوسة (املهمات). نستثني السابقة "ك" و"ل" لأن "كامل" كلمة سليمة.
# كلمات الألف المجردة + مل في البداية غير موجودة ("أمل" بهمزة، "إملاء" بهمزة).
_RE_ALEF_MEEM_LAM = re.compile(r"^[وفب]{0,2}امل")
# لمن/لمدة/... معكوسة: كلمات دالّة لا وجود لها بهذا الشكل في العربية.
_RE_LAM_MEEM_WORDS = re.compile(r"^[وف]?(?:ملن|ملدة|ملثل|ملذلك)$")

_PATTERNS = (
    ("اإل/األ/اآل", _RE_ALEF_HAMZA_LAM),
    ("االـ", _RE_ALEF_ALEF_LAM),
    ("امل", _RE_ALEF_MEEM_LAM),
    ("ملن", _RE_LAM_MEEM_WORDS),
)


@dataclass
class ReversedReport:
    flagged: bool = False
    suspect_words: list[str] = field(default_factory=list)
    arabic_words: int = 0

    def describe(self) -> str:
        sample = "، ".join(dict.fromkeys(self.suspect_words[:3]))
        return f"{REASON_TEXT}: {len(self.suspect_words)}/{self.arabic_words} ({sample})"


def _prepare(text: str) -> str:
    # أشكال العرض (ﻫﺬﺍ) → الحروف الأساسية، ثم حذف التشكيل والتطويل.
    text = "".join(
        unicodedata.normalize("NFKC", c) if 0xFB50 <= ord(c) <= 0xFDFF or 0xFE70 <= ord(c) <= 0xFEFF else c
        for c in text
    )
    return _DIACRITICS.sub("", text)


def _is_suspect(word: str) -> bool:
    return any(rx.search(word) for _, rx in _PATTERNS)


def detect_reversed_letters(text: str) -> ReversedReport:
    words = [w for w in _WORD_SPLIT.split(_prepare(text)) if _ARABIC_LETTER.search(w)]
    suspects = [w for w in words if _is_suspect(w)]
    threshold = 1 if len(words) < SHORT_PAGE_WORDS else MIN_SUSPECT_WORDS
    return ReversedReport(
        flagged=len(suspects) >= threshold,
        suspect_words=suspects,
        arabic_words=len(words),
    )


# ---------------------------------------------------------------- مؤشرات الصفحة الضعيفة
WEAK_REASONS = {
    "reversed_letters": "ترتيب حروف معكوس",
    "too_short": "نص قصير بشكل مريب مقارنة بكثافة الصفحة",
    "weak_match": "تطابق ضعيف بين طبقة النص وOCR",
    "error": "تعذّرت المعالجة",
    "api_incomplete": "ناتج المزوّد ناقص (علامة صفحة مفقودة أو نص قصير)",
    # مؤشرات درجة الصفحة (page_score.py)
    "brackets": "علامات [ ] كثيرة (كلمات لم يقرأها المحرّك)",
    "unknown_words": "كلمات كثيرة غير موجودة في القاموس المحلي",
    "low_score": "درجة جودة منخفضة (مجموع المؤشرات)",
    "table_mismatch": "جدول ممتد بعدد أعمدة غير متطابق مع الصفحة السابقة",
    "paddle_low_conf": "أسطر بثقة منخفضة من PaddleOCR (معلّمة بـ [؟])",
    "poor_scan_digits": "مسح رديء (أقل من 150 نقطة) وفيه أرقام — راجع الأرقام أو استخدم «عبر المواقع»",
}

# نسبة حبر أقل من هذا = صفحة شبه فارغة (يُقبل منها نص فارغ)
INK_BLANK = 0.006
# عدد الحروف المتوقع تقريباً لكل 1% حبر؛ "مريب" إذا قلّ الناتج عن 15% من المتوقع
CHARS_PER_INK_PERCENT = 200
SHORT_FRACTION = 0.15


def visible_chars(text: str) -> int:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return sum(1 for c in text if not c.isspace() and c not in "|-*#_")


# صفحة فاصلة بين المستندات ("مستند رقم (3)" بشريط أسود كبير): نصها القصير سليم رغم كثافة الحبر.
_DIVIDER_RE = re.compile(
    r"^(?:ال)?(?:مستند|مرفق|ملحق|وثيقة|حافظة|بند)\s*(?:رقم)?\s*[(\[]?\s*[0-9٠-٩]{1,3}\s*[)\]]?$")


def is_divider_text(text: str) -> bool:
    t = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    t = re.sub(r"[#*_>|\-\s]+", " ", t).strip()
    return bool(_DIVIDER_RE.match(t))


def suspiciously_short(text: str, ink: float | None, fraction: float = SHORT_FRACTION) -> bool:
    n = visible_chars(text)
    if ink is None:
        return n == 0
    if ink < INK_BLANK or is_divider_text(text):
        return False
    expected = ink * 100 * CHARS_PER_INK_PERCENT
    return n < max(5, expected * fraction)


def weak_ocr_reason(text: str, ink: float | None) -> str | None:
    """سبب اعتبار ناتج OCR ضعيفاً، أو None."""
    if repetition_problem(text):
        return "repetition"
    if suspiciously_short(text, ink):
        return "too_short"
    if detect_reversed_letters(text).flagged:
        return "reversed_letters"
    return None


# ---------------------------------------------------------------- كاشف حلقات التكرار
# النماذج الصغيرة تقرأ أول الصفحة ثم تدخل حلقة: "م.م. م.م. …" أو "ملا ملا …" أو "٠٠٠٠…".
# أي صفحة يكتشفها هذا الفحص لا يُكتب نصها أبداً.
OCR_CHECK_VERSION = 1
REPEAT_REASON = "repetition"
WEAK_REASONS[REPEAT_REASON] = "ناتج غير موثوق (حلقة تكرار من النموذج)"

_CHAR_RUN = re.compile(r"(\S)\1{29,}")          # نفس الحرف 30 مرة متتالية
MIN_WORDS_FOR_RATIO = 60
MIN_UNIQUE_RATIO = 0.2
MAX_SAME_RUN = 8                                  # نفس الكلمة/العبارة متتالية
MAX_SHARE = 0.25
MAX_TABLE_ROWS = 30                                  # كلمة واحدة تشكل ربع النص


def repetition_problem(text: str) -> str | None:
    """يعيد وصف المشكلة إن كان النص حلقة تكرار، وإلا None."""
    body = re.sub(r"<!--.*?-->", " ", text or "", flags=re.S)
    if _CHAR_RUN.search(body.replace("-", "").replace("|", "").replace("=", "")):
        return "حرف مكرر بلا نهاية"
    # صفوف الجداول تتكرر طبيعياً: تُفحص كأسطر كاملة (30 صفاً متطابقاً متتالياً = حلقة)
    lines = body.splitlines()
    run, prev = 1, None
    for ln in (x.strip() for x in lines if x.strip().startswith("|")):
        run = run + 1 if ln == prev else 1
        prev = ln
        if run >= MAX_TABLE_ROWS:
            return "صف جدول مكرر بلا نهاية"
    body = "\n".join(x for x in lines if not x.strip().startswith("|"))
    words = [w for w in re.split(r"\s+", body) if w and not re.fullmatch(r"[-:#*_>]+", w)]
    if not words:
        return None
    # عبارة من 1 إلى 4 كلمات تتكرر متتالية
    for size in (1, 2, 3, 4):
        run = 1
        for i in range(size, len(words) - size + 1, size):
            if words[i:i + size] == words[i - size:i]:
                run += 1
                if run >= MAX_SAME_RUN:
                    return f"عبارة مكررة متتالية: {' '.join(words[i:i + size])[:30]}"
            else:
                run = 1
    if len(words) >= MIN_WORDS_FOR_RATIO:
        counts: dict[str, int] = {}
        for w in words:
            counts[w] = counts.get(w, 0) + 1
        if len(counts) / len(words) < MIN_UNIQUE_RATIO:
            return f"تنوّع الكلمات منخفض جداً ({len(counts)}/{len(words)})"
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if n / len(words) > MAX_SHARE and len(top) > 1:
            return f"كلمة واحدة تملأ النص: {top[:20]}"
    return None


# ---------------------------------------------------------------- اعتذار النموذج بدل القراءة
_REFUSAL_RE = re.compile(
    r"(i('| a)?m (sorry|unable)|i (cannot|can't|can not|am not able)|unable to (read|transcribe|process|extract)|"
    r"cannot (read|transcribe|process)|can't (read|transcribe)|as an ai|"
    r"لا (أستطيع|استطيع|يمكنني)|عذراً|عذرا|آسف|اعتذر|لم أتمكن|لا يمكن قراءة)", re.I)


def looks_like_refusal(text: str) -> bool:
    """رد قصير يعتذر عن القراءة ("لا أستطيع قراءة الصورة") لا نص الصفحة: لا يُحفظ كأنه محتوى."""
    t = (text or "").strip()
    return 0 < len(t) < 500 and bool(_REFUSAL_RE.search(t))
