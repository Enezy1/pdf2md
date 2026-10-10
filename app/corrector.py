"""مصحّح نص OCR: النموذج يقترح تعديلات (JSON) والكود يقرر ما يُطبَّق.

- الإرسال نص فقط عبر cloud_gate.send_text، أو صورة + نص عبر cloud_gate.send للصفحات الصعبة.
- يُرفض كل تعديل: يمس رقماً، أو يمس اسماً/جهة مكتشفة في النص، أو يغيّر الطول كثيراً، أو يمس
  علامات الصفحات/الجداول، أو لا يوجد نصه الأصلي في الصفحة.
- المقارنة بعد توحيد الأرقام، فتحويل ٣ إلى 3 ليس تعديلاً.
- فحص اكتمال بعد التطبيق: كل علامات الصفحات موجودة ونسبة الطول معقولة؛ وإلا تُعاد المحاولة
  مرة ثم تُترك الصفحة خاماً مع تعليمها.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .digits import DIGITS_RULE
from .facts import find_names, normalize_for_compare
from .providers.batching import MARKER_RE

MAX_EDITS = 200
LEN_RATIO = (0.85, 1.15)            # نسبة طول النص المصحّح إلى الخام
_DIGIT = re.compile(r"\d")
_ENTITY_HEADS = re.compile(r"(?:وزارة|شركة|بنك|محكمة|المحكمة|هيئة|الهيئة|مؤسسة|بلدية|إدارة|الإدارة|النيابة|"
                           r"جامعة|مجلس|ديوان|مكتب|السيد|السيدة|المحامي|المحامية|الأستاذ)")

TEXT_PROMPT = """أنت مدقق لنص عربي ناتج عن OCR لمستند قانوني. أعد JSON فقط بهذا الشكل:
{"edits": [{"original": "...", "corrected": "...", "reason": "..."}], "noise": ["..."]}
القواعد:
- صحّح فقط أخطاء OCR الواضحة في الكلمات العادية، اعتماداً على السياق.
- "original" يجب أن يكون نصاً موجوداً حرفياً في المدخل (كلمة أو كلمتان)، و"corrected" بديله.
- لا تغيّر أي رقم أو مبلغ أو تاريخ أو رقم قضية أو رقم هاتف أو اسم علم أو اسم جهة. إذا شككت في أحدها
  فاقترح وضعه كما هو بين [ ] ("corrected": "[الأصل]") ولا تخمّن.
- لا تلخّص ولا تحذف ولا تضف جملاً، ولا تلمس علامات <!-- page:N --> أو خطوط الجداول |.
- اكتب كل الأرقام بالأرقام الإنجليزية 0-9 دائماً.
- "noise": أسطر كاملة كما هي في المدخل ليست نصاً بل قراءة خاطئة لتوقيع أو ختم أو شعار (كلمة أو كلمتان
  بلا معنى في سياقها، مثل سطر تحت اسم المحامي في آخر المذكرة). لا تضع فيها أي اسم أو رقم.
- إذا لم يوجد خطأ أعد {"edits": [], "noise": []}."""

VISION_PROMPT = TEXT_PROMPT + """
مرفق صورة الصفحة: قارن نص OCR بالصورة وصحّح أخطاء القراءة بنفس القواعد (التعديلات فقط، لا النص كاملاً).
""" + DIGITS_RULE


@dataclass
class Edit:
    original: str
    corrected: str
    reason: str = ""
    applied: bool = False
    why_rejected: str = ""
    kind: str = "edit"               # "noise" = حذف سطر توقيع/ختم كامل

    def to_dict(self) -> dict:
        return {"original": self.original, "corrected": self.corrected, "reason": self.reason,
                "applied": self.applied, "rejected": self.why_rejected, "kind": self.kind}


@dataclass
class Correction:
    text: str                        # النص بعد التطبيق (أو الخام إن فشل)
    edits: list[Edit] = field(default_factory=list)
    ok: bool = True                  # False ⇒ بقي خاماً وعُلّم للمراجعة
    problem: str = ""
    attempts: int = 0
    used_image: bool = False

    @property
    def applied(self) -> int:
        return sum(1 for e in self.edits if e.applied)

    @property
    def rejected(self) -> int:
        return sum(1 for e in self.edits if not e.applied)


# ---------------------------------------------------------------------- التحليل
def parse_edits(reply: str) -> list[Edit]:
    """يستخرج قائمة التعديلات من رد النموذج. يرفع ValueError إن لم يكن JSON صالحاً."""
    s = (reply or "").strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("الرد ليس JSON")
    data = json.loads(m.group(0))
    items = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("لا توجد قائمة edits")
    out = []
    for it in items[:MAX_EDITS]:
        if isinstance(it, dict) and isinstance(it.get("original"), str) and isinstance(it.get("corrected"), str):
            out.append(Edit(it["original"], it["corrected"], str(it.get("reason", ""))[:200]))
    for line in (data.get("noise") or [])[:20]:
        if isinstance(line, str) and line.strip():
            out.append(Edit(line.strip(), "", "توقيع أو ختم", kind="noise"))
    return out


def protected_words(text: str) -> set[str]:
    """كلمات الأسماء والجهات المكتشفة في النص الخام (لا يمسها أي تعديل)."""
    words: set[str] = set()
    for _role, name, _raw in find_names(text):
        words.update(w for w in name.split() if len(w) >= 2)
    return words


def _digits_of(s: str) -> list[str]:
    return re.findall(r"\d+", normalize_for_compare(s))


def _word_re(o: str) -> re.Pattern:
    """الأصل ككلمة/عبارة كاملة فقط: "مر" لا تطابق داخل "المرقاب" أو "أمريكي"."""
    return re.compile(r"(?<!\w)" + re.escape(o) + r"(?!\w)")


def check_edit(e: Edit, text: str, protected: set[str]) -> str:
    """"" = مقبول، وإلا سبب الرفض."""
    o, c = e.original, e.corrected
    if not o.strip():
        return "أصل فارغ"
    if not _word_re(o).search(text):
        return "الأصل غير موجود ككلمة كاملة"
    if normalize_for_compare(o) == normalize_for_compare(c):
        return "لا تغيير (أرقام فقط)"
    if c == f"[{o}]":
        return ""                                   # تعليم شك: مسموح
    if "[" in c or "]" in c:
        return "تخمين بين [ ] (الأصل يبقى كما هو)"
    if _DIGIT.search(normalize_for_compare(o)) or _DIGIT.search(normalize_for_compare(c)):
        return "يمس رقماً"
    if _digits_of(o) != _digits_of(c):
        return "يمس رقماً"
    if "<!--" in o or "<!--" in c or o.count("|") != c.count("|") or "#" in o + c:
        return "يمس علامة صفحة أو جدول"
    ow = set(re.findall(r"[\u0621-\u064A]+", o))
    if ow & protected or _ENTITY_HEADS.search(o) or _ENTITY_HEADS.search(c):
        return "يمس اسماً أو جهة"
    lo, lc = len(o.strip()), len(c.strip())
    if abs(lc - lo) > max(3, 0.3 * lo):
        return "يغيّر الطول كثيراً"
    if abs(len(o.split()) - len(c.split())) > 1:
        return "يغيّر عدد الكلمات"
    return ""


NOISE_MAX_CHARS = 8


def check_noise(e: Edit, text: str, protected: set[str]) -> str:
    line = e.original
    if line not in (ln.strip() for ln in text.splitlines()):
        return "ليس سطراً كاملاً في النص"
    if len(line.split()) > 2 or len(line) > NOISE_MAX_CHARS:
        return "سطر أطول من أن يكون توقيعاً"
    if _DIGIT.search(normalize_for_compare(line)):
        return "يمس رقماً"
    if set(line.split()) & protected or _ENTITY_HEADS.search(line):
        return "يمس اسماً أو جهة"
    return ""


def apply_edits(text: str, edits: list[Edit]) -> str:
    protected = protected_words(text)
    out = text
    for e in edits:
        if e.kind == "noise":
            e.why_rejected = check_noise(e, out, protected)
            if not e.why_rejected:
                out = "\n".join(ln for ln in out.splitlines() if ln.strip() != e.original)
                e.applied = True
            continue
        e.why_rejected = check_edit(e, out, protected)
        if not e.why_rejected:
            out = _word_re(e.original).sub(lambda m, c=e.corrected: c, out)
            e.applied = True
    return out


def completeness_problem(raw: str, corrected: str) -> str:
    want = [m.group(1) for m in MARKER_RE.finditer(raw)]
    got = [m.group(1) for m in MARKER_RE.finditer(corrected)]
    if want != got:
        return f"علامات الصفحات تغيّرت ({want} ← {got})"
    a, b = len(raw.strip()), len(corrected.strip())
    if a and not (LEN_RATIO[0] <= b / a <= LEN_RATIO[1]):
        return f"نسبة الطول غير معقولة ({b / a:.2f})"
    return ""


# ---------------------------------------------------------------------- التشغيل
def correct(raw: str, ask, retries: int = 1) -> Correction:
    """ask(prompt, text) → رد النموذج. أخطاء المزوّد (الحصة، 429…) تُرفع للمستدعي."""
    res = Correction(raw)
    last = ""
    for attempt in range(1 + retries):
        res.attempts = attempt + 1
        try:
            edits = parse_edits(ask(raw))
        except (ValueError, json.JSONDecodeError) as e:
            last = f"رد غير صالح: {e}"
            continue
        fixed = apply_edits(raw, edits)
        problem = completeness_problem(raw, fixed)
        if problem:
            last = problem
            res.edits = edits
            for ed in edits:
                if ed.applied:
                    ed.applied, ed.why_rejected = False, "فشل فحص الاكتمال"
            continue
        res.text, res.edits, res.ok, res.problem = fixed, edits, True, ""
        return res
    res.text, res.ok, res.problem = raw, False, last
    return res


def render_report(source_name: str, pages: dict[int, dict]) -> str:
    """تقرير فروق بسيط للمراجعة: {صفحة: {edits, ok, problem, image}}."""
    out = [f"# تصحيحات OCR — {source_name}", ""]
    total_a = sum(sum(1 for e in p["edits"] if e["applied"]) for p in pages.values())
    total_r = sum(sum(1 for e in p["edits"] if not e["applied"]) for p in pages.values())
    out += [f"مطبّق: {total_a} — مرفوض: {total_r}", ""]
    for n in sorted(pages):
        p = pages[n]
        flag = "" if p.get("ok", True) else f" ⚠ تُركت خاماً: {p.get('problem', '')}"
        img = " (مع الصورة)" if p.get("image") else ""
        out += [f"## صفحة {n}{img}{flag}", ""]
        if not p["edits"]:
            out += ["_(لا تعديلات)_", ""]
            continue
        out += ["| الحالة | الأصل | المصحّح | السبب |", "| --- | --- | --- | --- |"]
        for e in p["edits"]:
            st = "✓" if e["applied"] else f"✗ {e['rejected']}"
            cells = [st, e["original"], e["corrected"], e["reason"]]
            out.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
