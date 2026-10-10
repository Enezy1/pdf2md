"""توجيه الصفحة المصوّرة: OCR الداخلي (PaddleOCR) أولاً، ثم يُقرَّر لكل صفحة وحدها هل تحتاج طرفاً خارجياً.

الأدلة (أحكام حقيقية من مجلد judgments، 2026-10-10: 99 صفحة ممسوحة بدقة 200-300 نقطة، عليها ختم مائل):
  - النص بعد تقويم الميل جيد (الكلمات صحيحة في الغالب).
  - الأرقام غير موثوقة بأي دقة: 11 من 38 رقماً (تواريخ، أرقام قضايا، سنوات) صحيحة حرفياً، والخطأ غالباً
    "معقول" (2024/3290 بدل 2024/3295) فلا يكشفه فحص المعقولية، وثقة النموذج في السطر الخاطئ عالية.
  إذن: الرقم من OCR الداخلي "غير متحقق منه" حتى يؤكده مصدر آخر.

القرار لكل صفحة (route):
  local   النص سليم ولا أرقام مهمة: يبقى محلياً، لا يُرسل شيء.
  verify  النص سليم وفيه أرقام: OCR خارجي (OCR.space) يقرأ الصفحة نفسها، فإذا تطابقت الأرقام تبقى الصفحة كما
          هي دون Gemini؛ وإلا (أو إن لم يوجد OCR خارجي) تذهب هذه الصفحة وحدها لنموذج الرؤية.
  reread  النص نفسه ضعيف: الصفحة تُقرأ من جديد عبر المواقع (السلسلة المعتادة).
  vision  جدول بشبكة خطوط أو صفحة مدوّرة: نموذج الرؤية يقرأ الصفحة كاملة (OCR يبعثرهما).
بلا مزوّد خارجي: النص المحلي يبقى، وما يُشك فيه يُذكر للمستخدم صراحة ليتحقق بنفسه.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .digits import normalize_digits

LOCAL, VERIFY, REREAD, VISION = "local", "verify", "reread", "vision"

# نص ضعيف (قيست على 107 صفحة حقيقية بعد تقويم الميل): انظر text_weak
WEAK_NOISE = 0.10
WEAK_MEAN_CONF = 0.55
POOR_SCAN_DPI = 150        # نفس paddle_engine.POOR_SCAN_DPI (قرار المستخدم): مسح كهذا + أرقام ⇒ إعادة قراءة

_NUM = re.compile(r"\d+(?:\s?[/\-–.]\s?\d+)*")
_NO_DIGIT = re.compile(r"\D")

LABELS = {
    "table": "جدول",
    "rotated": "صفحة مدوّرة",
    "weak_text": "نص ضعيف القراءة",
    "poor_scan_digits": "مسح رديء وفيه أرقام",
    "numbers": "أرقام تحتاج تأكيداً",
    "implausible_numbers": "أرقام غير معقولة",
}


@dataclass
class Route:
    kind: str                               # local | verify | reread | vision
    reasons: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)       # أرقام الصفحة (خانتان فأكثر) كما قرأها Paddle
    suspicious: list[str] = field(default_factory=list)    # أرقام غير معقولة (تاريخ مستحيل، سنة ناقصة…)

    def to_meta(self) -> dict:
        return {"route": self.kind, "reasons": self.reasons, "numbers": self.numbers[:40],
                "suspicious": self.suspicious[:20]}

    def label(self) -> str:
        return "، ".join(LABELS.get(r, r) for r in self.reasons)


# ------------------------------------------------------------ الصورة قبل القراءة

def image_trouble(png: bytes) -> str:
    """"table" أو "rotated" أو "" — من صورة الصفحة نفسها، قبل أي OCR."""
    from . import layout
    if layout.is_sideways(png):
        return "rotated"
    if table_inside(png):
        return "table"
    return ""


TABLE_INNER_V = 2          # خطوط عمودية داخلية (بعيدة عن الحواف) على الأقل
EDGE = 0.14               # خط عند 0.87 من العرض في حكم إحالة كان إطاراً لا عموداً


def table_inside(png: bytes) -> bool:
    """جدول حقيقي: خطوط أفقية طويلة + خطوط أعمدة **داخلية**. layout.has_table وحده خُدع في الأحكام بإطار الصفحة
    (خطوط الحواف فقط: 0.06/0.10 و0.90/0.94 من العرض) وبخط ترويسة واحد في الوسط — 13 صفحة نصية من 99.
    الجداول الحقيقية فيها 4 خطوط داخلية فأكثر (صحيفة دعوى ص5، تقرير خبير ص8)."""
    import io

    import numpy as np
    from PIL import Image, ImageFilter

    from . import layout
    try:
        g = Image.open(io.BytesIO(png)).convert("L")
        g.thumbnail((1000, 1000))
        a = np.array(g.filter(ImageFilter.MinFilter(3))) < 170
        h_lines = layout._count(layout._runs(a, int(a.shape[1] * 0.4)))
        cols = layout._runs(a.T, int(a.shape[0] * 0.1))
        w = a.shape[1]
        starts = [x for x in np.flatnonzero(cols) if x == 0 or not cols[x - 1]]
        inner = [x for x in starts if EDGE * w <= x <= (1 - EDGE) * w]
    except Exception:
        return False
    return h_lines >= layout.TABLE_H_LINES and len(inner) >= TABLE_INNER_V


# ------------------------------------------------------------ الأرقام

def numbers(text: str) -> list[str]:
    """الأرقام المهمة (خانتان فأكثر: تواريخ، أرقام قضايا، مبالغ، سنوات) بالترتيب ودون تكرار، بأرقام إنجليزية."""
    out: list[str] = []
    for m in _NUM.finditer(normalize_digits(text or "")):
        tok = re.sub(r"\s", "", m.group(0)).strip(".")
        if len(_NO_DIGIT.sub("", tok)) >= 2 and tok not in out:
            out.append(tok)
    return out


def _valid_date(parts: list[int]) -> bool:
    """تاريخ من ثلاثة أجزاء بأي ترتيب شائع (ي/ش/س أو س/ش/ي)."""
    if len(parts) != 3:
        return True
    if parts[0] > 31:
        y, mo, d = parts
    else:
        d, mo, y = parts
    return 1 <= d <= 31 and 1 <= mo <= 12 and (1300 <= y <= 1500 or 1900 <= y <= 2100)


def implausible(tok: str) -> bool:
    """رقم معطوب بوضوح: تاريخ مستحيل، أو سنة من خانتين/ثلاث داخل تاريخ (الصفر الضائع: 2025 ← 25)."""
    parts = [p for p in re.split(r"[/\-–]", tok) if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        if not _valid_date(nums):
            return True
        return not any(len(p) == 4 for p in parts)
    return False


def _digits_set(text: str) -> set[str]:
    return {_NO_DIGIT.sub("", t) for t in numbers(text)}


def compare_numbers(local_text: str, other_text: str) -> list[str]:
    """أرقام الصفحة المحلية التي لا تطابق القراءة الأخرى، والعكس (رقم أسقطه Paddle كلياً).
    المقارنة بالخانات وحدها: "/" و"-" تُقرأ بأشكال مختلفة بين المحرّكات. [] = تطابق كامل."""
    mine, theirs = _digits_set(local_text), _digits_set(other_text)
    out = [t for t in numbers(local_text) if _NO_DIGIT.sub("", t) not in theirs]
    out += [t for t in numbers(other_text) if _NO_DIGIT.sub("", t) not in mine]
    seen: list[str] = []
    for t in out:
        if t not in seen:
            seen.append(t)
    return seen


# ------------------------------------------------------------ أشرطة الأرقام
# عند اختلاف أرقام الصفحة بين OCR الداخلي والخارجي: بدل صورة الصفحة كاملة لنموذج الرؤية، صورة واحدة صغيرة فيها
# أسطر الأرقام المختلف عليها فقط (~2-3% من مساحة الصفحة)، ويُطلب منه الأرقام وحدها JSON.

STRIP_PROMPT = (
    "الصورة فيها أشرطة مرقّمة (#1، #2 …)، كل شريط سطر أو جزء سطر من مستند عربي ممسوح ضوئياً.\n"
    "لكل شريط: اكتب كل الأرقام الظاهرة فيه حرفياً كما هي مطبوعة (تواريخ، أرقام قضايا، مبالغ، سنوات، أرقام مفردة)،\n"
    "بترتيب قراءتها في السطر العربي (من اليمين إلى اليسار)، بأرقام إنجليزية 0-9، مع الفواصل كما هي (/ - . ,).\n"
    "الصفر الهندي يظهر نقطة صغيرة (٠). لا تخمّن: رقم غير واضح اكتبه كما تراه وأضف ؟ بعده.\n"
    'أعد JSON فقط بلا أي شرح: {"1": ["2025/13880", "44"], "2": []}'
)
STRIP_MAX = 25            # أقصى عدد أشرطة في الطلب الواحد


def compose_strips(items: list[tuple[int, bytes]]) -> bytes:
    """[(الرقم، صورة الشريط)] ← صورة JPEG واحدة: الأشرطة تحت بعض، ورقم كل شريط على يساره."""
    import io

    from PIL import Image, ImageDraw
    ims = [(lab, Image.open(io.BytesIO(png)).convert("L")) for lab, png in items]
    label_w, gap = 64, 14
    width = min(2000, label_w + max(im.width for _l, im in ims))
    rows = []
    for lab, im in ims:
        if im.width > width - label_w:
            im = im.resize((width - label_w, max(1, int(im.height * (width - label_w) / im.width))))
        rows.append((lab, im))
    out = Image.new("L", (width, sum(im.height + gap for _l, im in rows) + gap), 255)
    d = ImageDraw.Draw(out)
    y = gap
    for lab, im in rows:
        d.text((6, y + im.height // 2 - 6), f"#{lab}", fill=0)
        out.paste(im, (label_w, y))
        d.line((0, y + im.height + gap // 2, width, y + im.height + gap // 2), fill=200)
        y += im.height + gap
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def parse_strip_reply(text: str) -> dict[int, list[str]] | None:
    """{"1": [...]} ← {1: [...]}. None إن لم يكن JSON صالحاً."""
    import json
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    out = {}
    for k, v in data.items():
        if str(k).strip("#").isdigit() and isinstance(v, list):
            out[int(str(k).strip("#"))] = [normalize_digits(str(x)).strip() for x in v]
    return out


_ANY_NUM = re.compile(r"\d+(?:[/\-–.,]\d+)*")


def apply_numbers(line: str, new: list[str]) -> tuple[str, list[str]]:
    """يستبدل أرقام السطر بترتيبها بقراءة نموذج الرؤية. (السطر الجديد، الأرقام غير المؤكدة).
    إن اختلف العدد لا يُستبدل شيء (لا محاذاة آمنة) وتُعاد كل أرقام النموذج للمراجعة.
    رقم عليه "؟" من النموذج لا يُستبدل ويُعلَّم."""
    old = list(_ANY_NUM.finditer(normalize_digits(line)))
    clean = [re.sub(r"\s", "", x.rstrip("؟?")) for x in new]
    if len(old) != len(clean):
        return line, [x for x in clean if len(_NO_DIGIT.sub("", x)) >= 2]
    out, pos, doubt = [], 0, []
    src = normalize_digits(line)
    for m, x, raw in zip(old, clean, new):
        out.append(src[pos:m.start()])
        if raw.endswith(("؟", "?")) or not _ANY_NUM.fullmatch(x):
            out.append(m.group(0))
            doubt.append(x or m.group(0))
        else:
            out.append(x)
        pos = m.end()
    out.append(src[pos:])
    return "".join(out), doubt


def remaining_doubts(final: str, paddle: str, ocr: str) -> list[str]:
    """بعد قراءة الأشرطة: الرقم مقبول إن اتفقت عليه قراءتان من ثلاث (الداخلي، الخارجي، نموذج الرؤية).
    يبقى مشكوكاً فيه: رقم انفرد به نموذج الرؤية، أو رقم في OCR الخارجي لا يوجد في النص النهائي ولا في الداخلي."""
    fin, pad, oc = _digits_set(final), _digits_set(paddle), _digits_set(ocr)
    out = [t for t in numbers(final) if _NO_DIGIT.sub("", t) not in pad | oc]
    out += [t for t in numbers(ocr) if _NO_DIGIT.sub("", t) not in fin | pad]
    return list(dict.fromkeys(out))


def disputed_lines(fragment: str, strip_keys: list[int], diff: list[str]) -> list[int]:
    """أسطر الأرقام التي فيها رقم مختلف عليه. إن لم يظهر أي رقم مختلف عليه في سطر محلي (كلها أرقام أسقطها
    Paddle كلياً) فلا يُعرف موضعها: تُرسل كل أسطر الأرقام."""
    lines = fragment.split("\n\n")
    want = {_NO_DIGIT.sub("", t) for t in diff}
    keys = []
    for k in strip_keys:
        if k >= len(lines):
            continue
        mine = {_NO_DIGIT.sub("", t) for t in numbers(lines[k])}
        if mine & want:
            keys.append(k)
    if not keys:
        return [k for k in strip_keys if k < len(lines)][:STRIP_MAX]
    return keys[:STRIP_MAX]   # رقم لم يُعرف موضعه ولم يُصحَّح تعلّمه remaining_doubts


# ------------------------------------------------------------ القرار

_AR = r"ء-يً-ْـ"


def noise_ratio(text: str) -> float:
    """نسبة الكلمات التي ليست كلمة: كتابتان في كلمة واحدة، رموز فقط (= ~ *)، شظايا لاتينية من حرف أو حرفين.
    الكلمة الإنجليزية الكاملة (ترويسة "State Of Kuwait") والحرف العربي المفرد (ش.م.ك.م) ليسا ضجيجاً."""
    toks = (text or "").split()
    if not toks:
        return 1.0
    bad = 0
    for t in toks:
        core = t.strip(".,،:؛()[]/-–«»\"'")
        if not core:
            continue
        ar, lat = re.search(f"[{_AR}]", core), re.search("[A-Za-z]", core)
        if ar and lat:
            bad += 1
        elif not ar and not lat and not re.search(r"[\d٠-٩]", core):
            bad += 1
        elif lat and not ar and len(re.sub("[^A-Za-z]", "", core)) <= 2 and core.lower() not in ("of", "no"):
            bad += 1
    return bad / len(toks)


def text_weak(text: str, confs: list[float]) -> bool:
    """ثقة الأسطر وحدها لا تصلح على هذه المسوحات: الختم المائل يخفضها والكلمات صحيحة (حكم إحالة: 59% من الأسطر
    تحت 0.80 والكلمات 97% صحيحة). المعتمد: ضجيج النص، ومتوسط ثقة منهار. على 107 صفحة: 11 ضعيفة، كلها فعلاً كذلك
    (إيصالات طابعة نقطية، نماذج بخط اليد، صفحة حروف مبعثرة)."""
    if not confs:
        return True
    return noise_ratio(text) >= WEAK_NOISE or sum(confs) / len(confs) < WEAK_MEAN_CONF


def decide(text: str, confs: list[float], trouble: str = "", dpi: float | None = None) -> Route:
    """المسار المطلوب للصفحة لو توفّر كل شيء. ما يُفعل فعلاً حسب المتاح يقرره المستدعي (pipeline/dispatcher)."""
    nums = numbers(text)
    bad = [t for t in nums if implausible(t)]
    weak = text_weak(text, confs)
    # "مدوّرة" تُصدَّق فقط إن فشل Paddle فعلاً: كاشف الدوران خُدع بصفحة نصها قليل أعلاها (حكم، ص2) قرأها Paddle جيداً
    if trouble == "table" or (trouble == "rotated" and weak):
        return Route(VISION, [trouble], nums, bad)
    if weak:
        return Route(REREAD, ["weak_text"], nums, bad)
    if nums and dpi is not None and dpi < POOR_SCAN_DPI:
        return Route(REREAD, ["poor_scan_digits"], nums, bad)
    if nums:
        return Route(VERIFY, ["implausible_numbers" if bad else "numbers"], nums, bad)
    return Route(LOCAL, [], nums, bad)
